"""Opt-in offline-report, saved-result, and accessible-tooltip UI regression."""

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from image_lab.server import Application, LocalServer
from image_lab.store import RunStore, write_json_atomic
from test_image_lab import config_data, generated_image, parameters


def run(channel):
    with tempfile.TemporaryDirectory(prefix="image-lab-report-ui-") as temporary, ExitStack() as mocks:
        root = Path(temporary)
        shutil.copyfile(ROOT / "image-lab.html", root / "image-lab.html")
        write_json_atomic(root / "image-lab.config.json", config_data())
        mocks.enter_context(patch("image_lab.providers.shutil.which", return_value="mock-azure-cli"))
        images = mocks.enter_context(patch("image_lab.server.providers.generate", return_value=generated_image()))
        translator = mocks.enter_context(patch("image_lab.server.prompts.translate", side_effect=AssertionError("No text model calls")))
        app = Application(root, root / "image-lab.config.json", root / "results", import_legacy=False)
        mocks.enter_context(patch.object(app.credentials, "headers", return_value={}))
        jobs = []
        for index in range(6):
            ids = ["mai-image-2-5", "gpt-image-2"] if index == 5 else ["gpt-image-2"]
            job = app.create_job({
                **parameters(runs=2, model_ids=ids), "topic": f"Browser fixture {index}",
                "prompt": 'A temporary poster titled "HELLO" <script>window.bad=1</script>.',
            })
            app.worker.join(timeout=10)
            assert not app.worker.is_alive()
            jobs.append(app.store.get(job["id"]))
        latest = jobs[-1]
        app.store.rate(latest["id"], latest["samples"][0]["id"], {"notes": "PRIVATE NOTE: temporary fixture only"})
        before_calls = images.call_count
        server = LocalServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel=channel, headless=True)
                try:
                    context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True, has_touch=True)
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base + "/?operation=generate&family=gpt", wait_until="networkidle")
                    expect(page.locator(".recent-card")).to_have_count(4)
                    expect(page.locator("#recent-section")).to_be_visible()
                    expected_gpt_image = next(sample["image_url"] for sample in latest["samples"] if sample["model_id"] == "gpt-image-2")
                    expect(page.locator(f'[data-recent-open="{latest["id"]}"] img')).to_have_attribute("src", expected_gpt_image)

                    info = page.locator('[data-info="topic-help"]')
                    expect(page.locator("#topic-help")).to_be_hidden()
                    info.hover()
                    expect(page.locator("#info-popover")).to_be_visible()
                    expect(page.locator("#info-popover")).to_contain_text("归档")
                    page.locator("#info-popover").hover()
                    expect(page.locator("#info-popover")).to_be_visible()
                    page.keyboard.press("Escape")
                    expect(page.locator("#info-popover")).to_be_hidden()
                    info.focus()
                    expect(page.locator("#info-popover")).to_be_visible()
                    page.keyboard.press("Escape")
                    expect(page.locator("#info-popover")).to_be_hidden()
                    page.locator("#topic").focus()
                    info.click()
                    expect(page.locator("#info-popover")).to_be_visible()
                    page.locator("h1").click()
                    expect(page.locator("#info-popover")).to_be_hidden()

                    page.locator("#history-button").click()
                    expect(page.locator(".history-row")).to_have_count(6)
                    page.locator(f'[data-open="{jobs[0]["id"]}"]').click()
                    page.locator("#round").select_option("2")
                    page.locator("#pin-button").click()
                    expect(page.locator("#pin-button")).to_have_attribute("aria-pressed", "true")
                    expect(page.locator(".recent-open").first).to_have_attribute("data-recent-open", jobs[0]["id"])
                    page.locator("#pinned-only").click()
                    expect(page.locator(".recent-card")).to_have_count(1)
                    assert RunStore(app.store.output).get(jobs[0]["id"])["pinned"]
                    saved_view = json.loads(page.evaluate("localStorage.getItem('image-lab:last-view:v1')"))
                    assert set(saved_view) == {"job_id", "operation", "family", "round"}
                    assert saved_view["job_id"] == jobs[0]["id"] and saved_view["round"] == 2
                    page.close()

                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base, wait_until="networkidle")
                    expect(page.locator("#round")).to_have_value("2")
                    expect(page.locator("#pin-button")).to_have_attribute("aria-pressed", "true")
                    assert f"job={jobs[0]['id']}" in page.url
                    expect(page.locator('[data-family="gpt"]')).to_have_attribute("aria-pressed", "true")

                    page.goto(base + "/?operation=generate&family=mai", wait_until="networkidle")
                    expect(page.locator("#family-context")).to_contain_text("1 / 2")
                    assert f"job={latest['id']}" in page.url
                    page.locator('[data-family="all"]').click()
                    page.locator("#blind-button").click()
                    expect(page.locator("#report-button")).to_be_disabled()
                    page.locator("#blind-button").click()
                    page.locator("#report-button").click()
                    expect(page.locator("#report-dialog")).to_be_visible()
                    expect(page.locator("#report-scope")).to_contain_text("2 个模型")
                    page.locator("#export-report").click()
                    assert not app.store.get(latest["id"])["reports"]
                    page.locator("#report-consent").check()
                    with page.expect_download() as download_event:
                        page.locator("#export-report").click()
                    download = download_event.value
                    report_path = root / "offline-report.html"
                    download.save_as(report_path)
                    expect(page.locator("#report-list a")).to_have_count(1)
                    expect(page.locator("#report-status")).to_contain_text("报告快照已保存")
                    report = app.store.get(latest["id"])["reports"][0]
                    assert hashlib.sha256(report_path.read_bytes()).hexdigest() == report["sha256"]
                    assert images.call_count == before_calls

                    offline = browser.new_context(offline=True, viewport={"width": 1100, "height": 900})
                    try:
                        report_page = offline.new_page()
                        external_requests = []
                        report_page.on("request", lambda request: external_requests.append(request.url) if request.url.startswith(("http:", "https:")) else None)
                        report_page.on("pageerror", lambda error: errors.append(str(error)))
                        report_page.goto(report_path.as_uri(), wait_until="load")
                        expect(report_page.locator("h1")).to_have_text("Browser fixture 5")
                        expect(report_page.locator("body")).to_contain_text("当前没有人工质量评分")
                        expect(report_page.locator("body")).to_contain_text("PRIVATE NOTE: temporary fixture only")
                        assert report_page.evaluate("window.bad") is None
                        expect(report_page.locator("article.sample")).to_have_count(4)
                        for image in report_page.locator("img").all():
                            expect(image).to_have_js_property("naturalWidth", 2)
                        assert not external_requests
                        report_page.locator("#theme-button").click()
                        pdf_path = root / "offline-report.pdf"
                        report_page.pdf(path=str(pdf_path), print_background=True, format="A4")
                        assert pdf_path.read_bytes().startswith(b"%PDF-")
                        report_page.set_viewport_size({"width": 390, "height": 844})
                        assert report_page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    finally:
                        offline.close()

                    page.locator('[data-close="report-dialog"]').first.click()
                    page.locator('[data-family="gpt"]').click()
                    page.locator("#report-button").click()
                    page.locator("#report-images").uncheck()
                    page.locator("#report-notes").uncheck()
                    page.locator("#report-consent").check()
                    with page.expect_download() as download_event:
                        page.locator("#export-report").click()
                    bare_path = root / "no-images-report.html"
                    download_event.value.save_as(bare_path)
                    bare = bare_path.read_text(encoding="utf-8")
                    assert "PRIVATE NOTE" not in bare and "data:image/" not in bare
                    assert "GPT Image 2" in bare and "MAI Image 2.5" not in bare
                    expect(page.locator("#report-list a")).to_have_count(2)
                    assert RunStore(app.store.output).history()[0]["report_count"] == 2

                    page.locator('[data-close="report-dialog"]').first.click()
                    page.reload(wait_until="networkidle")
                    page.locator("#report-button").click()
                    expect(page.locator("#report-list a")).to_have_count(2)
                    expect(page.locator("#report-consent")).not_to_be_checked()
                    page.locator('[data-close="report-dialog"]').first.click()
                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(page.locator("#recent-section")).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.locator('[data-info="saved-help"]').tap()
                    expect(page.locator("#info-popover")).to_be_visible()
                    tooltip = page.locator("#info-popover").bounding_box()
                    assert tooltip["x"] >= 0 and tooltip["x"] + tooltip["width"] <= 390
                    page.keyboard.press("Escape")
                    expect(page.locator("#info-popover")).to_be_hidden()
                    assert images.call_count == before_calls
                    translator.assert_not_called()
                    assert not errors, errors
                    context.close()
                finally:
                    browser.close()
        finally:
            app.shutdown()
            server.shutdown()
            server.server_close()
            thread.join(timeout=10)
    print("Passed: immutable offline report downloads, real score provenance, printing, and family-scoped content.")
    print("Passed: pin persistence, restored last view, family thumbnails, keyboard/hover/tap tooltips, mobile layout.")
    print("No cloud model requests or changes to real experiment data were made.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="msedge")
    run(parser.parse_args().channel)
