"""Loopback-only HTTP API and bounded image benchmark scheduler."""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import secrets
import threading
import time
import urllib.parse
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__, prompts, providers
from .archive import ArchiveError, ImageArchive, prompt_topic
from .locking import AlreadyRunningError, single_instance
from .providers import AppConfig, Credentials, ModelConfig, ProviderError, ValidationError
from .store import (
    ACTIVE_JOBS, RATING_FIELDS, TERMINAL, RunStore, empty_sample, filter_job, now_iso,
    write_bytes_atomic, write_json_atomic,
)


ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("image_lab")


class ConflictError(RuntimeError):
    pass


class HttpError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def parse_job_input(raw: Any, config: AppConfig) -> tuple[dict[str, Any], list[ModelConfig]]:
    expected = {"prompt", "model_ids", "size", "gpt_quality", "runs", "mode"}
    optional = {"topic", "english_prompt", "language_mode"}
    if not isinstance(raw, dict) or not expected.issubset(raw) or set(raw) - expected - optional:
        raise ValidationError("Supply the experiment settings, with optional topic, english_prompt, and language_mode.")
    prompt = raw["prompt"]
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise ValidationError("The shared prompt must contain 1 to 4000 characters.")
    topic = raw.get("topic", "")
    if not isinstance(topic, str) or len(topic) > 80:
        raise ValidationError("topic must be a string of at most 80 characters.")
    english = raw.get("english_prompt", "")
    if not isinstance(english, str) or len(english) > prompts.MAX_ENGLISH_PROMPT:
        raise ValidationError(f"english_prompt must be text of at most {prompts.MAX_ENGLISH_PROMPT} characters.")
    english = english.strip()
    if english:
        prompts.validate_english_prompt(english)
    language_mode = raw.get("language_mode", "model_defaults")
    if language_mode not in prompts.LANGUAGE_MODES:
        raise ValidationError("Invalid language_mode.")
    identifiers = raw["model_ids"]
    if (
        not isinstance(identifiers, list) or not 1 <= len(identifiers) <= len(providers.MODEL_PROVIDERS)
        or any(not isinstance(identifier, str) for identifier in identifiers)
        or len(set(identifiers)) != len(identifiers)
    ):
        raise ValidationError(f"Select 1 to {len(providers.MODEL_PROVIDERS)} unique models.")
    model_map = {model.id: model for model in config.models}
    if any(identifier not in model_map for identifier in identifiers):
        raise ValidationError("An unknown model was selected.")
    models = [model_map[identifier] for identifier in identifiers]
    for model in models:
        problem = model.configuration_error()
        if problem:
            raise ValidationError(f"{model.name}: {problem}")
    if raw["size"] not in providers.SIZES:
        raise ValidationError("Unsupported image size.")
    if raw["gpt_quality"] not in providers.QUALITIES:
        raise ValidationError("Unsupported GPT quality.")
    if type(raw["runs"]) is not int or not 1 <= raw["runs"] <= 10:
        raise ValidationError("The number of rounds must be an integer from 1 to 10.")
    if raw["mode"] not in ("parallel", "sequential"):
        raise ValidationError("mode must be parallel or sequential.")
    for model in models:
        providers.build_request(model, prompt.strip(), raw["size"], raw["gpt_quality"])
    translation_needed = (
        not english and prompts.needs_english_version(prompt)
        and any(prompts.uses_english(model, language_mode) for model in models)
    )
    if translation_needed:
        problem = config.translation.configuration_error()
        if problem:
            raise ValidationError(
                "An English counterpart is required. Provide one or configure/enable the Azure translator. " + problem
            )
    return {
        **raw, "prompt": prompt.strip(), "topic": prompt_topic(prompt, topic),
        "english_prompt": english, "language_mode": language_mode,
    }, models


def planned_sample(model: ModelConfig, number: int, params: dict[str, Any]) -> dict[str, Any]:
    wants_english = prompts.uses_english(model, params["language_mode"])
    if wants_english and params["english_prompt"]:
        text, variant = params["english_prompt"], "provided_english"
    elif wants_english and prompts.needs_english_version(params["prompt"]):
        text, variant = "", "pending_english"
    else:
        text, variant = params["prompt"], "original"
    sample = empty_sample(model.id, number, providers.build_request(model, text, params["size"], params["gpt_quality"]))
    sample.update(original_prompt=params["prompt"], effective_prompt=text or None, prompt_variant=variant)
    return sample


def load_config(root: Path, config_path: Path) -> AppConfig:
    if not config_path.exists() and config_path.resolve() == (root / "image-lab.config.json").resolve():
        example = root / "image-lab.config.example.json"
        if not example.is_file():
            raise ValidationError("Configuration is missing. Copy image-lab.config.example.json to image-lab.config.json.")
        config = AppConfig.parse(json.loads(example.read_text(encoding="utf-8-sig")))
        write_json_atomic(config_path, config.persisted())
        LOG.info("Created local configuration from the safe template. Configure and enable your own models in the UI.")
        return config
    return AppConfig.parse(json.loads(config_path.read_text(encoding="utf-8-sig")))


class Application:
    def __init__(
        self, root: Path, config_path: Path, output: Path, import_legacy: bool = True,
        archive_dir: Path | None = None,
    ) -> None:
        self.root = root
        self.config_path = config_path
        self.config = load_config(root, config_path)
        self.store = RunStore(output)
        if import_legacy:
            self.store.import_legacy(root, self.config)
        self.credentials = Credentials()
        self.csrf_token = secrets.token_hex(32)
        self.lock = threading.RLock()
        self.active_job_id: str | None = None
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.archive_dir = (archive_dir or root / "image-lab-archive").resolve()
        self.archive_lock = threading.Lock()
        for item in self.store.history():
            self.sync_archive(item["id"])

    def sync_archive(self, identifier: str) -> dict[str, Any]:
        with self.archive_lock:
            job = self.store.get(identifier)
            if job["status"] in ACTIVE_JOBS:
                return job
            topic = prompt_topic(job.get("prompt", ""), job.get("topic", ""))
            try:
                archive = ImageArchive(self.store.output, self.archive_dir)
                result = archive.export({**job, "topic": topic})
            except (OSError, ArchiveError) as exc:
                LOG.warning("Readable archive failed for %s: %s", identifier, type(exc).__name__)
                result = {
                    "status": "failed", "relative_dir": None, "root_name": self.archive_dir.name,
                    "error": f"Archive update failed ({type(exc).__name__}). Original results are preserved; check the archive directory and server terminal.",
                }
                if isinstance(exc, ArchiveError):
                    result["error"] += " " + str(exc)
            return self.store.update_job(identifier, topic=topic, archive=result)

    def rate(self, identifier: str, sample_id: str, data: Any) -> dict[str, Any]:
        self.store.rate(identifier, sample_id, data)
        return self.sync_archive(identifier)

    def bootstrap(self) -> dict[str, Any]:
        with self.lock:
            return {
                "csrf_token": self.csrf_token,
                "config": self.config.public(),
                "history": self.store.history(),
                "warnings": list(self.store.warnings),
                "active_job_id": self.active_job_id,
            }

    def save_config(self, raw: Any) -> dict[str, Any]:
        with self.lock:
            if self.active_job_id is not None:
                raise ConflictError("Wait for the current experiment to finish before changing model settings.")
            if isinstance(raw, dict) and "request_timeout_seconds" not in raw:
                raw = {**raw, "request_timeout_seconds": self.config.request_timeout_seconds}
            if isinstance(raw, dict) and "translation" not in raw:
                raw = {**raw, "translation": self.config.translation.public()}
            if isinstance(raw, dict) and isinstance(raw.get("models"), list):
                supplied = {
                    model["id"] for model in raw["models"]
                    if isinstance(model, dict) and isinstance(model.get("id"), str)
                }
                retained = [
                    model.public() for model in self.config.models
                    if model.id in ("flux-2-pro", "flux-2-flex", "gpt-image-2-5-sunburst") and model.id not in supplied
                ]
                raw = {**raw, "models": [*raw["models"], *retained]}
            config = AppConfig.parse(raw)
            write_json_atomic(self.config_path, config.persisted())
            self.config = config
            return config.public()

    def create_job(self, raw: Any) -> dict[str, Any]:
        with self.lock:
            if self.active_job_id is not None:
                raise ConflictError("An experiment is already active. Stop its future requests or wait for completion.")
            config = self.config
            params, models = parse_job_input(raw, config)
            identifier = uuid.uuid4().hex
            created = now_iso()
            job = {
                "schema_version": 1,
                "id": identifier,
                "created_at": created,
                "updated_at": created,
                "status": "queued",
                "source": "live",
                "label": params["topic"],
                **params,
                "models": [model.public() for model in models],
                "timing_scope": "end_to_end",
                "cancel_requested": False,
                "samples": [
                    planned_sample(model, round_number, params)
                    for round_number in range(1, params["runs"] + 1)
                    for model in models
                ],
                "translation": {"status": "not_needed", "elapsed_ms": None, "api_ms": None, "request_attempted": False},
                "warnings": [],
            }
            self.store.add(job)
            self.active_job_id = identifier
            self.cancel_event = threading.Event()
            self.worker = threading.Thread(
                target=self._run_job, args=(identifier, models, config, self.cancel_event),
                daemon=True, name=f"image-benchmark-{identifier[:8]}",
            )
            self.worker.start()
            return self.store.get(identifier)

    def cancel(self, identifier: str) -> dict[str, Any]:
        with self.lock:
            self.store.get(identifier)
            if self.active_job_id != identifier:
                raise ConflictError("This experiment is no longer running.")
            self.cancel_event.set()
            return self.store.update_job(identifier, cancel_requested=True)

    def _finish_remaining(self, identifier: str, status: str, message: str) -> None:
        for sample in self.store.get(identifier)["samples"]:
            if sample["status"] not in TERMINAL:
                self.store.update_sample(
                    identifier, sample["id"], status=status, error=message, completed_at=now_iso(),
                )

    def _prepare_prompts(
        self, identifier: str, config: AppConfig, models: list[ModelConfig], cancel_event: threading.Event,
    ) -> None:
        job = self.store.get(identifier)
        pending = [sample for sample in job["samples"] if sample.get("prompt_variant") == "pending_english"]
        if pending and not cancel_event.is_set():
            self.store.update_job(identifier, translation={
                "status": "running", "elapsed_ms": None, "api_ms": None, "request_attempted": None,
            })
            try:
                result = prompts.translate(job["prompt"], config.translation, self.credentials, config.subscription_id)
            except prompts.TranslationError as exc:
                self.store.update_job(identifier, translation={
                    "status": "failed", "elapsed_ms": exc.elapsed_ms, "api_ms": None,
                    "request_attempted": exc.request_attempted, "error": str(exc),
                })
                for sample in pending:
                    self.store.update_sample(
                        identifier, sample["id"], status="failed", completed_at=now_iso(),
                        error="English preparation failed before an image request was sent: " + str(exc),
                    )
            else:
                model_map = {model.id: model for model in models}
                for sample in pending:
                    self.store.update_sample(
                        identifier, sample["id"], effective_prompt=result.english_prompt,
                        prompt_variant="translated_english",
                        request=providers.build_request(
                            model_map[sample["model_id"]], result.english_prompt, job["size"], job["gpt_quality"],
                        ),
                    )
                self.store.update_job(identifier, english_prompt=result.english_prompt, translation={
                    "status": "completed", "elapsed_ms": result.elapsed_ms, "api_ms": result.api_ms,
                    "request_attempted": True, "usage": result.usage,
                    "deployment": config.translation.deployment,
                })
        current = self.store.get(identifier)
        effective = {sample["effective_prompt"] for sample in current["samples"] if sample.get("effective_prompt")}
        warnings = list(current["warnings"])
        if len(effective) > 1:
            warnings.append(
                "This experiment uses different prompt-language variants. Compare the same task, "
                "not identical prompt strings; original and effective prompts are recorded for every sample."
            )
        elif effective and next(iter(effective)) != current["prompt"]:
            warnings.append("All image models use an English counterpart, not the original prompt string.")
        if current["translation"]["status"] == "failed":
            warnings.append(
                "English preparation failed. Models that require an English counterpart were not called; "
                "original-language models can still proceed. No untranslated fallback was sent."
            )
        self.store.update_job(identifier, warnings=warnings)

    def _run_job(
        self, identifier: str, models: list[ModelConfig], config: AppConfig, cancel_event: threading.Event,
    ) -> None:
        try:
            self.store.update_job(identifier, status="preparing")
            self._prepare_prompts(identifier, config, models, cancel_event)
            ready = []
            for model in models:
                if cancel_event.is_set():
                    break
                if not any(
                    sample["model_id"] == model.id and sample["status"] == "pending"
                    for sample in self.store.get(identifier)["samples"]
                ):
                    continue
                try:
                    self.credentials.headers(model, config.subscription_id)
                    ready.append(model)
                except ProviderError as exc:
                    for sample in self.store.get(identifier)["samples"]:
                        if sample["model_id"] == model.id:
                            self.store.update_sample(
                                identifier, sample["id"], status="failed",
                                error=f"Authentication failed before an image request was sent: {exc}",
                                completed_at=now_iso(),
                            )
            self.store.update_job(identifier, status="running")
            job = self.store.get(identifier)
            with ThreadPoolExecutor(max_workers=max(1, len(ready)), thread_name_prefix="image-request") as executor:
                for round_number in range(1, job["runs"] + 1):
                    if cancel_event.is_set():
                        break
                    rows = {
                        sample["model_id"]: sample for sample in job["samples"]
                        if sample["round"] == round_number
                    }
                    if job["mode"] == "parallel":
                        futures = [
                            executor.submit(
                                self._run_sample, identifier, rows[model.id], model, config, cancel_event,
                            )
                            for model in ready
                        ]
                        for future in futures:
                            future.result()
                    elif ready:
                        # Rotate order each round to reduce a fixed first/last-model bias.
                        offset = (round_number - 1) % len(ready)
                        for model in ready[offset:] + ready[:offset]:
                            if cancel_event.is_set():
                                break
                            self._run_sample(identifier, rows[model.id], model, config, cancel_event)
            if cancel_event.is_set():
                self._finish_remaining(identifier, "cancelled", "Stopped before sending a generation request.")
                status = "cancelled"
            else:
                completed = self.store.get(identifier)
                status = "completed" if any(row["status"] == "success" for row in completed["samples"]) else "failed"
            self.store.update_job(identifier, status=status)
            self.sync_archive(identifier)
        except Exception as exc:
            # This is the thread boundary: unexpected faults must become visible, durable failures.
            LOG.exception("Benchmark %s failed unexpectedly", identifier)
            cancel_event.set()
            self.store.fail_job(
                identifier, f"Internal error ({type(exc).__name__}); see the server terminal.",
            )
        finally:
            with self.lock:
                if self.active_job_id == identifier:
                    self.active_job_id = None

    def _run_sample(
        self, identifier: str, sample: dict[str, Any], model: ModelConfig,
        config: AppConfig, cancel_event: threading.Event,
    ) -> None:
        if cancel_event.is_set():
            return
        self.store.update_sample(identifier, sample["id"], status="preparing")
        try:
            headers = self.credentials.headers(model, config.subscription_id)
        except ProviderError as exc:
            self.store.update_sample(
                identifier, sample["id"], status="failed", error=str(exc), completed_at=now_iso(),
            )
            return
        if cancel_event.is_set():
            return
        requested_width, requested_height = (
            int(part) for part in self.store.get(identifier)["size"].split("x")
        )
        self.store.update_sample(identifier, sample["id"], status="running", started_at=now_iso())
        started = time.perf_counter()
        try:
            image = providers.generate(model, sample["request"], headers, config.request_timeout_seconds)
            filename = f"{sample['id']}.{image.extension}"
            save_started = time.perf_counter()
            write_bytes_atomic(self.store.output / identifier / filename, image.content)
            saved = time.perf_counter()
            warnings = []
            if (image.width, image.height) != (requested_width, requested_height):
                warnings.append(
                    f"Requested {requested_width}x{requested_height}, received {image.width}x{image.height}. "
                    "Resolution differs; this is not an equal-pixel comparison."
                )
            self.store.update_sample(
                identifier, sample["id"], status="success", completed_at=now_iso(),
                elapsed_ms=round((saved - started) * 1000, 3),
                api_ms=image.api_ms, download_ms=image.download_ms,
                save_ms=round((saved - save_started) * 1000, 3),
                output_bytes=len(image.content), width=image.width, height=image.height,
                filename=filename, image_url=f"/images/{identifier}/{filename}",
                revised_prompt=image.revised_prompt, warnings=warnings,
            )
        except (ProviderError, OSError, ValueError) as exc:
            LOG.warning("Sample %s failed: %s", sample["id"], providers.redact_error(str(exc), tuple(headers.values())))
            self.store.update_sample(
                identifier, sample["id"], status="failed", completed_at=now_iso(),
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                error=providers.redact_error(str(exc), tuple(headers.values())),
            )

    def shutdown(self) -> None:
        with self.lock:
            if self.active_job_id:
                self.cancel_event.set()
                self.store.update_job(self.active_job_id, cancel_requested=True)
                LOG.info("Waiting for in-flight calls; no more generation requests will be sent.")
            worker = self.worker
        if worker:
            worker.join()


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_csv(job: dict[str, Any]) -> bytes:
    output = io.StringIO(newline="")
    fields = [
        "experiment_id", "created_at", "source", "timing_scope", "model", "model_id",
        "deployment", "model_version", "topic", "prompt", "original_prompt", "effective_prompt",
        "prompt_variant", "language_mode", "translation_ms", "requested_size", "gpt_quality", "mode",
        "round", "status", "elapsed_seconds", "api_seconds", "download_seconds", "save_seconds",
        "actual_width", "actual_height", "output_bytes", *RATING_FIELDS, "notes",
        "image_file", "error", "warnings", "request_json", "revised_prompt",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    models = {model["id"]: model for model in job["models"]}
    for sample in job["samples"]:
        model = models[sample["model_id"]]
        rating = sample.get("rating") or {}
        row = {
            "experiment_id": job["id"], "created_at": job["created_at"],
            "source": job["source"], "timing_scope": job["timing_scope"],
            "model": model["name"], "model_id": model["id"],
            "deployment": model["deployment"], "model_version": model.get("version", ""),
            "topic": job.get("topic", ""), "prompt": job["prompt"],
            "original_prompt": job["prompt"],
            "effective_prompt": sample.get("effective_prompt", sample.get("request", {}).get("prompt")),
            "prompt_variant": sample.get("prompt_variant", "original"),
            "language_mode": job.get("language_mode", "original_all"),
            "translation_ms": job.get("translation", {}).get("elapsed_ms"),
            "requested_size": job["size"],
            "gpt_quality": job["gpt_quality"] if model["provider"] == "gpt" else "",
            "mode": job["mode"], "round": sample["round"], "status": sample["status"],
            "actual_width": sample.get("width"), "actual_height": sample.get("height"),
            "output_bytes": sample.get("output_bytes"),
            **{field: rating.get(field) for field in RATING_FIELDS},
            "notes": rating.get("notes", ""), "image_file": sample.get("filename"),
            "error": sample.get("error"), "warnings": " | ".join(sample.get("warnings", [])),
            "request_json": json.dumps(sample.get("request", {}), ensure_ascii=False),
            "revised_prompt": sample.get("revised_prompt"),
        }
        for name in ("elapsed", "api", "download", "save"):
            value = sample.get(f"{name}_ms")
            row[f"{name}_seconds"] = round(value / 1000, 6) if value is not None else None
        writer.writerow({key: csv_value(value) for key, value in row.items()})
    return output.getvalue().encode("utf-8-sig")


def reject_json_constant(value: str) -> None:
    raise ValidationError(f"JSON must not contain {value}.")


class BusyRequestHandler(BaseHTTPRequestHandler):
    def handle(self) -> None:
        self.connection.settimeout(1)
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        LOG.warning("Local HTTP worker limit reached; request was not processed.")

    def do_GET(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if 0 < length <= 131072:
            self.rfile.read(length)
        body = b'{"error":"Local server is busy. Please try again shortly."}'
        self.close_connection = True
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET
    do_PUT = do_GET


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    max_http_workers = 16

    def __init__(self, address: tuple[str, int], app: Application) -> None:
        self.app = app
        self.request_slots = threading.BoundedSemaphore(self.max_http_workers)
        super().__init__(address, RequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.request_slots.acquire(blocking=False):
            try:
                BusyRequestHandler(request, client_address, self)
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except (OSError, RuntimeError, MemoryError):
            self.request_slots.release()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


class RequestHandler(BaseHTTPRequestHandler):
    server: LocalServer
    server_version = f"ImageLab/{__version__}"
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionError, TimeoutError):
            self.close_connection = True
            LOG.debug("Local browser closed its connection.")

    def log_message(self, format: str, *args: Any) -> None:
        if len(args) > 1 and str(args[1]).startswith(("4", "5")):
            LOG.warning("HTTP %s %s", self.command, urllib.parse.urlsplit(self.path).path)

    def _check_client(self, mutate: bool) -> None:
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "").lower() not in allowed:
            raise HttpError(403, "This workbench is available only through its loopback URL.")
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in allowed}:
            raise HttpError(403, "Cross-origin requests are not allowed.")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise HttpError(403, "Cross-site requests are not allowed.")
        if mutate:
            token = self.headers.get("X-Image-Lab-Token", "")
            if not secrets.compare_digest(token.encode("utf-8"), self.server.app.csrf_token.encode("ascii")):
                raise HttpError(403, "Missing or expired local request token. Reload the page.")

    def _read_json(self) -> Any:
        if self.headers.get_content_type() != "application/json":
            raise HttpError(415, "Use Content-Type: application/json.")
        if self.headers.get("Transfer-Encoding"):
            raise HttpError(400, "Transfer-Encoding is not supported.")
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError as exc:
            raise HttpError(400, "Invalid Content-Length.") from exc
        if not 0 <= length <= 131072:
            raise HttpError(413, "Request body must not exceed 128 KB.")
        self.connection.settimeout(15)
        body = self.rfile.read(length)
        if len(body) != length:
            raise HttpError(400, "Incomplete request body.")
        try:
            return json.loads(body.decode("utf-8"), parse_constant=reject_json_constant)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HttpError(400, "Invalid UTF-8 JSON body.") from exc

    def _send(self, status: int, content: bytes, content_type: str, download: str | None = None) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'self'; form-action 'self'",
        )
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        if content:
            self.wfile.write(content)

    def _json(self, status: int, data: Any, download: str | None = None) -> None:
        self._send(
            status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            "application/json; charset=utf-8", download,
        )

    def _dispatch(self, action: Any, mutate: bool = False) -> None:
        try:
            self._check_client(mutate)
            action()
        except (ConnectionError, TimeoutError):
            LOG.info("Local client disconnected or timed out.")
            self.close_connection = True
        except HttpError as exc:
            self.close_connection = True
            self._json(exc.status, {"error": str(exc)})
        except ValidationError as exc:
            self._json(400, {"error": str(exc)})
        except ConflictError as exc:
            self._json(409, {"error": str(exc)})
        except KeyError as exc:
            self._json(404, {"error": str(exc).strip("'")})
        except OSError:
            LOG.exception("Could not read or persist local workbench data")
            self._json(500, {"error": "Local file I/O failed. See the server terminal; data was not silently discarded."})
        except Exception:
            LOG.exception("Unexpected local API failure")
            self._json(500, {"error": "Unexpected server error. See the server terminal."})

    def do_GET(self) -> None:
        self._dispatch(self._get)

    def do_POST(self) -> None:
        self._dispatch(self._post, mutate=True)

    def do_PUT(self) -> None:
        self._dispatch(self._put, mutate=True)

    def _path(self) -> str:
        return urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

    def _get(self) -> None:
        path = self._path()
        app = self.server.app
        if path in ("/", "/index.html", "/image-lab.html"):
            self._send(200, (app.root / "image-lab.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif path == "/api/health":
            self._json(200, {"ok": True, "active_job_id": app.active_job_id})
        elif path == "/api/bootstrap":
            self._json(200, app.bootstrap())
        elif match := re.fullmatch(r"/api/jobs/([^/]+)(?:/export\.(json|csv))?", path):
            identifier, export = match.groups()
            job = app.store.get(identifier)
            suffix = ""
            if export:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query, keep_blank_values=True)
                if set(query) - {"provider"} or len(query.get("provider", ["all"])) != 1:
                    raise ValidationError("Exports accept a single provider filter.")
                provider = query.get("provider", ["all"])[0]
                job = filter_job(job, provider)
                suffix = f"-{provider}" if provider != "all" else ""
            if export == "csv":
                self._send(200, export_csv(job), "text/csv; charset=utf-8", f"image-lab-{identifier}{suffix}.csv")
            else:
                self._json(200, job, f"image-lab-{identifier}{suffix}.json" if export else None)
        elif match := re.fullmatch(r"/images/([^/]+)/([^/]+)", path):
            image_path = app.store.image_path(*match.groups())
            content_type = "image/png" if image_path.suffix == ".png" else "image/jpeg"
            self._send(200, image_path.read_bytes(), content_type)
        else:
            raise HttpError(404, "Not found.")

    def _post(self) -> None:
        path = self._path()
        data = self._read_json()
        if path == "/api/jobs":
            self._json(202, self.server.app.create_job(data))
        elif match := re.fullmatch(r"/api/jobs/([^/]+)/cancel", path):
            if data != {}:
                raise ValidationError("Cancel expects an empty object.")
            self._json(200, self.server.app.cancel(match[1]))
        elif match := re.fullmatch(r"/api/jobs/([^/]+)/archive", path):
            if data != {}:
                raise ValidationError("Archive refresh expects an empty object.")
            job = self.server.app.store.get(match[1])
            if job["status"] in ACTIVE_JOBS:
                raise ConflictError("The readable archive is finalized when the experiment finishes.")
            self._json(200, self.server.app.sync_archive(match[1]))
        else:
            raise HttpError(404, "Not found.")

    def _put(self) -> None:
        path = self._path()
        data = self._read_json()
        if path == "/api/config":
            self._json(200, self.server.app.save_config(data))
        elif match := re.fullmatch(r"/api/jobs/([^/]+)/samples/([^/]+)/rating", path):
            self._json(200, self.server.app.rate(match[1], match[2], data))
        else:
            raise HttpError(404, "Not found.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local image model comparison workbench.")
    parser.add_argument("--version", action="version", version=f"Image Lab {__version__}")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--config", type=Path, default=ROOT / "image-lab.config.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "image-lab-output")
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "image-lab-archive")
    parser.add_argument("--no-import", action="store_true", help="Skip importing old PowerShell reports.")
    parser.add_argument("--open", action="store_true", help="Open the UI in the default browser.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        with ExitStack() as locks:
            locks.enter_context(single_instance(args.config.with_name(f".{args.config.name}.lock")))
            locks.enter_context(single_instance(args.output_dir / ".server.lock"))
            locks.enter_context(single_instance(args.archive_dir / ".server.lock"))
            app = Application(
                ROOT, args.config, args.output_dir, import_legacy=not args.no_import, archive_dir=args.archive_dir,
            )
            server = LocalServer(("127.0.0.1", args.port), app)
            url = f"http://127.0.0.1:{args.port}"
            LOG.info("Image Lab listening at %s", url)
            LOG.info("Results: %s | Original scripts and Azure security settings are unchanged.", app.store.output)
            if args.open:
                webbrowser.open(url)
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                LOG.info("Stopping the local workbench.")
            finally:
                server.server_close()
                app.shutdown()
    except AlreadyRunningError as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
