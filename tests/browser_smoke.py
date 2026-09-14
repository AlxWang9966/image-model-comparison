"""Opt-in UI regression against a temporary backend with mocked image providers."""

from __future__ import annotations

import argparse
import csv
import io
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
from image_lab.store import write_json_atomic
from test_image_lab import config_data, generated_image, parameters


GPT_IDS = ["gpt-image-2", "gpt-image-2-5", "gpt-image-2-5-sunburst"]
FLUX_IDS = ["flux-1-kontext-pro", "flux-2-pro", "flux-2-flex"]


def run(channel: str) -> None:
    with tempfile.TemporaryDirectory(prefix="image-lab-ui-") as temporary, ExitStack() as mocks:
        root = Path(temporary)
        shutil.copyfile(ROOT / "image-lab.html", root / "image-lab.html")
        config = config_data()
        flare = next(model for model in config["models"] if model["id"] == "gpt-image-2-5")
        flare.update(
            enabled=True,
            endpoint="https://example.openai.azure.com/openai/deployments/gpt-image-2.5-flare/images/generations?api-version=2025-04-01-preview",
        )
        write_json_atomic(root / "image-lab.config.json", config)
        mocks.enter_context(patch("image_lab.providers.shutil.which", return_value="mock-azure-cli"))
        image_calls = mocks.enter_context(patch("image_lab.server.providers.generate", return_value=generated_image()))
        translator = mocks.enter_context(patch("image_lab.server.prompts.translate", side_effect=AssertionError("Unexpected translation")))
        app = Application(root, root / "image-lab.config.json", root / "results", import_legacy=False)
        mocks.enter_context(patch.object(app.credentials, "headers", return_value={"Authorization": "Bearer unit-fixture"}))

        def generate_history(ids: list[str]) -> dict:
            job = app.create_job(parameters(model_ids=ids))
            app.worker.join(timeout=10)
            assert not app.worker.is_alive()
            return app.store.get(job["id"])

        gpt_job = generate_history(GPT_IDS)
        all_ids = [model["id"] for model in config["models"]]
        mixed_job = generate_history(all_ids)
        timings = {
            "mai-image-2-5": 100,
            "gpt-image-2": 300,
            "gpt-image-2-5": 200,
            "gpt-image-2-5-sunburst": 250,
            "flux-1-kontext-pro": 50,
            "flux-2-pro": 60,
            "flux-2-flex": 70,
        }
        for sample in mixed_job["samples"]:
            app.store.update_sample(mixed_job["id"], sample["id"], elapsed_ms=timings[sample["model_id"]])
        app.sync_archive(mixed_job["id"])
        baseline_calls = image_calls.call_count
        server = LocalServer(("127.0.0.1", 0), app)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        release = threading.Event()

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel=channel, headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.add_init_script("""
                        Object.defineProperty(navigator, "clipboard", {
                            value: {writeText: async value => {window.copiedArchivePath = value;}}
                        });
                    """)
                    page.goto(base, wait_until="networkidle")
                    expect(page.locator("#ready-count")).to_have_text("7 / 7")
                    expect(page.locator(".result-card")).to_have_count(7)
                    assert page.locator("#advanced-options").get_attribute("open") is None
                    expect(page.locator("#runs")).to_be_hidden()
                    expect(page.locator("#error-banner")).to_be_hidden()

                    def select_family(family: str) -> None:
                        page.locator(f'[data-family="{family}"]').click()
                        expect(page.locator(f'[data-family="{family}"]')).to_have_attribute("aria-pressed", "true")

                    def selected_ids() -> list[str]:
                        return page.locator("#model-list input:checked").evaluate_all(
                            "inputs => inputs.map(input => input.dataset.model)"
                        )

                    select_family("gpt")
                    assert selected_ids() == GPT_IDS
                    expect(page.locator(".result-card")).to_have_count(3)
                    expect(page.locator("#summary-table tbody tr")).to_have_count(3)
                    expect(page.locator(".result-card.fastest h3")).to_have_text("GPT Image 2.5 Flare")
                    expect(page.locator("#family-context")).to_contain_text("3 / 7")
                    for image in page.locator(".image-button img").all():
                        expect(image).to_have_js_property("naturalWidth", 2)

                    exported = page.request.get(base + page.locator("#json-export").get_attribute("href"))
                    assert exported.ok and exported.json()["model_ids"] == GPT_IDS
                    exported_csv = page.request.get(base + page.locator("#csv-export").get_attribute("href"))
                    csv_rows = list(csv.DictReader(io.StringIO(exported_csv.body().decode("utf-8-sig"))))
                    assert [row["model_id"] for row in csv_rows] == GPT_IDS
                    assert len(app.store.get(mixed_job["id"])["samples"]) == 7

                    page.locator("#archive-copy").click()
                    expect(page.locator("#toast")).to_contain_text("已复制")
                    assert page.evaluate("window.copiedArchivePath").startswith(".\\image-lab-archive\\")
                    page.locator("#archive-refresh").click()
                    expect(page.locator("#toast")).to_contain_text("没有发送生图请求")
                    assert image_calls.call_count == baseline_calls

                    page.locator("#blind-button").click()
                    assert page.locator(".card-name h3").all_text_contents() == ["图像 A", "图像 B", "图像 C"]
                    expect(page.locator("#run-details")).to_be_hidden()
                    expect(page.locator("#archive-note")).to_be_hidden()
                    assert page.locator("#json-export").get_attribute("href") is None
                    page.locator(".image-button").first.click()
                    expect(page.locator("#zoom-title")).to_contain_text("图像 A")
                    page.locator("#zoom-actual").click()
                    expect(page.locator("#zoom-value")).to_have_text("100%")
                    page.locator('[data-close="zoom-dialog"]').click()
                    sample_id = page.locator('[data-action="rate"]').first.get_attribute("data-sample")
                    page.locator('[data-action="rate"]').first.click()
                    page.locator("#rating-visual").select_option("4")
                    page.locator("#rating-notes").fill("Temporary UI fixture, not a real review.")
                    page.locator("#save-rating").click()
                    expect(page.locator("#rating-dialog")).not_to_be_visible()
                    stored = next(sample for sample in app.store.get(mixed_job["id"])["samples"] if sample["id"] == sample_id)
                    assert stored["rating"]["visual"] == 4 and stored["rating"]["text"] is None
                    page.locator("#blind-button").click()

                    page.locator("#history-button").click()
                    expect(page.locator(".history-row")).to_have_count(2)
                    page.locator(f'[data-open="{gpt_job["id"]}"]').click()
                    expect(page.locator("#json-export")).to_have_attribute(
                        "href", f'/api/jobs/{gpt_job["id"]}/export.json?provider=gpt',
                    )
                    select_family("flux")
                    assert selected_ids() == FLUX_IDS
                    expect(page.locator(".result-card")).to_have_count(0)
                    expect(page.locator("#summary-table")).to_be_empty()
                    expect(page.locator("#result-grid")).to_contain_text("没有FLUX 系列")
                    page.locator('[data-action="history"]').click()
                    expect(page.locator(".history-row")).to_have_count(1)
                    page.locator(f'[data-open="{mixed_job["id"]}"]').click()
                    expect(page.locator(".result-card")).to_have_count(3)
                    expect(page.locator(".result-card.fastest h3")).to_have_text("FLUX.1 Kontext Pro")
                    select_family("mai")
                    expect(page.locator(".result-card")).to_have_count(1)
                    expect(page.locator("#result-grid")).to_have_class("result-grid single")

                    select_family("gpt")
                    page.locator("#reuse-button").click()
                    assert selected_ids() == GPT_IDS
                    page.locator('input[data-model="gpt-image-2"]').uncheck()
                    expect(page.locator("#request-count")).to_contain_text("2 次生图请求")
                    expect(page.locator(".result-card")).to_have_count(3)
                    page.locator("#new-button").click()
                    page.locator("#start-top-button").click()
                    expect(page.locator("#job-status")).to_have_text("已完成", timeout=15000)
                    expect(page.locator("#cancel-button")).to_be_hidden(timeout=15000)
                    assert image_calls.call_count == baseline_calls + 2
                    assert set(app.store.get(app.store.history()[0]["id"])["model_ids"]) == set(GPT_IDS[1:])

                    select_family("gpt")
                    started = threading.Event()

                    def blocking_image(*args):
                        started.set()
                        if not release.wait(30):
                            raise RuntimeError("UI test did not release the mocked provider")
                        return generated_image()

                    image_calls.side_effect = blocking_image
                    page.locator("#start-top-button").click()
                    expect(page.locator("#cancel-button")).to_be_visible()
                    assert started.wait(5)
                    active_id = app.active_job_id
                    select_family("flux")
                    expect(page.locator(".result-card")).to_have_count(0)
                    expect(page.locator("#start-button")).to_be_disabled()
                    assert app.store.get(active_id)["model_ids"] == GPT_IDS
                    assert not app.store.get(active_id)["cancel_requested"]
                    release.set()
                    expect(page.locator("#cancel-button")).to_be_hidden(timeout=15000)
                    assert image_calls.call_count == baseline_calls + 5
                    assert selected_ids() == FLUX_IDS

                    select_family("gpt")
                    page.locator("#advanced-options > summary").click()
                    page.locator("#runs").fill("11")
                    page.locator("#advanced-options > summary").click()
                    before_invalid = image_calls.call_count
                    page.locator("#start-top-button").click()
                    expect(page.locator("#runs")).to_be_visible()
                    assert image_calls.call_count == before_invalid
                    page.locator("#runs").fill("1")
                    page.locator("#advanced-options > summary").click()

                    page.goto(base + "/?family=gpt&clawpilotTheme=dark", wait_until="networkidle")
                    expect(page.locator(".result-card")).to_have_count(3)
                    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert page.locator("#archive-note").bounding_box()["height"] < 360
                    assert page.locator("#archive-path").bounding_box()["width"] > 250
                    expect(page.locator("#family-bar")).to_be_visible()
                    expect(page.locator("#runs")).to_be_hidden()
                    assert selected_ids() == GPT_IDS
                    assert not errors, errors
                    translator.assert_not_called()
                finally:
                    browser.close()
        finally:
            release.set()
            app.shutdown()
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=10)
    print("Passed: model-family views, matching exports, archive actions, blind review, and mobile layout.")
    print("Passed: hidden groups never generate; changing views preserves active experiments.")
    print("All calls used temporary data and mocked image providers. No Azure/OpenAI requests were sent.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="msedge", help="Installed Playwright browser channel (default: msedge).")
    run(parser.parse_args().channel)
