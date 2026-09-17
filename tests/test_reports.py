import base64
import copy
import hashlib
import html
import http.client
import json
import threading
from html.parser import HTMLParser
from unittest.mock import patch

from image_lab import reports
from image_lab.providers import ValidationError
from image_lab.server import ConflictError, LocalServer
from image_lab.store import RunStore
from test_editing import EditingFixture
from test_image_lab import WorkbenchFixture, parameters, png


def report_options(**changes):
    return {
        "provider": "all", "include_images": True, "include_notes": True,
        "share_acknowledged": True, **changes,
    }


class Elements(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.elements = []
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ReportTests(WorkbenchFixture):
    def test_unscored_report_is_offline_and_does_not_invent_quality(self):
        job = self.run_job()
        baseline = self.generate.call_count
        content = reports.render_report(self.app.store, job["id"]).decode("utf-8")
        self.assertIn("当前没有人工质量评分", content)
        self.assertIn("未评分 / N/A", content)
        self.assertIn("不是官方认证", content)
        self.assertNotIn('class="score-value"', content)
        self.assertIn("性能可视化", content)
        elements = Elements(content).elements
        embedded = [attrs["src"] for tag, attrs in elements if tag == "img"]
        self.assertEqual(len(embedded), 3)
        for source in embedded:
            self.assertTrue(source.startswith("data:image/png;base64,"))
            self.assertEqual(base64.b64decode(source.split(",", 1)[1]), png())
        for _, attrs in elements:
            for key in ("src", "href"):
                if key in attrs:
                    self.assertFalse(attrs[key].startswith(("http:", "https:", "//", "/")))
        self.assertEqual(self.generate.call_count, baseline)
        self.assertFalse(self.app.store.get(job["id"])["reports"])

    def test_recorded_scores_and_timing_are_not_replaced_by_a_new_judge(self):
        job = self.run_job(parameters(runs=2, model_ids=["mai-image-2-5", "gpt-image-2"]))
        for sample in job["samples"]:
            is_mai = sample["model_id"] == "mai-image-2-5"
            rating = 3 if is_mai else 5
            self.app.store.rate(job["id"], sample["id"], {"adherence": rating, "visual": 4})
            self.app.store.update_sample(
                job["id"], sample["id"], elapsed_ms=(1000 if is_mai else 2000) * sample["round"],
            )
        content = reports.render_report(self.app.store, job["id"]).decode("utf-8")
        self.assertIn("1.50 s", content)
        self.assertIn("3.00 s", content)
        self.assertIn('class="score-value">5.00', content)
        self.assertIn('style="width:100.00%"', content)
        self.assertIn("指令遵循：当前已评均值最高为 GPT Image 2", content)
        self.assertIn("文字准确性：评分覆盖不足", content)
        self.assertIn("每模型 2 张已评", content)
        self.assertIn("未评分 / N/A", content)
        self.assertEqual(self.generate.call_count, 4)

    def test_equal_counts_on_different_rounds_do_not_produce_quality_leaders(self):
        job = self.run_job(parameters(runs=2, model_ids=["mai-image-2-5", "gpt-image-2"]))
        for sample in job["samples"]:
            if (sample["model_id"], sample["round"]) in (("mai-image-2-5", 1), ("gpt-image-2", 2)):
                self.app.store.rate(job["id"], sample["id"], {"adherence": 5})
        content = reports.render_report(self.app.store, job["id"], include_images=False).decode("utf-8")
        self.assertIn("各模型已评轮次不一致", content)
        self.assertNotIn("指令遵循：当前已评均值最高", content)

    def test_failed_sample_stays_in_denominator_but_not_successful_mean(self):
        job = self.run_job(parameters(runs=2, model_ids=["gpt-image-2"]))
        self.app.store.update_sample(job["id"], job["samples"][0]["id"], elapsed_ms=1000)
        self.app.store.update_sample(
            job["id"], job["samples"][1]["id"], status="failed", elapsed_ms=99000,
            error="HTTP 429: personal-resource connection data", filename=None, image_url=None,
        )
        content = reports.render_report(self.app.store, job["id"]).decode("utf-8")
        self.assertIn("<strong>1 / 2</strong>", content)
        self.assertIn("1.00 s", content)
        self.assertIn("HTTP 429", content)
        self.assertNotIn("personal-resource", content)
        self.assertIn("1 条失败 / 中断", content)

    def test_family_report_contains_only_that_familys_samples_and_scores(self):
        job = self.run_job()
        content = reports.render_report(self.app.store, job["id"], "gpt").decode("utf-8")
        self.assertIn("GPT Image 2", content)
        self.assertNotIn("MAI Image 2.5", content)
        self.assertNotIn("FLUX.1 Kontext Pro", content)
        self.assertEqual(content.count('<article class="sample">'), 1)
        self.assertIn("1 / 3 个原始参与模型", content)
        self.assertEqual(len(self.app.store.get(job["id"])["samples"]), 3)
        for provider in ("", "invalid", None, True):
            with self.subTest(provider=provider), self.assertRaises(ValidationError):
                reports.render_report(self.app.store, job["id"], provider)

    def test_prompts_names_and_notes_are_escaped_and_connection_fields_are_omitted(self):
        prompt = '<img src=x onerror="window.hacked=1"> a $literal prompt'
        job = self.run_job({**parameters(), "prompt": prompt})
        models = copy.deepcopy(job["models"])
        models[0].update(
            name='<script>window.hacked=2</script>',
            endpoint="https://secret-resource.example.test/private",
            deployment="private-deployment-alias",
            api_key="test-private-key",
        )
        self.app.store.update_job(job["id"], models=models, topic='<svg onload="bad()">Report</svg>')
        self.app.store.rate(job["id"], job["samples"][0]["id"], {
            "visual": 4, "notes": '<a href="https://evil.example">private-review</a>',
        })
        content = reports.render_report(self.app.store, job["id"]).decode("utf-8")
        self.assertIn(html.escape(prompt, quote=True), content)
        self.assertIn("&lt;script&gt;window.hacked=2&lt;/script&gt;", content)
        self.assertIn("&lt;a href=", content)
        for private in ("secret-resource", "private-deployment-alias", "test-private-key", str(self.root)):
            self.assertNotIn(private, content)
        for tag, attrs in Elements(content).elements:
            self.assertNotEqual(tag, "svg")
            self.assertNotIn("onerror", attrs)
            self.assertNotIn("onload", attrs)
        redacted = reports.render_report(
            self.app.store, job["id"], include_images=False, include_notes=False,
        ).decode("utf-8")
        self.assertNotIn("private-review", redacted)
        self.assertNotIn("data:image/png;base64,", redacted)
        self.assertIn('class="score-value">4.00', redacted)

    def test_image_budget_is_exact_and_has_an_explicit_no_image_alternative(self):
        job = self.run_job()
        with patch("image_lab.reports.MAX_EMBEDDED_BYTES", len(png()) * 3):
            report = self.app.export_report(job["id"], report_options())
            self.assertTrue(report["id"])
        with patch("image_lab.reports.MAX_EMBEDDED_BYTES", len(png()) * 3 - 1):
            with self.assertRaisesRegex(ValidationError, "32 MiB"):
                self.app.export_report(job["id"], report_options())
            no_images = self.app.export_report(job["id"], report_options(include_images=False))
        self.assertFalse(no_images["include_images"])
        self.assertEqual(len(self.app.store.get(job["id"])["reports"]), 2)

    def test_missing_and_modified_images_are_visible_not_silently_replaced(self):
        job = self.run_job()
        path = self.app.store.image_path(job["id"], job["samples"][0]["filename"])
        path.unlink()
        path = self.app.store.image_path(job["id"], job["samples"][1]["filename"])
        path.write_bytes(b"wrong-data")
        content = reports.render_report(self.app.store, job["id"]).decode("utf-8")
        self.assertIn("图片文件缺失", content)
        self.assertIn("图片与记录不一致", content)
        self.assertIn("图片完整性说明", content)
        self.assertEqual(content.count('<img src="data:'), 1)
        self.assertIn("<strong>3 / 3</strong>", content)

    def test_saved_report_is_immutable_and_persists_across_restarts(self):
        job = self.run_job()
        report = self.app.export_report(job["id"], report_options())
        content = self.app.store.report_content(job["id"], report["id"])
        self.assertEqual(report["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(report["source_updated_at"], job["updated_at"])
        self.app.store.rate(job["id"], job["samples"][0]["id"], {"visual": 5})
        self.assertEqual(self.app.store.report_content(job["id"], report["id"]), content)
        newer = self.app.export_report(job["id"], report_options())
        self.assertNotEqual(newer["id"], report["id"])
        self.assertNotEqual(newer["sha256"], report["sha256"])
        restored = RunStore(self.app.store.output)
        self.assertEqual(restored.report_content(job["id"], report["id"]), content)
        self.assertEqual(restored.history()[0]["report_count"], 2)
        self.assertEqual(self.generate.call_count, 3)

    def test_saved_report_paths_and_checksums_are_enforced(self):
        job = self.run_job()
        report = self.app.export_report(job["id"], report_options())
        for report_id in ("../file", report["id"] + ".html", "a" * 32):
            with self.subTest(report_id=report_id), self.assertRaises(KeyError):
                self.app.store.report_content(job["id"], report_id)
        another = self.run_job(parameters(model_ids=["gpt-image-2"]))
        with self.assertRaises(KeyError):
            self.app.store.report_content(another["id"], report["id"])
        path = self.app.store.output / job["id"] / "reports" / f"{report['id']}.html"
        content = path.read_bytes()
        path.write_bytes(b"x" + content[1:])
        with self.assertRaisesRegex(ValidationError, "checksum"):
            self.app.store.report_content(job["id"], report["id"])

    def test_invalid_options_or_active_jobs_do_not_save_success_shaped_reports(self):
        job = self.run_job()
        for options in (
            {}, report_options(share_acknowledged=False),
            report_options(include_images=1), report_options(include_notes="false"),
        ):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.app.export_report(job["id"], options)
        self.app.store.update_job(job["id"], status="running")
        with self.assertRaises(ValidationError):
            self.app.export_report(job["id"], report_options())
        self.assertFalse(self.app.store.get(job["id"])["reports"])
        self.assertFalse(self.app.report_lock.locked())
        self.app.store.update_job(job["id"], status="completed")

    def test_report_generation_is_bounded_to_one_request(self):
        job = self.run_job()
        self.app.report_lock.acquire()
        try:
            with self.assertRaises(ConflictError):
                self.app.export_report(job["id"], report_options())
        finally:
            self.app.report_lock.release()
        self.assertFalse(self.app.store.get(job["id"])["reports"])

    def test_metadata_write_failure_does_not_publish_an_orphan_report(self):
        job = self.run_job()
        with patch.object(self.app.store, "_persist", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.app.export_report(job["id"], report_options())
        self.assertFalse(self.app.store.get(job["id"])["reports"])
        self.assertEqual(list((self.app.store.output / job["id"] / "reports").glob("*.html")), [])
        self.assertFalse(self.app.report_lock.locked())

    def test_pinning_retains_original_sample_data_and_survives_restart(self):
        job = self.run_job()
        pinned = self.app.store.pin(job["id"], True)
        self.assertTrue(pinned["pinned"])
        self.assertEqual(pinned["samples"], job["samples"])
        self.assertEqual(pinned["created_at"], job["created_at"])
        self.assertTrue(RunStore(self.app.store.output).get(job["id"])["pinned"])
        self.assertTrue(self.app.store.history()[0]["pinned"])
        self.app.store.pin(job["id"], False)
        self.assertFalse(RunStore(self.app.store.output).get(job["id"])["pinned"])
        with self.assertRaises(ValidationError):
            self.app.store.pin(job["id"], "true")

    def test_family_thumbnails_use_the_corresponding_model_and_survive_missing_ratings(self):
        job = self.run_job()
        summary = self.app.store.history()[0]
        for model in job["models"]:
            sample = next(row for row in job["samples"] if row["model_id"] == model["id"])
            self.assertEqual(summary["preview_images"][model["provider"]], sample["image_url"])
        self.assertEqual(summary["ratings_count"], 0)
        self.assertEqual(summary["report_count"], 0)

    def test_filtered_legacy_unknown_sizes_are_not_called_mismatched_sizes(self):
        job = self.run_job()
        self.app.store.update_job(job["id"], source="legacy", size="unknown", timing_scope="legacy_api_response", prompt="")
        content = reports.render_report(self.app.store, job["id"]).decode("utf-8")
        self.assertIn("原记录未保存提示词", content)
        self.assertIn("旧脚本计时", content)
        self.assertNotIn("张输出的实际尺寸与目标尺寸不同", content)


class EditingReportTests(EditingFixture):
    def test_edit_report_includes_shared_source_and_separate_human_dimensions(self):
        job = self.finish(self.payload(ids=["mai-image-2-5", "mai-image-2-6"]))
        self.app.store.rate(job["id"], job["samples"][0]["id"], {"adherence": 4, "preservation": 5})
        content = reports.render_report(self.app.store, job["id"], "mai").decode("utf-8")
        self.assertEqual(content.count('<img src="data:'), 3)
        self.assertIn(job["reference_image"]["sha256"], content)
        self.assertIn("未改内容保持", content)
        self.assertIn("编辑任务不合成总分", content)
        self.assertIn("未评分 / N/A", content)
        self.generate.assert_not_called()

    def test_report_does_not_unlock_a_filtered_failed_edit(self):
        job = self.finish(self.payload(ids=["mai-image-2-5", "gpt-image-2"]))
        self.app.store.update_sample(
            job["id"], job["samples"][1]["id"], status="failed", error="HTTP 500: private diagnostics",
        )
        content = reports.render_report(self.app.store, job["id"], "mai").decode("utf-8")
        self.assertIn("原始编辑实验尚未全部成功", content)
        self.assertIn("编辑评分未开放", content)
        self.assertNotIn("private diagnostics", content)
        self.assertNotIn("GPT Image 2", content)


class ReportHttpTests(WorkbenchFixture):
    def test_report_creation_download_and_pin_are_local_authenticated_operations(self):
        job = self.run_job()
        server = LocalServer(("127.0.0.1", 0), self.app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(method, path, payload=None, token=True):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["X-Image-Lab-Token"] = self.app.csrf_token
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            try:
                connection.request(method, path, body=json.dumps(payload) if payload is not None else None, headers=headers)
                response = connection.getresponse()
                return response.status, dict(response.getheaders()), response.read()
            finally:
                connection.close()
        try:
            self.assertEqual(request("POST", f"/api/jobs/{job['id']}/report", report_options(), token=False)[0], 403)
            self.assertEqual(request("PUT", f"/api/jobs/{job['id']}/pin", {"pinned": True}, token=False)[0], 403)
            status, _, body = request("PUT", f"/api/jobs/{job['id']}/pin", {"pinned": True})
            self.assertEqual(status, 200)
            self.assertTrue(json.loads(body)["pinned"])
            status, _, body = request("POST", f"/api/jobs/{job['id']}/report", report_options(provider="gpt"))
            self.assertEqual(status, 201)
            report = json.loads(body)
            status, headers, document = request("GET", report["download_url"], token=False)
            self.assertEqual(status, 200)
            self.assertIn("attachment;", headers["Content-Disposition"])
            self.assertIn(b"GPT Image 2", document)
            self.assertNotIn(b"MAI Image 2.5", document)
            self.assertEqual(request("GET", f"/api/jobs/{job['id']}/reports/../../image-lab.config.json")[0], 404)
            self.assertEqual(self.app.store.history()[0]["report_count"], 1)
            self.assertEqual(self.generate.call_count, 3)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
