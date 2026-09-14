import base64
import csv
import http.client
import io
import json
import os
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
import zlib
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from image_lab import prompts, providers
from image_lab.archive import ImageArchive, component
from image_lab.locking import AlreadyRunningError, single_instance
from image_lab.providers import (
    AppConfig, Credentials, GeneratedImage, ModelConfig, ProviderError,
    TranslationConfig, ValidationError, build_request, generate, image_info,
)
from image_lab.server import Application, ConflictError, LocalServer, export_csv, parse_job_input
from image_lab.store import RunStore, filter_job, summarize, validate_rating, write_json_atomic


ROOT = Path(__file__).resolve().parent.parent


def png(width=2, height=2, pixel=b"\x20\x40\x60"):
    def chunk(kind, content):
        data = kind + content
        return struct.pack(">I", len(content)) + data + struct.pack(">I", zlib.crc32(data))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + pixel * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def config_data():
    data = json.loads((ROOT / "image-lab.config.example.json").read_text(encoding="utf-8"))
    data["resource_group"] = "image-lab-tests"
    endpoints = {
        "mai-image-2-5": "https://example.services.ai.azure.com/mai/v1/images/generations",
        "gpt-image-2": "https://example.openai.azure.com/openai/deployments/gpt-image-2/images/generations?api-version=2025-04-01-preview",
        "flux-1-kontext-pro": "https://example.services.ai.azure.com/providers/blackforestlabs/v1/flux-kontext-pro?api-version=preview",
        "flux-2-pro": "https://example.services.ai.azure.com/providers/blackforestlabs/v1/flux-2-pro?api-version=preview",
        "flux-2-flex": "https://example.services.ai.azure.com/providers/blackforestlabs/v1/flux-2-flex?api-version=preview",
        "gpt-image-2-5-sunburst": "https://example.openai.azure.com/openai/deployments/gpt-image-2.5-sunburst/images/generations?api-version=2025-04-01-preview",
    }
    for model in data["models"]:
        if model["id"] != "gpt-image-2-5":
            model["enabled"] = True
            model["endpoint"] = endpoints[model["id"]]
            model["prompt_policy"] = "original"
    return data


def generated_image():
    return GeneratedImage(png(), "png", 2, 2, 12.5, 0, None)


def parameters(runs=1, mode="parallel", model_ids=None):
    return {
        "prompt": "\u6625\u65e5\u8bfb\u4e66\u4f1a",
        "model_ids": model_ids or ["mai-image-2-5", "gpt-image-2", "flux-1-kontext-pro"],
        "size": "1024x1024",
        "gpt_quality": "medium",
        "runs": runs,
        "mode": mode,
    }


class ProviderTests(unittest.TestCase):
    def setUp(self):
        cli = patch("image_lab.providers.shutil.which", return_value="mock-azure-cli")
        cli.start()
        self.addCleanup(cli.stop)
        self.config = AppConfig.parse(config_data())
        self.mai, self.gpt, self.future, self.flux, self.flux_pro, self.flux_flex, self.sunburst = self.config.models

    def test_flux_two_requests_use_native_dimensions_and_record_flex_controls(self):
        pro = build_request(self.flux_pro, "Red cube", "2048x2048", "medium")
        self.assertEqual(pro["width"], 2048)
        self.assertEqual(pro["height"], 2048)
        self.assertEqual(pro["num_images"], 1)
        self.assertNotIn("aspect_ratio", pro)
        self.assertNotIn("steps", pro)
        flex = build_request(self.flux_flex, "Red cube", "1536x1024", "medium")
        self.assertEqual(flex["steps"], 50)
        self.assertEqual(flex["guidance"], 4.5)
        self.assertNotIn("quality", flex)

    def test_flux_two_route_and_parameter_validation(self):
        for values in (
            {"endpoint": self.flux_pro.endpoint},
            {"endpoint": "https://example.services.ai.azure.com/openai/v1/images/generations"},
            {"flux_steps": 0}, {"flux_steps": 51}, {"flux_steps": True},
            {"flux_guidance": 1.4}, {"flux_guidance": float("nan")},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                ModelConfig.parse({**self.flux_flex.public(), **values})

    def test_four_slot_configuration_migrates_without_enabling_services(self):
        original = config_data()
        original["models"] = original["models"][:4]
        original.pop("translation")
        for model in original["models"]:
            model.pop("prompt_policy", None)
        migrated = AppConfig.parse(original)
        self.assertEqual(len(migrated.models), 7)
        self.assertEqual(migrated.models[0].endpoint, original["models"][0]["endpoint"])
        self.assertTrue(all(not model.enabled for model in migrated.models[4:]))
        self.assertFalse(migrated.translation.enabled)
        self.assertEqual(migrated.models[3].prompt_policy, "original")

    def test_flare_and_sunburst_use_separate_gpt_requests(self):
        flare = replace(
            self.future, enabled=True,
            endpoint="https://example.openai.azure.com/openai/deployments/gpt-image-2.5-flare/images/generations?api-version=2025-04-01-preview",
        )
        self.assertEqual(ModelConfig.parse(flare.public()).deployment, "gpt-image-2.5-flare")
        for model in (flare, self.sunburst):
            body = build_request(model, "A bookstore poster.", "1024x1024", "medium")
            self.assertEqual(body["n"], 1)
            self.assertEqual(body["quality"], "medium")
            self.assertEqual(body["output_format"], "png")
            self.assertNotIn("model", body)
        self.assertNotEqual(flare.id, self.sunburst.id)
        self.assertNotEqual(flare.endpoint, self.sunburst.endpoint)

    def test_six_slot_configuration_adds_disabled_sunburst_without_relabeling_history(self):
        original = config_data()
        original["models"] = original["models"][:6]
        original["models"][2]["name"] = "My existing GPT 2.5 configuration"
        migrated = AppConfig.parse(original)
        self.assertEqual(migrated.models[2].name, original["models"][2]["name"])
        self.assertEqual(migrated.models[-1].id, "gpt-image-2-5-sunburst")
        self.assertFalse(migrated.models[-1].enabled)
        self.assertFalse(migrated.models[-1].endpoint)

    def test_adapters_preserve_prompt_and_only_send_supported_controls(self):
        prompt = "\u4e2d\u6587 poster"
        self.assertEqual(build_request(self.mai, prompt, "1024x1024", "high"), {
            "model": "MAI-Image-2.5", "prompt": prompt, "width": 1024, "height": 1024,
        })
        gpt = build_request(self.gpt, prompt, "1024x1536", "high")
        self.assertEqual(gpt["quality"], "high")
        self.assertEqual(gpt["size"], "1024x1536")
        self.assertNotIn("model", gpt)
        flux = build_request(self.flux, prompt, "1024x1024", "high")
        self.assertEqual(flux["model"], "FLUX.1-Kontext-pro")
        self.assertNotIn("quality", flux)
        self.assertEqual(flux["aspect_ratio"], "1:1")
        self.assertEqual(flux["output_format"], "png")

    def test_flux_image_api_remains_supported_without_native_parameters(self):
        model = replace(
            self.flux, endpoint="https://example.services.ai.azure.com/openai/v1/images/generations?api-version=preview",
        )
        body = build_request(model, "p", "1024x1024", "medium")
        self.assertEqual(body["size"], "1024x1024")
        self.assertEqual(body["n"], 1)
        self.assertNotIn("quality", body)
        self.assertNotIn("aspect_ratio", body)
        self.assertNotIn("output_format", body)

    def test_native_flux_endpoint_and_key_header_are_validated(self):
        self.assertEqual(ModelConfig.parse(self.flux.public()).provider, "flux")
        with self.assertRaises(ValidationError):
            ModelConfig.parse({**self.flux.public(), "auth_mode": "api_key_env", "api_key_header": "api-key"})

    def test_gpt_v1_body_includes_deployment(self):
        model = replace(self.gpt, endpoint="https://example.openai.azure.com/openai/v1/images/generations")
        self.assertEqual(build_request(model, "p", "1024x1024", "medium")["model"], "gpt-image-2")

    def test_size_intersection_enforced_server_side(self):
        for model in (self.mai, self.flux):
            with self.assertRaises(ValidationError):
                build_request(model, "p", "1536x1024", "medium")

    def test_future_slot_is_explicitly_unconfigured(self):
        self.assertFalse(self.future.public()["configured"])
        self.assertIn("real endpoint", self.future.public()["configuration_error"])
        with self.assertRaises(ValidationError):
            parse_job_input(parameters(model_ids=[self.future.id]), self.config)

    def test_endpoint_validation_blocks_token_exfiltration_and_secret_queries(self):
        invalid = [
            "http://example.openai.azure.com/openai/v1/images/generations",
            "https://127.0.0.1/openai/v1/images/generations",
            "https://example.openai.azure.com.evil.test/openai/v1/images/generations",
            "https://user:secret@example.openai.azure.com/openai/v1/images/generations",
            "https://example.openai.azure.com/openai/v1/images/generations?api-key=secret",
            "https://api.openai.com/v1/images/generations",
        ]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValidationError):
                ModelConfig.parse({**self.gpt.public(), "endpoint": endpoint})

    def test_config_does_not_accept_secret_values_or_mismatched_deployment(self):
        for change in (
            {"api_key": "not-a-real-secret"},
            {"api_key_env": "sk-not-an-environment-variable"},
            {"deployment": "different-deployment"},
            {"enabled": 1},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                ModelConfig.parse({**self.gpt.public(), **change})

    def test_environment_key_never_appears_in_public_config(self):
        model = replace(self.gpt, auth_mode="api_key_env", api_key_env="IMAGE_LAB_TEST_KEY")
        with patch.dict(os.environ, {"IMAGE_LAB_TEST_KEY": "unit-test-key-value"}):
            self.assertNotIn("unit-test-key-value", json.dumps(model.public()))
            self.assertEqual(Credentials().headers(model, self.config.subscription_id), {"api-key": "unit-test-key-value"})
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(model.public()["configured"])

    def test_azure_token_cache_and_no_expiry_no_cache(self):
        credentials = Credentials()
        with patch.object(credentials, "_get_azure_token", return_value=("unit-test-token", time.time() + 3600)) as get:
            self.assertEqual(credentials.headers(self.mai, self.config.subscription_id), {"Authorization": "Bearer unit-test-token"})
            credentials.headers(self.flux, self.config.subscription_id)
            self.assertEqual(get.call_count, 1)
        credentials = Credentials()
        with patch.object(credentials, "_get_azure_token", return_value=("unit-test-token", 0)) as get:
            credentials.headers(self.mai, self.config.subscription_id)
            credentials.headers(self.mai, self.config.subscription_id)
            self.assertEqual(get.call_count, 2)

    def test_base64_image_and_utf8_request(self):
        raw = json.dumps({"data": [{"b64_json": base64.b64encode(png()).decode()}]}).encode()
        opener = Mock()
        opener.open.return_value = io.BytesIO(raw)
        body = build_request(self.mai, "\u4e2d\u6587", "1024x1024", "medium")
        with patch("image_lab.providers.urllib.request.build_opener", return_value=opener):
            image = generate(self.mai, body, {"Authorization": "Bearer unit-token"}, 30)
        self.assertEqual((image.extension, image.width, image.height), ("png", 2, 2))
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)["prompt"], "\u4e2d\u6587")
        self.assertGreaterEqual(image.api_ms, 0)

    def test_url_image_download_does_not_forward_credentials(self):
        url = "https://example.blob.core.windows.net/images/file.png?sig=unit-test-signature"
        api, download = Mock(), Mock()
        api.open.return_value = io.BytesIO(json.dumps({"data": [{"url": url}]}).encode())
        download.open.return_value = io.BytesIO(png())
        with patch("image_lab.providers.urllib.request.build_opener", side_effect=[api, download]):
            image = generate(self.flux, {"prompt": "p"}, {"Authorization": "Bearer private-unit-token"}, 30)
        self.assertEqual(image.width, 2)
        self.assertEqual(download.open.call_args.args, (url,))
        self.assertNotIn("headers", download.open.call_args.kwargs)

    def test_untrusted_image_urls_and_redirects_are_refused(self):
        for url in ("http://example.blob.core.windows.net/a", "https://localhost/a", "https://example.test/a"):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                providers.validate_image_url(url)
            with self.subTest(redirect=url), self.assertRaises(ProviderError):
                providers._ImageRedirect().redirect_request(None, None, 302, "", {}, url)

    def test_api_redirects_are_not_followed(self):
        self.assertIsNone(providers._NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test"))

    def test_invalid_responses_surface_errors(self):
        for raw in (b"not-json", b'{"data":[]}', b'{"data":[{"b64_json":"%%%"}]}', b'{"id":"async-not-supported"}'):
            opener = Mock()
            opener.open.return_value = io.BytesIO(raw)
            with self.subTest(raw=raw), patch("image_lab.providers.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(ProviderError):
                    generate(self.mai, {}, {}, 30)

    def test_rate_limit_error_is_visible_and_not_retried_or_leaked(self):
        payload = {"error": {"message": "Bearer unit-token https://example.test/a?sig=secret"}}
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError(
            self.mai.endpoint, 429, "Too Many Requests", {},
            io.BytesIO(json.dumps(payload).encode()),
        )
        with patch("image_lab.providers.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError) as raised:
                generate(self.mai, {}, {"Authorization": "Bearer unit-token"}, 30)
        self.assertIn("429", str(raised.exception))
        self.assertIn("no automatic retry", str(raised.exception))
        self.assertNotIn("unit-token", str(raised.exception))
        self.assertNotIn("sig=secret", str(raised.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_redaction_also_covers_bare_echoed_bearer_tokens(self):
        message = providers.redact_error("Rejected raw unit-token-value", ("Bearer unit-token-value",))
        self.assertNotIn("unit-token-value", message)
        self.assertIn("[redacted]", message)

    def test_image_dimensions_and_invalid_content(self):
        self.assertEqual(image_info(png(12, 7)), ("png", 12, 7))
        jpeg_header = b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x02\x00\x03\x01\x01\x11\x00\xff\xd9"
        self.assertEqual(image_info(jpeg_header), ("jpg", 3, 2))
        for raw in (b"", b"<html>failure</html>", png()[:20]):
            with self.assertRaises(ProviderError):
                image_info(raw)

    def test_invalid_job_parameters(self):
        for change in (
            {"runs": True}, {"runs": 0}, {"runs": 11},
            {"prompt": " "}, {"prompt": "a" * 4001}, {"model_ids": ["unknown"]},
            {"model_ids": ["gpt-image-2", "gpt-image-2"]}, {"mode": "anything"},
            {"size": "1536x1024"}, {"gpt_quality": "hd"},
            {"topic": "x" * 81}, {"topic": 12},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                parse_job_input({**parameters(), **change}, self.config)


class WorkbenchFixture(unittest.TestCase):
    def setUp(self):
        cli = patch("image_lab.providers.shutil.which", return_value="mock-azure-cli")
        cli.start()
        self.addCleanup(cli.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "image-lab.config.json"
        write_json_atomic(self.config_path, config_data())
        self.app = Application(self.root, self.config_path, self.root / "results", import_legacy=False)
        self.auth_patch = patch.object(self.app.credentials, "headers", return_value={"Authorization": "Bearer unit-token"})
        self.auth_patch.start()
        self.generate_patch = patch("image_lab.server.providers.generate", return_value=generated_image())
        self.generate = self.generate_patch.start()

    def tearDown(self):
        self.app.shutdown()
        self.generate_patch.stop()
        self.auth_patch.stop()
        self.temporary.cleanup()

    def run_job(self, params=None):
        initial = self.app.create_job(params or parameters())
        self.app.worker.join(timeout=10)
        self.assertFalse(self.app.worker.is_alive())
        return self.app.store.get(initial["id"])


class ApplicationTests(WorkbenchFixture):
    def test_older_settings_clients_do_not_erase_new_model_connections(self):
        old = self.app.config.public()
        old["models"] = old["models"][:4]
        old.pop("translation")
        original = self.app.config.models[4:]
        saved = self.app.save_config(old)
        self.assertEqual(len(saved["models"]), 7)
        for model in original:
            after = next(item for item in saved["models"] if item["id"] == model.id)
            self.assertEqual(after["endpoint"], model.endpoint)
            self.assertEqual(after["enabled"], model.enabled)

    def enable_english_routing(self, translator=True):
        settings = TranslationConfig(
            enabled=translator,
            endpoint="https://example.openai.azure.com/openai/deployments/translator/chat/completions?api-version=2024-10-21",
            deployment="translator",
        )
        self.app.config = replace(
            self.app.config, translation=settings,
            models=tuple(replace(model, prompt_policy="english") if model.provider == "flux" else model for model in self.app.config.models),
        )

    def test_english_translation_is_shared_once_and_actual_inputs_are_archived(self):
        self.enable_english_routing()
        original = "\u8bbe\u8ba1\u6d77\u62a5\uff0c\u6807\u9898\u201c\u6625\u65e5\u8bfb\u4e66\u4f1a\u201d\u3002"
        english = 'Design a poster with the exact title "\u6625\u65e5\u8bfb\u4e66\u4f1a".'
        with patch("image_lab.server.prompts.translate", return_value=prompts.TranslationResult(english, 9999, 9000, {"total_tokens": 120})) as translate:
            job = self.run_job({**parameters(runs=2), "prompt": original})
        self.assertEqual(translate.call_count, 1)
        self.assertEqual(job["english_prompt"], english)
        self.assertEqual(job["translation"]["elapsed_ms"], 9999)
        for sample in job["samples"]:
            is_flux = sample["model_id"] == "flux-1-kontext-pro"
            self.assertEqual(sample["request"]["prompt"], english if is_flux else original)
            self.assertEqual(sample["effective_prompt"], english if is_flux else original)
            self.assertEqual(sample["original_prompt"], original)
            self.assertEqual(sample["prompt_variant"], "translated_english" if is_flux else "original")
            self.assertLess(sample["elapsed_ms"], 9999)
        folder = self.app.archive_dir / job["archive"]["relative_dir"]
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        flux = next(sample for sample in manifest["samples"] if sample["model"]["id"] == "flux-1-kontext-pro")
        self.assertEqual(flux["prompt"], english)
        self.assertEqual(flux["original_prompt"], original)
        self.assertNotIn("deployment", manifest["translation"])
        self.assertIn("_en.png", flux["image"]["file"])
        self.assertTrue(any("different prompt-language" in warning for warning in job["warnings"]))

    def test_reviewed_english_counterpart_needs_no_translation_call(self):
        self.enable_english_routing(translator=False)
        with patch("image_lab.server.prompts.translate") as translate:
            job = self.run_job({**parameters(), "english_prompt": "A bookstore poster."})
        translate.assert_not_called()
        flux = next(sample for sample in job["samples"] if sample["model_id"] == "flux-1-kontext-pro")
        self.assertEqual(flux["prompt_variant"], "provided_english")
        self.assertEqual(job["translation"]["status"], "not_needed")

    def test_language_diagnostic_mode_preserves_chinese_for_every_model(self):
        self.enable_english_routing(translator=False)
        with patch("image_lab.server.prompts.translate") as translate:
            job = self.run_job({**parameters(), "language_mode": "original_all"})
        translate.assert_not_called()
        self.assertTrue(all(sample["request"]["prompt"] == job["prompt"] for sample in job["samples"]))

    def test_english_failure_does_not_fall_back_to_chinese_or_block_original_models(self):
        self.enable_english_routing()
        with patch("image_lab.server.prompts.translate", side_effect=prompts.TranslationError("Refused", 123, True)):
            job = self.run_job()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["translation"]["status"], "failed")
        self.assertEqual(self.generate.call_count, 2)
        flux = next(sample for sample in job["samples"] if sample["model_id"] == "flux-1-kontext-pro")
        self.assertEqual(flux["status"], "failed")
        self.assertIsNone(flux["started_at"])
        self.assertIsNone(flux["effective_prompt"])
        self.assertIn("preparation failed", flux["error"])

    def test_missing_english_service_is_rejected_before_billing(self):
        self.enable_english_routing(translator=False)
        with self.assertRaises(ValidationError):
            self.app.create_job(parameters())
        self.generate.assert_not_called()
        self.assertFalse(self.app.store.history())

    def test_cancellation_during_translation_sends_no_image_requests(self):
        self.enable_english_routing()
        entered, release = threading.Event(), threading.Event()
        def translate(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test release timeout")
            return prompts.TranslationResult("A bookstore poster.", 10, 5, None)
        with patch("image_lab.server.prompts.translate", side_effect=translate):
            job = self.app.create_job(parameters())
            try:
                self.assertTrue(entered.wait(5))
                self.app.cancel(job["id"])
            finally:
                release.set()
            self.app.worker.join(timeout=10)
        self.generate.assert_not_called()
        self.assertEqual(self.app.store.get(job["id"])["status"], "cancelled")

    def test_all_seven_slots_can_participate_without_a_four_model_limit(self):
        future = next(model for model in self.app.config.models if model.id == "gpt-image-2-5")
        future = replace(
            future, enabled=True, deployment="gpt-image-2.5",
            endpoint="https://example.openai.azure.com/openai/v1/images/generations",
        )
        self.app.config = replace(
            self.app.config,
            models=tuple(future if model.id == future.id else model for model in self.app.config.models),
        )
        job = self.run_job(parameters(model_ids=[model.id for model in self.app.config.models]))
        self.assertEqual(job["progress"], {"done": 7, "total": 7})

    def test_family_filter_is_a_view_and_keeps_statistics_in_the_same_experiment(self):
        job = self.run_job()
        filtered = filter_job(job, "gpt")
        self.assertEqual(filtered["id"], job["id"])
        self.assertEqual(filtered["view_filter"], "gpt")
        self.assertEqual(filtered["model_ids"], ["gpt-image-2"])
        self.assertEqual(filtered["progress"], {"done": 1, "total": 1})
        self.assertEqual(len(filtered["summary"]), 1)
        self.assertEqual(filtered["summary"][0]["mean_ms"], job["summary"][1]["mean_ms"])
        self.assertEqual(len(job["samples"]), 3)
        self.assertEqual(len(self.app.store.get(job["id"])["models"]), 3)
        self.assertEqual(self.app.store.history()[0]["providers"], ["flux", "gpt", "mai"])
        with self.assertRaises(ValidationError):
            filter_job(job, "unknown")
        with self.assertRaises(ValidationError):
            filter_job(filtered, "flux")

    def test_readable_archive_has_model_topic_round_and_complete_notes(self):
        job = self.run_job({**parameters(), "topic": "Bookstore / \u4e2d\u6587\u6d77\u62a5"})
        self.assertEqual(job["topic"], "Bookstore / \u4e2d\u6587\u6d77\u62a5")
        self.assertEqual(job["archive"]["status"], "ready")
        folder = self.app.archive_dir / job["archive"]["relative_dir"]
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["image_count"], 3)
        for row in manifest["samples"]:
            self.assertIn(row["model"]["id"], row["image"]["file"])
            self.assertIn("round-01", row["image"]["file"])
            self.assertEqual(row["topic"], job["topic"])
            self.assertEqual(row["prompt"], job["prompt"])
            self.assertEqual((folder / row["image"]["file"]).read_bytes(), png())
            self.assertEqual(len(row["image"]["sha256"]), 64)
            self.assertTrue((folder / row["notes_file"]).is_file())
        self.assertTrue((folder / "README.md").is_file())

    def test_archive_omits_connection_details_keys_paths_and_private_errors(self):
        job = self.run_job()
        job["models"][0].update(
            endpoint="https://personal-resource.example.test/",
            deployment="private-customer-deployment",
            subscription_id="private-subscription",
            api_key="real-looking-but-fake-secret",
        )
        job["samples"][0]["request"]["api_key"] = "request-secret-value"
        job["samples"][0]["error"] = "HTTP 403: private-error-with-credentials"
        archive = ImageArchive(self.app.store.output, self.root / "filtered-archive")
        result = archive.export(job, include_reviews=False)
        folder = archive.destination / result["relative_dir"]
        text = (folder / "manifest.json").read_text(encoding="utf-8")
        for secret in (
            "personal-resource", "private-customer-deployment", "private-subscription",
            "real-looking-but-fake-secret", "request-secret-value", "private-error-with-credentials",
            str(self.root),
        ):
            self.assertNotIn(secret, text)
        self.assertEqual(json.loads(text)["samples"][0]["http_error_code"], 403)

    def test_archive_ratings_refresh_and_public_examples_can_omit_them(self):
        job = self.run_job()
        updated = self.app.rate(job["id"], job["samples"][0]["id"], {"visual": 5, "notes": "Private reviewer note"})
        folder = self.app.archive_dir / updated["archive"]["relative_dir"]
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["samples"][0]["human_review"]["notes"], "Private reviewer note")
        archive = ImageArchive(self.app.store.output, self.root / "example-export")
        published = archive.export(updated, include_reviews=False)
        text = (archive.destination / published["relative_dir"] / "manifest.json").read_text(encoding="utf-8")
        self.assertNotIn("Private reviewer note", text)
        self.assertIsNone(json.loads(text)["samples"][0]["human_review"])

    def test_archive_does_not_overwrite_an_edited_image(self):
        job = self.run_job()
        folder = self.app.archive_dir / job["archive"]["relative_dir"]
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        image = folder / manifest["samples"][0]["image"]["file"]
        image.write_bytes(b"user-edited-copy")
        updated = self.app.sync_archive(job["id"])
        self.assertEqual(updated["archive"]["status"], "failed")
        self.assertEqual(updated["status"], "completed")
        self.assertEqual(image.read_bytes(), b"user-edited-copy")
        self.assertEqual(self.generate.call_count, 3)

    def test_archive_marks_missing_successful_images_without_replacements(self):
        job = self.run_job()
        self.app.store.update_sample(job["id"], job["samples"][0]["id"], filename=None, image_url=None)
        archive = ImageArchive(self.app.store.output, self.root / "missing-image-archive")
        result = archive.export(self.app.store.get(job["id"]))
        manifest = json.loads((archive.destination / result["relative_dir"] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result["missing_images"], 1)
        self.assertEqual(result["image_count"], 2)
        self.assertEqual(manifest["samples"][0]["image"]["status"], "not_retained")
        self.assertIsNone(manifest["samples"][0]["image"]["file"])

    def test_parallel_rounds_are_real_bounded_concurrency(self):
        barrier = threading.Barrier(3)
        def generation(*args):
            barrier.wait(timeout=5)
            return generated_image()
        self.generate.side_effect = generation
        job = self.run_job(parameters(runs=2))
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["progress"], {"done": 6, "total": 6})
        self.assertEqual(self.generate.call_count, 6)
        self.assertTrue(all(row["successes"] == 2 for row in job["summary"]))
        self.assertTrue(all(row["rating_mean"] is None for row in job["summary"]))
        for sample in job["samples"]:
            self.assertTrue(self.app.store.image_path(job["id"], sample["filename"]).is_file())
            self.assertEqual(sample["width"], 2)
            self.assertTrue(sample["warnings"])
            self.assertGreaterEqual(sample["elapsed_ms"], sample["save_ms"])

    def test_sequential_rounds_rotate_order(self):
        job = self.run_job(parameters(runs=2, mode="sequential"))
        order = [call.args[0].id for call in self.generate.call_args_list]
        self.assertEqual(order, [
            "mai-image-2-5", "gpt-image-2", "flux-1-kontext-pro",
            "gpt-image-2", "flux-1-kontext-pro", "mai-image-2-5",
        ])
        self.assertEqual(job["status"], "completed")

    def test_cancellation_keeps_inflight_result_and_stops_pending_calls(self):
        entered, release = threading.Event(), threading.Event()
        def generation(*args):
            entered.set()
            if not release.wait(timeout=5):
                raise RuntimeError("test release timed out")
            return generated_image()
        self.generate.side_effect = generation
        job = self.app.create_job(parameters(runs=2, mode="sequential"))
        try:
            self.assertTrue(entered.wait(timeout=5))
            with self.assertRaises(ConflictError):
                self.app.create_job(parameters())
            with self.assertRaises(ConflictError):
                self.app.save_config(self.app.config.public())
            self.app.cancel(job["id"])
        finally:
            release.set()
        self.app.worker.join(timeout=10)
        final = self.app.store.get(job["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(self.generate.call_count, 1)
        self.assertEqual(sum(row["status"] == "success" for row in final["samples"]), 1)
        self.assertEqual(sum(row["status"] == "cancelled" for row in final["samples"]), 5)

    def test_provider_failure_does_not_hide_other_results(self):
        def generation(model, *args):
            if model.provider == "flux":
                raise ProviderError("HTTP 429: Rate limited; no automatic retry.")
            return generated_image()
        self.generate.side_effect = generation
        job = self.run_job()
        self.assertEqual(job["status"], "completed")
        flux = next(row for row in job["summary"] if row["model_id"] == "flux-1-kontext-pro")
        self.assertEqual(flux["failures"], 1)
        self.assertIsNone(flux["mean_ms"])
        self.assertEqual(self.generate.call_count, 3)

    def test_auth_failure_never_sends_an_image_request_or_invents_latency(self):
        self.app.credentials.headers.side_effect = ProviderError("Login expired")
        job = self.run_job()
        self.assertEqual(job["status"], "failed")
        self.generate.assert_not_called()
        self.assertTrue(all(sample["elapsed_ms"] is None for sample in job["samples"]))
        self.assertTrue(all(row["attempted"] == 0 for row in job["summary"]))

    def test_ratings_persist_and_null_is_not_a_zero_score(self):
        job = self.run_job()
        sample_id = job["samples"][0]["id"]
        updated = self.app.store.rate(job["id"], sample_id, {
            "adherence": 5, "visual": 4, "composition": None, "text": None, "notes": "Review note",
        })
        self.assertEqual(updated["summary"][0]["rating_mean"], 4.5)
        self.assertEqual(updated["summary"][0]["rated_count"], 1)
        reloaded = RunStore(self.app.store.output).get(job["id"])
        self.assertEqual(reloaded["samples"][0]["rating"]["notes"], "Review note")
        cleared = self.app.store.rate(job["id"], sample_id, {})
        self.assertIsNone(cleared["summary"][0]["rating_mean"])
        self.assertEqual(cleared["summary"][0]["rated_count"], 0)

    def test_invalid_ratings_and_missing_samples(self):
        for value in (0, 6, True, 2.5, "5"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                validate_rating({"visual": value})
        with self.assertRaises(ValidationError):
            validate_rating({"notes": "x" * 2001})
        job = self.run_job()
        with self.assertRaises(KeyError):
            self.app.store.rate(job["id"], "not-a-sample", {"visual": 5})

    def test_restart_marks_incomplete_calls_interrupted_without_retrying(self):
        job = self.run_job()
        self.app.store.update_job(job["id"], status="running")
        self.app.store.update_sample(job["id"], job["samples"][0]["id"], status="running")
        calls_before = self.generate.call_count
        reloaded = RunStore(self.app.store.output).get(job["id"])
        self.assertEqual(reloaded["status"], "interrupted")
        self.assertEqual(reloaded["samples"][0]["status"], "interrupted")
        self.assertEqual(self.generate.call_count, calls_before)

    def test_failed_persistence_is_visible_even_when_disk_cannot_record_the_error(self):
        job = self.run_job()
        self.app.store.update_job(job["id"], status="running")
        self.app.store.update_sample(job["id"], job["samples"][0]["id"], status="running")
        with patch.object(self.app.store, "_persist", side_effect=OSError("Disk full")):
            self.app.store.fail_job(job["id"], "Disk write failed.")
        current = self.app.store.get(job["id"])
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["samples"][0]["status"], "failed")
        self.assertIn("memory only", current["warnings"][-1])
        self.assertTrue(self.app.store.warnings)

    def test_csv_is_utf8_and_prevents_spreadsheet_formula_execution(self):
        job = self.run_job({**parameters(), "prompt": " =2+3"})
        self.app.store.rate(job["id"], job["samples"][0]["id"], {"notes": "\t=HYPERLINK(\"bad\")"})
        content = export_csv(self.app.store.get(job["id"]))
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertTrue(rows[0]["prompt"].startswith("'"))
        self.assertTrue(rows[0]["notes"].startswith("'"))
        self.assertEqual(rows[0]["timing_scope"], "end_to_end")
        self.assertEqual(len(rows), 3)

    def test_summary_percentiles_exclude_failed_and_pending_samples(self):
        job = self.run_job(parameters(runs=3, model_ids=["gpt-image-2"]))
        samples = job["samples"]
        samples[0].update(elapsed_ms=10)
        samples[1].update(elapsed_ms=30)
        samples[2].update(status="failed", elapsed_ms=9000)
        summary = summarize(job)["summary"][0]
        self.assertEqual(summary["mean_ms"], 20)
        self.assertEqual(summary["median_ms"], 20)
        self.assertEqual(summary["p95_ms"], 30)
        self.assertEqual(summary["failures"], 1)

    def test_images_are_allowlisted_not_a_general_file_server(self):
        job = self.run_job()
        for filename in ("..", "..\\image-lab.config.json", "image-lab.config.json", "0123456789abcdef.png"):
            with self.subTest(filename=filename), self.assertRaises(KeyError):
                self.app.store.image_path(job["id"], filename)

    def test_legacy_import_only_attaches_provable_latest_images(self):
        directory = self.root / "old-benchmarks"
        directory.mkdir()
        source = directory / "MAI-Image-2.5-run-1.png"
        source.write_bytes(png())
        end = time.time()
        os.utime(source, (end - 10, end - 10))
        row = {
            "model": "MAI-Image-2.5", "run": 1, "success": True, "elapsed_ms": 20000,
            "output_path": str(source), "output_bytes": len(png()), "error": "",
        }
        for stamp in ("20260101-120000", "20260101-130000"):
            report = directory / f"benchmark-results-{stamp}.json"
            write_json_atomic(report, [row])
            os.utime(report, (end, end))
        write_json_atomic(directory / "request-manifest-20260101-130000.json", {
            "prompt": "\u4e2d\u6587\u6d77\u62a5", "width": 1024, "height": 1024,
            "gpt_size": "1024x1024", "gpt_quality": "medium", "models": [],
        })
        self.app.store.import_legacy(self.root, self.app.config)
        history = self.app.store.history()
        self.assertEqual(len(history), 2)
        newest = self.app.store.get(history[0]["id"])
        older = self.app.store.get(history[1]["id"])
        self.assertEqual(newest["prompt"], "\u4e2d\u6587\u6d77\u62a5")
        self.assertEqual(newest["timing_scope"], "legacy_api_response")
        self.assertEqual(newest["models"][0]["version"], "")
        self.assertEqual(newest["summary"][0]["attempted"], 1)
        self.assertTrue(newest["samples"][0]["image_url"])
        self.assertIsNone(older["samples"][0]["image_url"])
        copied = self.app.store.image_path(newest["id"], newest["samples"][0]["filename"])
        source.write_bytes(b"overwritten")
        self.assertEqual(copied.read_bytes(), png())
        self.app.store.import_legacy(self.root, self.app.config)
        self.assertEqual(len(self.app.store.history()), 2)


class HttpTests(WorkbenchFixture):
    def setUp(self):
        super().setUp()
        (self.root / "image-lab.html").write_text("<!doctype html><title>Unit fixture</title>", encoding="utf-8")
        self.server = LocalServer(("127.0.0.1", 0), self.app)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=5)
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        encoded = None if body is None else json.dumps(body)
        try:
            connection.request(method, path, body=encoded, headers=headers or {})
            response = connection.getresponse()
            content = response.read()
            return response.status, content
        finally:
            connection.close()

    def test_http_origin_token_and_private_files(self):
        status, content = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertNotIn(b"unit-token", content)
        csrf = json.loads(content)["csrf_token"]
        headers = {"Content-Type": "application/json", "X-Image-Lab-Token": csrf}
        self.assertEqual(self.request("POST", "/api/jobs", parameters())[0], 403)
        self.assertEqual(self.request("POST", "/api/jobs", parameters(), {
            **headers, "Origin": "https://other-site.example",
        })[0], 403)
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Host": "other-site.example"})[0], 403)
        for path in ("/image-lab.config.json", "/generate.ps1", "/images/../../image-lab.config.json"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_idle_connections_close_and_http_work_is_bounded(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", "/api/health")
            response = connection.getresponse()
            self.assertEqual(response.getheader("Connection"), "close")
            response.read()
        finally:
            connection.close()
        acquired = 0
        try:
            for _ in range(self.server.max_http_workers):
                self.assertTrue(self.server.request_slots.acquire(timeout=5))
                acquired += 1
            status, body = self.request("GET", "/api/health")
            self.assertEqual(status, 503)
            self.assertIn("busy", json.loads(body)["error"])
        finally:
            for _ in range(acquired):
                self.server.request_slots.release()
        self.assertEqual(self.request("GET", "/api/health")[0], 200)

    def test_http_full_local_flow(self):
        csrf = self.app.csrf_token
        headers = {"Content-Type": "application/json", "X-Image-Lab-Token": csrf}
        status, content = self.request("POST", "/api/jobs", parameters(), headers)
        self.assertEqual(status, 202)
        identifier = json.loads(content)["id"]
        self.app.worker.join(timeout=10)
        status, content = self.request("GET", f"/api/jobs/{identifier}")
        job = json.loads(content)
        self.assertEqual(job["progress"]["done"], 3)
        sample = job["samples"][0]
        status, image = self.request("GET", sample["image_url"])
        self.assertEqual((status, image), (200, png()))
        status, content = self.request(
            "PUT", f"/api/jobs/{identifier}/samples/{sample['id']}/rating", {"visual": 5}, headers,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["summary"][0]["rating_mean"], 5)
        self.assertEqual(self.request("GET", f"/api/jobs/{identifier}/export.json")[0], 200)
        self.assertEqual(self.request("GET", f"/api/jobs/{identifier}/export.csv")[0], 200)
        status, content = self.request("GET", f"/api/jobs/{identifier}/export.json?provider=gpt")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["model_ids"], ["gpt-image-2"])
        status, content = self.request("GET", f"/api/jobs/{identifier}/export.csv?provider=flux")
        self.assertEqual(status, 200)
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))
        self.assertEqual([row["model_id"] for row in rows], ["flux-1-kontext-pro"])
        self.assertEqual(self.request("GET", f"/api/jobs/{identifier}/export.json?provider=unknown")[0], 400)
        self.assertEqual(self.request("GET", f"/api/jobs/{identifier}/export.json?provider=gpt&provider=flux")[0], 400)
        status, content = self.request("POST", f"/api/jobs/{identifier}/archive", {}, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["archive"]["image_count"], 3)
        self.assertEqual(self.request("PUT", "/api/config", self.app.config.public(), headers)[0], 200)
        self.assertEqual(self.request("GET", "/")[0], 200)


class LockTests(unittest.TestCase):
    def test_same_data_cannot_be_opened_by_two_servers_and_lock_is_reusable(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / ".server.lock"
            with single_instance(lock):
                with self.assertRaises(AlreadyRunningError):
                    with single_instance(lock):
                        self.fail("A second server acquired an existing lock.")
            with single_instance(lock):
                self.assertTrue(lock.is_file())


class PublicPackageTests(unittest.TestCase):
    def test_first_start_creates_disabled_local_config_without_any_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            example = json.loads((ROOT / "image-lab.config.example.json").read_text(encoding="utf-8"))
            write_json_atomic(root / "image-lab.config.example.json", example)
            local = root / "image-lab.config.json"
            with patch("image_lab.providers.shutil.which", return_value=None), patch.object(Credentials, "headers") as auth:
                app = Application(root, local, root / "results", import_legacy=False)
                self.assertTrue(local.is_file())
                self.assertFalse(any(model["configured"] for model in app.bootstrap()["config"]["models"]))
                self.assertFalse(app.bootstrap()["history"])
                auth.assert_not_called()
                before = local.read_bytes()
                Application(root, local, root / "results", import_legacy=False)
                self.assertEqual(before, local.read_bytes())

    def test_archive_folder_names_are_portable_and_bounded(self):
        for topic in ("../private", "CON", "LPT1.txt", "name:with*invalid?characters", "\U0001f30d" * 80, "x" * 200):
            name = component(topic)
            self.assertNotRegex(name, r'[\\/:*?"<>|]')
            self.assertNotIn("..", name)
            self.assertLessEqual(len(name.encode("utf-16-le")), 80)


class PromptTranslationTests(unittest.TestCase):
    def setUp(self):
        cli = patch("image_lab.providers.shutil.which", return_value="mock-azure-cli")
        cli.start()
        self.addCleanup(cli.stop)
        self.settings = TranslationConfig(
            enabled=True, endpoint="https://example.openai.azure.com/openai/deployments/translator/chat/completions?api-version=2024-10-21",
            deployment="translator",
        )
        self.credentials = Mock()
        self.credentials.headers.return_value = {"Authorization": "Bearer unit-test-translation-token"}

    def test_language_detection_distinguishes_instructions_and_quoted_chinese_text(self):
        self.assertTrue(prompts.needs_english_version("\u4e00\u4e2a\u7ea2\u8272\u7acb\u65b9\u4f53"))
        self.assertTrue(prompts.needs_english_version('"\u6625\u65e5\u8bfb\u4e66\u4f1a"'))
        self.assertFalse(prompts.needs_english_version('A poster with title "\u6625\u65e5\u8bfb\u4e66\u4f1a".'))
        self.assertFalse(prompts.needs_english_version("A reader's hand beside a card titled '\u6625\u65e5\u8bfb\u4e66\u4f1a'."))
        self.assertFalse(prompts.needs_english_version("A red cube."))

    def test_translation_preserves_quoted_display_text(self):
        original = "\u6d77\u62a5\u6807\u9898\u201c\u6625\u65e5\u8bfb\u4e66\u4f1a\u201d"
        english = 'A poster with the title "\u6625\u65e5\u8bfb\u4e66\u4f1a".'
        prompts.validate_translation(original, english)
        with self.assertRaises(ValidationError):
            prompts.validate_translation(original, "A Spring Reading Club poster.")
        with self.assertRaises(ValidationError):
            prompts.validate_english_prompt("\u8fd9\u4ecd\u7136\u662f\u4e2d\u6587\u6307\u4ee4")

    def test_translation_request_is_utf8_json_and_does_not_forward_keys_in_messages(self):
        original = "\u753b\u4e00\u4e2a\u7ea2\u8272\u7acb\u65b9\u4f53"
        payload = {
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"english_prompt": "Draw a red cube."})}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        opener = Mock()
        opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
        with patch("image_lab.prompts.urllib.request.build_opener", return_value=opener):
            result = prompts.translate(original, self.settings, self.credentials, "subscription")
        body = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(body["messages"][1]["content"], original)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertNotIn("unit-test-translation-token", json.dumps(body))
        self.assertEqual(result.english_prompt, "Draw a red cube.")
        self.assertEqual(result.usage["total_tokens"], 30)

    def test_truncated_or_invalid_translation_is_not_accepted(self):
        for payload in (
            {"choices": [{"finish_reason": "length", "message": {"content": '{"english_prompt":"unfinished"}'}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}]},
            {"choices": [{"finish_reason": "stop", "message": {"content": '{"english_prompt":"\\u4e2d\\u6587"}'}}]},
        ):
            opener = Mock()
            opener.open.return_value = io.BytesIO(json.dumps(payload).encode())
            with patch("image_lab.prompts.urllib.request.build_opener", return_value=opener):
                with self.assertRaises(prompts.TranslationError) as error:
                    prompts.translate("\u4e2d\u6587", self.settings, self.credentials, "subscription")
            self.assertTrue(error.exception.request_attempted)

    def test_translation_timeout_is_reported_without_retry(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError("timed out")
        with patch("image_lab.prompts.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(prompts.TranslationError) as error:
                prompts.translate("\u7ea2\u8272", self.settings, self.credentials, "subscription")
        self.assertEqual(opener.open.call_count, 1)
        self.assertTrue(error.exception.request_attempted)

    def test_translator_configuration_rejects_untrusted_hosts_and_wrong_paths(self):
        for change in (
            {"endpoint": "https://untrusted.example/chat/completions"},
            {"endpoint": "https://api.openai.com/v1/chat/completions"},
            {"endpoint": "https://example.openai.azure.com/openai/v1/images/generations"},
            {"deployment": "mismatched"},
            {"api_key": "do-not-store-a-key"},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                TranslationConfig.parse({**self.settings.public(), **change})


if __name__ == "__main__":
    unittest.main()
