"""Opt-in single-image editing UI checks using private temporary files and mock providers."""

import argparse
import io
import json
import shutil
import sys
import tempfile
import threading
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from image_lab.providers import GeneratedImage
from image_lab.server import Application, LocalServer
from image_lab.store import write_json_atomic
from test_image_lab import config_data, generated_image, parameters


def run(channel):
    source = Image.new("RGB", (128, 128), (230, 180, 50))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "private browser fixture")
    uploaded = io.BytesIO()
    source.save(uploaded, "PNG", pnginfo=metadata)
    edited = Image.new("RGB", (128, 128), (50, 180, 70))
    output = io.BytesIO()
    edited.save(output, "PNG")
    output_image = GeneratedImage(output.getvalue(), "png", 128, 128, 10, 0, None)
    release = threading.Event()
    with tempfile.TemporaryDirectory(prefix="image-lab-edit-ui-") as temporary, ExitStack() as mocks:
        root = Path(temporary)
        shutil.copyfile(ROOT / "image-lab.html", root / "image-lab.html")
        config = config_data()
        model_count = len(config["models"])
        flare = next(model for model in config["models"] if model["id"] == "gpt-image-2-5")
        flare.update(enabled=True, endpoint="https://example.openai.azure.com/openai/v1/images/generations")
        write_json_atomic(root / "image-lab.config.json", config)
        mocks.enter_context(patch("image_lab.providers.shutil.which", return_value="mock-azure-cli"))
        generation = mocks.enter_context(patch("image_lab.server.providers.generate", return_value=generated_image()))
        translation = mocks.enter_context(patch("image_lab.server.prompts.translate", side_effect=AssertionError("No translation expected")))
        app = Application(root, root / "image-lab.config.json", root / "results", import_legacy=False)
        mocks.enter_context(patch.object(app.credentials, "headers", return_value={}))
        text_job = app.create_job(parameters(model_ids=["gpt-image-2"]))
        app.worker.join(10)
        received_sources = []

        def edit(model, body, headers, timeout, reference):
            received_sources.append(reference)
            if model.id == "flux-2-flex" and not release.wait(30):
                raise RuntimeError("Editing UI test did not release its mock")
            return output_image

        edits = mocks.enter_context(patch("image_lab.server.providers.edit", side_effect=edit))
        server = LocalServer(("127.0.0.1", 0), app)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel=channel, headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1050})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base, wait_until="networkidle")
                    expect(page.locator('[data-operation="generate"]')).to_have_attribute("aria-pressed", "true")
                    page.locator('[data-operation="edit"]').click()
                    expect(page.locator("#reference-upload")).to_be_visible()
                    expect(page.locator("#start-top-button")).to_be_disabled()
                    page.locator('[data-family="all"]').click()
                    expect(page.locator("#model-list input:checked")).to_have_count(model_count)
                    page.locator("#reference-file").set_input_files({
                        "name": "customer-private-filename.png",
                        "mimeType": "image/png",
                        "buffer": uploaded.getvalue(),
                    })
                    expect(page.locator("#reference-draft")).to_be_visible()
                    expect(page.locator("#reference-thumb")).to_have_js_property("naturalWidth", 128)
                    expect(page.locator("#start-top-button")).to_be_disabled()
                    assert len(app.store.history()) == 1
                    page.locator("#reference-consent").check()
                    page.locator("#prompt").fill("Change the object from yellow to green, preserving everything else.")
                    expect(page.locator("#start-top-button")).to_be_enabled()
                    page.locator("#start-top-button").click()
                    expect(page.locator("#reference-result")).to_be_visible()
                    expect(page.locator("#reference-result-image")).to_have_js_property("naturalWidth", 128)
                    expect(page.locator("#editing-status")).to_contain_text("待整轮所有模型")
                    page.locator('[data-family="gpt"]').click()
                    expect(page.locator(".image-button")).to_have_count(3)
                    for button in page.locator('[data-action="rate"]').all():
                        expect(button).to_be_disabled()
                    edit_job_id = app.active_job_id
                    current = app.store.get(edit_job_id)
                    sample = next(sample for sample in current["samples"] if sample["status"] == "success")
                    response = page.request.put(
                        base + f"/api/jobs/{edit_job_id}/samples/{sample['id']}/rating",
                        headers={"X-Image-Lab-Token": app.csrf_token},
                        data={"adherence": 5},
                    )
                    assert response.status == 400
                    release.set()
                    expect(page.locator("#cancel-button")).to_be_hidden(timeout=15000)
                    expect(page.locator("#editing-status")).to_contain_text("所有参与模型与轮次已成功")
                    expect(page.locator("#step-review")).to_have_class("current")
                    assert edits.call_count == model_count and generation.call_count == 1
                    assert len(set(received_sources)) == 1
                    assert b"private browser fixture" not in received_sources[0]
                    assert "customer-private-filename" not in json.dumps(app.store.get(edit_job_id))
                    page.locator('[data-family="mai"]').click()
                    expect(page.locator(".image-button")).to_have_count(2)
                    assert page.locator(".card-name h3").all_text_contents() == ["MAI Image 2.5", "MAI Image 2.6"]
                    for button in page.locator('[data-action="rate"]').all():
                        expect(button).to_be_enabled()
                    page.locator('[data-family="gpt"]').click()

                    page.locator(".image-button").first.click()
                    expect(page.locator("#zoom-stage")).to_have_class("zoom-stage comparing")
                    expect(page.locator("#zoom-source-image")).to_have_js_property("naturalWidth", 128)
                    expect(page.locator("#zoom-image")).to_have_js_property("naturalWidth", 128)
                    page.locator("#zoom-actual").click()
                    expect(page.locator("#zoom-value")).to_have_text("100%")
                    page.locator('[data-close="zoom-dialog"]').click()
                    page.locator("#view-reference").click()
                    expect(page.locator("#zoom-title")).to_have_text("本轮参考原图")
                    expect(page.locator("#zoom-source-pane")).to_be_hidden()
                    page.locator('[data-close="zoom-dialog"]').click()

                    page.locator("#blind-button").click()
                    page.locator('[data-action="rate"]').first.click()
                    expect(page.locator("#rating-name")).to_contain_text("图像 A")
                    expect(page.locator("#rating-preservation")).to_be_visible()
                    assert page.locator("#rating-composition").count() == 0
                    page.locator("#rating-adherence").select_option("4")
                    page.locator("#rating-preservation").select_option("5")
                    page.locator("#save-rating").click()
                    expect(page.locator("#rating-dialog")).not_to_be_visible()
                    page.locator("#blind-button").click()
                    expect(page.locator("#summary-table")).to_contain_text("未改内容保持")
                    assert "人工均分" not in page.locator("#summary-table").inner_text()
                    assert all(row["rating_mean"] is None for row in app.store.get(edit_job_id)["summary"])

                    page.locator("#history-button").click()
                    expect(page.locator(".history-row")).to_have_count(1)
                    page.locator('[data-close="history-dialog"]').click()
                    page.locator("#reuse-button").click()
                    expect(page.locator("#reference-consent")).not_to_be_checked()
                    expect(page.locator("#start-top-button")).to_be_disabled()
                    page.locator('[data-operation="generate"]').click()
                    expect(page.locator("#reference-upload")).to_be_hidden()
                    page.locator("#history-button").click()
                    expect(page.locator(".history-row")).to_have_count(1)
                    page.locator(f'[data-open="{text_job["id"]}"]').click()
                    expect(page.locator("#reference-result")).to_be_hidden()

                    page.goto(base + "/?operation=edit&family=gpt&clawpilotTheme=dark", wait_until="networkidle")
                    expect(page.locator("#editing-status")).to_contain_text("所有参与模型")
                    expect(page.locator("#reference-result-image")).to_have_js_property("naturalWidth", 128)
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert page.locator("#reference-result").bounding_box()["height"] < 300
                    assert page.locator("#archive-note").bounding_box()["height"] < 450
                    assert not errors, errors
                    translation.assert_not_called()
                finally:
                    browser.close()
        finally:
            release.set()
            app.shutdown()
            server.shutdown()
            server.server_close()
            thread.join(10)
    print("Passed editing upload, consent, same-reference provenance, all-model gate, MAI versions, source/result view, review, history and mobile UI.")
    print("All editing and scoring used temporary fixtures; no Azure/OpenAI image or judge calls were made.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="msedge")
    run(parser.parse_args().channel)
