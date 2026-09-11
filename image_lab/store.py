"""Durable runs, human ratings, summaries, and read-only legacy imports."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import re
import statistics
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .providers import AppConfig, ProviderError, ValidationError, image_info, redact_error


LOG = logging.getLogger("image_lab")
TERMINAL = {"success", "failed", "cancelled", "interrupted"}
ACTIVE_JOBS = {"queued", "preparing", "running"}
RATING_FIELDS = ("adherence", "visual", "composition", "text")
JOB_ID = re.compile(r"(?:[0-9a-f]{32}|legacy-[0-9a-f]{16})")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def write_bytes_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_json_atomic(path: Path, data: Any) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    write_bytes_atomic(path, content)


def score(rating: Any) -> float | None:
    if not isinstance(rating, dict):
        return None
    values = [rating.get(field) for field in RATING_FIELDS if type(rating.get(field)) is int]
    return statistics.mean(values) if values else None


def validate_rating(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {*RATING_FIELDS, "notes"}:
        raise ValidationError("A rating must contain only the four criteria and notes.")
    result: dict[str, Any] = {}
    for field in RATING_FIELDS:
        value = raw.get(field)
        if value is not None and (type(value) is not int or not 1 <= value <= 5):
            raise ValidationError(f"{field} must be an integer from 1 to 5, or null.")
        result[field] = value
    notes = raw.get("notes", "")
    if not isinstance(notes, str) or len(notes) > 2000:
        raise ValidationError("Rating notes must be a string of at most 2000 characters.")
    result["notes"] = notes.strip()
    return result


def summarize(job: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(job)
    samples = result["samples"]
    result["progress"] = {
        "done": sum(sample["status"] in TERMINAL for sample in samples),
        "total": len(samples),
    }
    result["summary"] = []
    for model_id in result["model_ids"]:
        rows = [sample for sample in samples if sample["model_id"] == model_id]
        successful = [row for row in rows if row["status"] == "success"]
        timings = sorted(
            row["elapsed_ms"] for row in successful
            if isinstance(row.get("elapsed_ms"), (float, int))
            and not isinstance(row["elapsed_ms"], bool)
            and math.isfinite(row["elapsed_ms"]) and row["elapsed_ms"] >= 0
        )
        scores = [value for row in successful if (value := score(row.get("rating"))) is not None]
        result["summary"].append({
            "model_id": model_id,
            "attempted": sum(bool(row.get("started_at")) or job["source"] == "legacy" for row in rows),
            "successes": len(successful),
            "failures": sum(row["status"] in ("failed", "interrupted") for row in rows),
            "cancelled": sum(row["status"] == "cancelled" for row in rows),
            "mean_ms": round(statistics.mean(timings), 3) if timings else None,
            "median_ms": round(statistics.median(timings), 3) if timings else None,
            "p95_ms": timings[max(0, math.ceil(len(timings) * 0.95) - 1)] if timings else None,
            "min_ms": timings[0] if timings else None,
            "max_ms": timings[-1] if timings else None,
            "rating_mean": round(statistics.mean(scores), 3) if scores else None,
            "rated_count": len(scores),
        })
    return result


def empty_sample(model_id: str, round_number: int, request: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:16],
        "model_id": model_id,
        "round": round_number,
        "status": "pending",
        "started_at": None,
        "completed_at": None,
        "elapsed_ms": None,
        "api_ms": None,
        "download_ms": None,
        "save_ms": None,
        "output_bytes": 0,
        "width": None,
        "height": None,
        "filename": None,
        "image_url": None,
        "error": None,
        "warnings": [],
        "rating": None,
        "request": request,
        "original_prompt": request.get("prompt"),
        "effective_prompt": request.get("prompt"),
        "prompt_variant": "original",
        "revised_prompt": None,
    }


class RunStore:
    def __init__(self, output: Path) -> None:
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self._load()

    def _load(self) -> None:
        for directory in sorted(self.output.iterdir()):
            if not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
                continue
            path = directory / "run.json"
            if not path.is_file():
                self.warn(f"Incomplete result directory {directory.name}; run.json is missing.")
                continue
            try:
                job = json.loads(path.read_text(encoding="utf-8-sig"))
                self._validate_saved_job(job, directory.name)
            except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
                self.warn(f"Could not load {directory.name}: {type(exc).__name__}. The original files were preserved.")
                continue
            if job["status"] in ACTIVE_JOBS:
                job["status"] = "interrupted"
                job["updated_at"] = now_iso()
                job["warnings"].append(
                    "The server stopped during this experiment. Pending/running requests were not automatically retried."
                )
                for sample in job["samples"]:
                    if sample["status"] not in TERMINAL:
                        sample.update(
                            status="interrupted", completed_at=now_iso(),
                            error="Server stopped; upstream completion/billing may be unknown.",
                        )
                write_json_atomic(path, job)
            self.jobs[job["id"]] = job

    @staticmethod
    def _validate_saved_job(job: Any, identifier: str) -> None:
        if (
            not isinstance(job, dict) or job.get("schema_version") != 1
            or job.get("id") != identifier
            or job.get("status") not in {*ACTIVE_JOBS, "completed", "cancelled", "interrupted", "failed"}
            or not isinstance(job.get("samples"), list)
            or not isinstance(job.get("model_ids"), list)
            or not isinstance(job.get("models"), list)
            or not isinstance(job.get("warnings"), list)
        ):
            raise ValidationError("Unsupported saved run format.")
        required = {
            "created_at", "updated_at", "prompt", "size", "gpt_quality", "runs",
            "mode", "source", "label", "timing_scope", "cancel_requested",
        }
        if not required.issubset(job):
            raise ValidationError("Incomplete saved run.")
        if "topic" in job and (not isinstance(job["topic"], str) or len(job["topic"]) > 80):
            raise ValidationError("Invalid saved prompt topic.")
        for sample in job["samples"]:
            if (
                not isinstance(sample, dict) or not isinstance(sample.get("id"), str)
                or not re.fullmatch(r"[0-9a-f]{16}", sample["id"])
                or sample.get("model_id") not in job["model_ids"]
                or sample.get("status") not in {*TERMINAL, "pending", "preparing", "running"}
                or type(sample.get("round")) is not int
                or not isinstance(sample.get("warnings"), list)
            ):
                raise ValidationError("Invalid saved sample.")
            if sample.get("rating") is not None:
                validate_rating(sample["rating"])

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        LOG.warning(message)

    def _persist(self, job: dict[str, Any]) -> None:
        write_json_atomic(self.output / job["id"] / "run.json", job)

    def add(self, job: dict[str, Any]) -> None:
        with self.lock:
            if job["id"] in self.jobs:
                raise ValidationError("A run with this ID already exists.")
            self._persist(job)
            self.jobs[job["id"]] = copy.deepcopy(job)

    def get(self, identifier: str) -> dict[str, Any]:
        with self.lock:
            if not JOB_ID.fullmatch(identifier) or identifier not in self.jobs:
                raise KeyError("Experiment not found.")
            return summarize(self.jobs[identifier])

    def update_job(self, identifier: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            job = copy.deepcopy(self.jobs[identifier])
            job.update(changes, updated_at=now_iso())
            self._persist(job)
            self.jobs[identifier] = job
            return summarize(job)

    def update_sample(self, identifier: str, sample_id: str, **changes: Any) -> dict[str, Any]:
        with self.lock:
            job = copy.deepcopy(self.jobs[identifier])
            sample = next((row for row in job["samples"] if row["id"] == sample_id), None)
            if sample is None:
                raise KeyError("Sample not found.")
            sample.update(changes)
            job["updated_at"] = now_iso()
            self._persist(job)
            self.jobs[identifier] = job
            return summarize(job)

    def fail_job(self, identifier: str, message: str) -> None:
        with self.lock:
            job = copy.deepcopy(self.jobs[identifier])
            job.update(status="failed", updated_at=now_iso())
            job["warnings"].append(message)
            for sample in job["samples"]:
                if sample["status"] not in TERMINAL:
                    sample.update(status="failed", error=message, completed_at=now_iso())
            try:
                self._persist(job)
            except OSError:
                warning = (
                    "The server could not persist this failure state. It is visible in memory only; "
                    "check disk space/permissions. Existing result files were preserved."
                )
                job["warnings"].append(warning)
                self.warn(warning)
            self.jobs[identifier] = job

    def rate(self, identifier: str, sample_id: str, raw: Any) -> dict[str, Any]:
        rating = validate_rating(raw)
        with self.lock:
            job = self.get(identifier)
            sample = next((row for row in job["samples"] if row["id"] == sample_id), None)
            if sample is None:
                raise KeyError("Sample not found.")
            if sample["status"] != "success" or not sample.get("filename"):
                raise ValidationError("Only successful samples with a saved image can be rated.")
            return self.update_sample(identifier, sample_id, rating=rating)

    def history(self) -> list[dict[str, Any]]:
        with self.lock:
            result = []
            for job in sorted(self.jobs.values(), key=lambda item: item["created_at"], reverse=True):
                result.append({
                    key: job[key] for key in (
                        "id", "created_at", "prompt", "mode", "runs", "size", "gpt_quality",
                        "status", "source", "model_ids", "label",
                    )
                })
                result[-1].update(
                    topic=job.get("topic", ""),
                    completed=sum(row["status"] in TERMINAL for row in job["samples"]),
                    total=len(job["samples"]),
                    ratings_count=sum(score(row.get("rating")) is not None for row in job["samples"]),
                )
            return result

    def image_path(self, identifier: str, filename: str) -> Path:
        job = self.get(identifier)
        if not re.fullmatch(r"[0-9a-f]{16}\.(?:png|jpg)", filename):
            raise KeyError("Image not found.")
        if not any(sample.get("filename") == filename for sample in job["samples"]):
            raise KeyError("Image is not part of this experiment.")
        directory = (self.output / identifier).resolve()
        path = (directory / filename).resolve()
        if path.parent != directory or not path.is_file():
            raise KeyError("Image file is missing.")
        return path

    def import_legacy(self, root: Path, config: AppConfig) -> None:
        directories = [root] + [
            path for path in root.iterdir()
            if path.is_dir() and path.resolve() != self.output
            and not path.name.startswith(".") and path.name not in ("image_lab", "tests")
        ]
        for directory in directories:
            reports = sorted(directory.glob("benchmark-results-*.json"))
            for report in reports:
                try:
                    self._import_report(report, report == reports[-1], root, config)
                except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ProviderError) as exc:
                    self.warn(
                        f"Could not import {report.relative_to(root)}: {type(exc).__name__}: "
                        f"{redact_error(str(exc))}. Original files were preserved."
                    )

    def _import_report(self, report: Path, newest_in_directory: bool, root: Path, config: AppConfig) -> None:
        if report.stat().st_size > 2 * 1024 * 1024:
            raise ValidationError("Legacy report is larger than 2 MB.")
        report_bytes = report.read_bytes()
        stamp = report.stem.removeprefix("benchmark-results-")
        try:
            created = datetime.strptime(stamp, "%Y%m%d-%H%M%S").astimezone(timezone.utc).isoformat()
        except ValueError as exc:
            raise ValidationError("Legacy report filename has no recognized timestamp.") from exc
        manifest_path = report.with_name(f"request-manifest-{stamp}.json")
        if manifest_path.exists() and manifest_path.stat().st_size > 2 * 1024 * 1024:
            raise ValidationError("Legacy manifest is larger than 2 MB.")
        manifest_bytes = manifest_path.read_bytes() if manifest_path.exists() else b""
        identifier = "legacy-" + hashlib.sha256(
            str(report.relative_to(root)).encode("utf-8") + report_bytes + manifest_bytes
        ).hexdigest()[:16]
        if identifier in self.jobs:
            return
        rows = json.loads(report_bytes.decode("utf-8-sig"))
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or not rows or len(rows) > 1000:
            raise ValidationError("Legacy report must contain 1 to 1000 samples.")
        manifest = json.loads(manifest_bytes.decode("utf-8-sig")) if manifest_bytes else {}
        if not isinstance(manifest, dict):
            raise ValidationError("Legacy manifest must be an object.")
        if not isinstance(manifest.get("prompt", ""), str):
            raise ValidationError("Legacy prompt must be text.")
        model_map = {model.deployment.lower(): model for model in config.models if model.deployment}
        model_ids: list[str] = []
        snapshots = []
        samples = []
        manifest_models = manifest.get("models", [])
        if not isinstance(manifest_models, list) or any(not isinstance(item, dict) for item in manifest_models):
            raise ValidationError("Legacy manifest models must be objects.")
        request_map = {str(item.get("name", "")).lower(): item.get("request_body", {}) for item in manifest_models}
        durations = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValidationError("Invalid legacy sample.")
            duration = row.get("elapsed_ms")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
                raise ValidationError("Invalid legacy elapsed_ms.")
            if type(row.get("success")) is not bool or type(row.get("run")) is not int or row["run"] < 1:
                raise ValidationError("Invalid legacy run/success values.")
            durations.append(duration)
        earliest_image = report.stat().st_mtime - sum(durations) / 1000 - 60
        for row in rows:
            model = model_map.get(str(row.get("model", "")).lower())
            if model is None:
                raise ValidationError(f"Unknown legacy model: {row.get('model')}.")
            if model.id not in model_ids:
                model_ids.append(model.id)
                snapshot = model.public()
                snapshot["version"] = ""
                snapshots.append(snapshot)
            request = request_map.get(str(row["model"]).lower(), {})
            if not isinstance(request, dict):
                raise ValidationError("Legacy request_body must be an object.")
            request = {
                key: value for key, value in request.items()
                if key in ("model", "prompt", "width", "height", "n", "size", "quality", "output_format")
            }
            sample = empty_sample(model.id, row["run"], request)
            sample.update(
                status="success" if row["success"] else "failed",
                elapsed_ms=row["elapsed_ms"], api_ms=row["elapsed_ms"],
                error=redact_error(str(row.get("error", ""))) or None,
                completed_at=created,
            )
            if row["success"]:
                output_path = row.get("output_path", "")
                if not isinstance(output_path, str):
                    raise ValidationError("Legacy output_path must be text.")
                source = (report.parent / Path(output_path).name).resolve()
                within_directory = source.parent == report.parent.resolve()
                declared_bytes = row.get("output_bytes")
                reliable = (
                    newest_in_directory and within_directory and source.is_file()
                    and source.stat().st_size == declared_bytes
                    and 0 < source.stat().st_size <= 32 * 1024 * 1024
                    and earliest_image <= source.stat().st_mtime <= report.stat().st_mtime + 2
                )
                if reliable:
                    content = source.read_bytes()
                    extension, width, height = image_info(content)
                    filename = f"{sample['id']}.{extension}"
                    write_bytes_atomic(self.output / identifier / filename, content)
                    sample.update(
                        filename=filename, image_url=f"/images/{identifier}/{filename}",
                        output_bytes=len(content), width=width, height=height,
                    )
                else:
                    sample["warnings"].append(
                        "The original image is missing or may have been overwritten by a later script run. "
                        "Timing is preserved, but no possibly unrelated image is attached."
                    )
            samples.append(sample)
        width, height = manifest.get("width"), manifest.get("height")
        requested_size = f"{width}x{height}" if width and height else "unknown"
        if manifest.get("gpt_size") and manifest["gpt_size"] != requested_size:
            requested_size = "mixed"
        warnings = [
            "Legacy timing stops at the API response, before decoding/saving. Do not mix it with new end-to-end timings.",
            "The original script did not record the model version; imported versions are unknown.",
        ]
        if not manifest_bytes:
            warnings.append("No request manifest exists; the original prompt, size, and quality are unknown.")
        job = {
            "schema_version": 1,
            "id": identifier,
            "created_at": created,
            "updated_at": created,
            "status": "completed",
            "source": "legacy",
            "label": f"{report.parent.name} / {stamp}",
            "prompt": manifest.get("prompt", ""),
            "size": requested_size,
            "gpt_quality": manifest.get("gpt_quality", "unknown"),
            "runs": max(sample["round"] for sample in samples),
            "mode": "sequential",
            "model_ids": model_ids,
            "models": snapshots,
            "timing_scope": "legacy_api_response",
            "cancel_requested": False,
            "samples": samples,
            "warnings": warnings,
        }
        self.add(job)
