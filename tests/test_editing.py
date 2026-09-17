import base64
import hashlib
import http.client
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, PngImagePlugin

from image_lab import prompts, providers, references
from image_lab.providers import AppConfig, ProviderError, ValidationError
from image_lab.server import Application, LocalServer
from image_lab.store import filter_job, write_json_atomic
from test_image_lab import config_data, generated_image, parameters, png


def source_png(metadata=False, size=(128, 128)):
    image = Image.new("RGB", size, (190, 80, 50))
    content = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    if metadata:
        info.add_text("Comment", "private input metadata")
    image.save(content, "PNG", pnginfo=info)
    image.close()
    return content.getvalue()


class ReferenceTests(unittest.TestCase):
    def test_normalized_image_keeps_pixels_and_removes_metadata(self):
        raw = source_png(metadata=True)
        normalized, info = references.normalize(raw)
        self.assertEqual(info["upload_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(info["sha256"], hashlib.sha256(normalized).hexdigest())
        with Image.open(io.BytesIO(normalized)) as image:
            self.assertEqual(image.size, (128, 128))
            self.assertEqual(image.getpixel((0, 0)), (190, 80, 50))
            self.assertFalse(image.info)
        self.assertNotIn(b"private input metadata", normalized)

    def test_exif_orientation_is_applied_and_filename_is_not_recorded(self):
        image = Image.new("RGB", (128, 256), (150, 50, 100))
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "private camera metadata"
        content = io.BytesIO()
        image.save(content, "JPEG", exif=exif)
        normalized, metadata = references.normalize(content.getvalue())
        self.assertEqual((metadata["width"], metadata["height"]), (256, 128))
        with Image.open(io.BytesIO(normalized)) as decoded:
            self.assertFalse(decoded.getexif())

    def test_bad_or_oversized_files_never_pass_header_only_validation(self):
        for raw in (b"not-an-image", source_png()[:40], png(2, 2), b"x" * (references.MAX_UPLOAD_BYTES + 1)):
            with self.subTest(length=len(raw)), self.assertRaises(ValidationError):
                references.normalize(raw)
        image = Image.new("RGB", (4097, 64))
        content = io.BytesIO()
        image.save(content, "PNG")
        with self.assertRaises(ValidationError):
            references.normalize(content.getvalue())

    def test_animated_png_is_rejected(self):
        first = Image.new("RGB", (128, 128), "red")
        second = Image.new("RGB", (128, 128), "blue")
        content = io.BytesIO()
        first.save(content, "PNG", save_all=True, append_images=[second], duration=100)
        with self.assertRaises(ValidationError):
            references.normalize(content.getvalue())

    def test_reference_store_is_private_and_detects_modified_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = references.ReferenceStore(Path(temporary))
            reference = store.add(source_png())
            restored, content = store.get(reference["id"])
            self.assertEqual(restored["sha256"], hashlib.sha256(content).hexdigest())
            self.assertNotIn("original_filename", restored)
            with self.assertRaises(ValidationError):
                store.get("../private")
            (store.root / reference["id"] / "reference.png").write_bytes(b"changed")
            with self.assertRaises(ValidationError):
                store.get(reference["id"])

    def test_missing_optional_decoder_is_explicit(self):
        with patch("image_lab.references.importlib.util.find_spec", return_value=None):
            self.assertFalse(references.capabilities()["available"])
            with self.assertRaisesRegex(ValidationError, "requirements-edit"):
                references.normalize(source_png())


class EditProviderTests(unittest.TestCase):
    def setUp(self):
        self.config = AppConfig.parse(config_data())
        self.source, _ = references.normalize(source_png())

    def test_native_flux_sends_same_reference_bytes_without_changing_generation_contract(self):
        for model in self.config.models:
            if model.provider != "flux":
                continue
            body = providers.build_edit_request(model, "Change only the cup color.", "1024x1024", "medium")
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps({
                "data": [{"b64_json": base64.b64encode(png()).decode()}],
            }).encode())
            with patch("image_lab.providers.urllib.request.build_opener", return_value=opener):
                result = providers.edit(model, body, {"Authorization": "Bearer fake-token"}, 30, self.source)
            request = opener.open.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(base64.b64decode(payload["input_image"]), self.source)
            self.assertEqual(request.full_url, model.endpoint)
            self.assertNotIn("input_image", body)
            self.assertEqual(result.extension, "png")
            if model.id == "flux-2-flex":
                self.assertEqual(payload["steps"], 50)
                self.assertEqual(payload["guidance"], 4.5)

    def test_mai_and_all_gpt_slots_use_edits_with_one_multipart_image(self):
        for model in self.config.models:
            if model.provider == "flux":
                continue
            if not model.endpoint:
                model = replace(
                    model, endpoint="https://example.openai.azure.com/openai/v1/images/generations",
                )
            body = providers.build_edit_request(model, "\u53ea\u4fee\u6539\u676f\u5b50\u989c\u8272", "1024x1024", "medium")
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps({
                "data": [{"b64_json": base64.b64encode(png()).decode()}],
            }).encode())
            with patch("image_lab.providers.urllib.request.build_opener", return_value=opener):
                providers.edit(model, body, {"Authorization": "Bearer fake-token"}, 30, self.source)
            request = opener.open.call_args.args[0]
            self.assertIn("/images/edits", request.full_url)
            self.assertNotIn("/images/generations", request.full_url)
            content_type = request.get_header("Content-type")
            message = BytesParser(policy=policy.default).parsebytes(
                f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + request.data,
            )
            parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
            self.assertEqual(parts["image"].get_payload(decode=True), self.source)
            self.assertEqual(parts["image"].get_filename(), "reference.png")
            self.assertEqual(parts["prompt"].get_payload(decode=True).decode(), "\u53ea\u4fee\u6539\u676f\u5b50\u989c\u8272")
            if model.id == "mai-image-2-6":
                self.assertEqual(set(parts), {"model", "prompt", "image", "auto_aspect_ratio", "web_grounding"})
                self.assertEqual(parts["auto_aspect_ratio"].get_payload(decode=True), b"false")
                self.assertEqual(parts["web_grounding"].get_payload(decode=True), b"false")
                self.assertEqual(parts["model"].get_payload(decode=True), b"MAI-Image-2.6")
            elif model.provider == "mai":
                self.assertEqual(set(parts), {"model", "prompt", "image"})
            else:
                self.assertEqual(parts["quality"].get_payload(decode=True), b"medium")

    def test_edit_failure_never_retries_as_text_generation(self):
        model = self.config.models[0]
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            providers.edit_endpoint(model), 404, "not found", {}, io.BytesIO(b'{"error":{"message":"missing route"}}'),
        )
        with patch("image_lab.providers.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError):
                providers.edit(model, {}, {}, 30, self.source)
        self.assertEqual(opener.open.call_count, 1)
        self.assertIn("/edits", opener.open.call_args.args[0].full_url)
        with self.assertRaises(ProviderError):
            providers.edit(model, {}, {}, 30, b"")


class EditingFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        config = config_data()
        flare = next(model for model in config["models"] if model["id"] == "gpt-image-2-5")
        flare.update(
            enabled=True, endpoint="https://example.openai.azure.com/openai/v1/images/generations",
        )
        write_json_atomic(self.root / "image-lab.config.json", config)
        self.cli = patch("image_lab.providers.shutil.which", return_value="mock-azure-cli")
        self.cli.start()
        self.addCleanup(self.cli.stop)
        self.app = Application(self.root, self.root / "image-lab.config.json", self.root / "results", import_legacy=False)
        self.addCleanup(self.app.shutdown)
        self.auth = patch.object(self.app.credentials, "headers", return_value={})
        self.auth.start()
        self.addCleanup(self.auth.stop)
        self.edit_patch = patch("image_lab.server.providers.edit", return_value=generated_image())
        self.edit = self.edit_patch.start()
        self.addCleanup(self.edit_patch.stop)
        self.generate_patch = patch("image_lab.server.providers.generate", side_effect=AssertionError("No generation fallback"))
        self.generate = self.generate_patch.start()
        self.addCleanup(self.generate_patch.stop)
        self.reference = self.app.references.add(source_png(metadata=True))

    def payload(self, ids=None):
        return {
            **parameters(model_ids=ids or [model.id for model in self.app.config.models]),
            "operation": "edit", "reference_image_id": self.reference["id"], "reference_consent": True,
            "prompt": "Change only the red object to green. Keep everything else unchanged.",
        }

    def finish(self, payload=None):
        job = self.app.create_job(payload or self.payload())
        self.app.worker.join(timeout=10)
        self.assertFalse(self.app.worker.is_alive())
        return self.app.store.get(job["id"])


class EditApplicationTests(EditingFixture):
    def test_all_edit_models_share_one_source_with_complete_archives(self):
        job = self.finish()
        self.assertEqual(job["operation"], "edit")
        self.assertTrue(job["review_ready"])
        count = len(self.app.config.models)
        self.assertEqual(job["progress"], {"done": count, "total": count})
        self.assertEqual(self.edit.call_count, count)
        self.generate.assert_not_called()
        reference = self.app.store.reference_bytes(job["id"])
        self.assertTrue(all(call.args[-1] == reference for call in self.edit.call_args_list))
        self.assertTrue(all(sample["reference_sha256"] == self.reference["sha256"] for sample in job["samples"]))
        self.assertNotIn(base64.b64encode(reference).decode(), json.dumps(job))
        archive = self.app.archive_dir / job["archive"]["relative_dir"]
        self.assertTrue(job["archive"]["relative_dir"].startswith("edit-"))
        self.assertEqual((archive / "reference.png").read_bytes(), reference)
        manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["operation"], "edit")
        self.assertEqual(manifest["reference_image"]["sha256"], self.reference["sha256"])
        latest_mai = next(sample for sample in manifest["samples"] if sample["model"]["id"] == "mai-image-2-6")
        self.assertFalse(latest_mai["request_parameters"]["auto_aspect_ratio"])
        self.assertFalse(latest_mai["request_parameters"]["web_grounding"])
        self.assertFalse(any("endpoint" in model for model in manifest["models"]))
        self.assertEqual(self.app.store.history()[0]["operation"], "edit")

    def test_new_mai_edit_failure_cannot_be_hidden_to_unlock_old_mai_scoring(self):
        def editing(model, *args):
            if model.id == "mai-image-2-6":
                raise ProviderError("HTTP 429: new model limited")
            return generated_image()
        self.edit.side_effect = editing
        job = self.finish(self.payload(ids=["mai-image-2-5", "mai-image-2-6"]))
        self.assertEqual(self.edit.call_count, 2)
        self.assertFalse(job["review_ready"])
        self.assertFalse(filter_job(job, "mai")["review_ready"])
        self.assertEqual(job["samples"][0]["status"], "success")
        with self.assertRaises(ValidationError):
            self.app.rate(job["id"], job["samples"][0]["id"], {"adherence": 5})

    def test_missing_reference_and_permission_fail_before_any_model_call(self):
        for change in (
            {"reference_image_id": ""}, {"reference_image_id": "../file"},
            {"reference_consent": False}, {"reference_consent": "true"},
            {"operation": "generate"}, {"reference_image_id": "0" * 32},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                self.app.create_job({**self.payload(), **change})
        self.edit.assert_not_called()
        self.generate.assert_not_called()

    def test_scoring_gate_cannot_be_bypassed_by_a_successful_family_filter(self):
        def editing(model, *args):
            if model.provider == "flux":
                raise ProviderError("HTTP 429")
            return generated_image()
        self.edit.side_effect = editing
        job = self.finish()
        self.assertFalse(job["review_ready"])
        self.assertFalse(filter_job(job, "gpt")["review_ready"])
        sample = next(sample for sample in job["samples"] if sample["status"] == "success")
        with self.assertRaisesRegex(ValidationError, "all editing"):
            self.app.rate(job["id"], sample["id"], {"adherence": 4})
        self.assertTrue(all(sample["rating"] is None for sample in job["samples"]))

    def test_edit_scores_are_dimension_based_not_a_composite_score(self):
        job = self.finish()
        rated = self.app.rate(job["id"], job["samples"][0]["id"], {
            "adherence": 4, "visual": 5, "preservation": 3, "text": None, "notes": "Temporary test",
        })
        summary = rated["summary"][0]
        self.assertIsNone(summary["rating_mean"])
        self.assertEqual(summary["rated_count"], 1)
        self.assertEqual(summary["rating_dimensions"]["preservation"], {"mean": 3, "count": 1})
        with self.assertRaises(ValidationError):
            self.app.rate(job["id"], job["samples"][0]["id"], {"composition": 5})
        restored = Application(self.root, self.root / "image-lab.config.json", self.root / "results", import_legacy=False)
        self.assertTrue(restored.store.get(job["id"])["review_ready"])
        self.assertEqual(restored.store.reference_bytes(job["id"]), self.app.store.reference_bytes(job["id"]))

    def test_running_edit_and_cancellation_keep_review_locked(self):
        entered, release = threading.Event(), threading.Event()
        def editing(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test release timed out")
            return generated_image()
        self.edit.side_effect = editing
        job = self.app.create_job({**self.payload(), "mode": "sequential"})
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(ValidationError):
                self.app.rate(job["id"], job["samples"][0]["id"], {"adherence": 4})
            self.app.cancel(job["id"])
        finally:
            release.set()
        self.app.worker.join(timeout=10)
        result = self.app.store.get(job["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["review_ready"])
        self.assertEqual(self.edit.call_count, 1)

    def test_translation_keeps_edit_contract_and_never_sends_images_to_translator(self):
        config = self.app.config
        self.app.config = replace(
            config,
            models=tuple(replace(model, prompt_policy="english") for model in config.models),
            translation=providers.TranslationConfig(
                enabled=True, deployment="translator",
                endpoint="https://example.openai.azure.com/openai/v1/chat/completions",
            ),
        )
        with patch("image_lab.server.prompts.translate", return_value=prompts.TranslationResult("Turn the red object green.", 12, 10, None)) as translate:
            job = self.finish({**self.payload(), "prompt": "\u5c06\u7ea2\u8272\u7269\u4f53\u53d8\u6210\u7eff\u8272"})
        self.assertEqual(translate.call_count, 1)
        self.assertIsInstance(translate.call_args.args[0], str)
        self.assertTrue(all(sample["prompt_variant"] == "translated_english" for sample in job["samples"]))
        self.assertEqual(set(job["samples"][0]["request"]), {"model", "prompt"})
        self.assertTrue(all(call.args[1]["prompt"] == "Turn the red object green." for call in self.edit.call_args_list))

    def test_input_hash_is_checked_before_billing(self):
        directory = self.app.references.root / self.reference["id"]
        (directory / "reference.png").write_bytes(b"tampered")
        with self.assertRaises(ValidationError):
            self.app.create_job(self.payload())
        self.edit.assert_not_called()


class ReferenceHttpTests(EditingFixture):
    def test_binary_upload_requires_csrf_and_serves_only_the_normalized_reference(self):
        server = LocalServer(("127.0.0.1", 0), self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        try:
            headers = {"Content-Type": "image/png", "X-Image-Lab-Token": self.app.csrf_token}
            self.assertEqual(request("POST", "/api/references", source_png())[0], 403)
            self.assertEqual(request("POST", "/api/references", b"junk", headers)[0], 400)
            status, body = request("POST", "/api/references", source_png(metadata=True), headers)
            self.assertEqual(status, 201)
            reference = json.loads(body)
            status, image = request("GET", reference["image_url"])
            self.assertEqual(status, 200)
            self.assertEqual(hashlib.sha256(image).hexdigest(), reference["sha256"])
            self.assertNotIn(b"private input metadata", image)
            self.assertEqual(request("GET", "/references/../../image-lab.config.json")[0], 404)
            payload = self.payload()
            payload["reference_image_id"] = reference["id"]
            status, body = request("POST", "/api/jobs", json.dumps(payload), {
                "Content-Type": "application/json", "X-Image-Lab-Token": self.app.csrf_token,
            })
            self.assertEqual(status, 202)
            job_id = json.loads(body)["id"]
            self.app.worker.join(timeout=10)
            self.assertEqual(request("GET", f"/images/{job_id}/reference.png"), (200, image))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
