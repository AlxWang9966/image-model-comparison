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
EDIT_RATING_FIELDS = ("adherence", "visual", "preservation", "text")
JOB_ID = re.compile(r"(?:[0-9a-f]{32}|legacy-[0-9a-f]{16})")
MAX_REPORT_BYTES = 64 * 1024 * 1024


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


def rating_fields(job: dict[str, Any]) -> tuple[str, ...]:
    return EDIT_RATING_FIELDS if job.get("operation", "generate") == "edit" else RATING_FIELDS


def review_ready(job: dict[str, Any]) -> bool:
    if job.get("operation", "generate") != "edit":
        return True
    return bool(
        job["status"] == "completed" and job["samples"]
        and all(sample["status"] == "success" and sample.get("filename") for sample in job["samples"])
        and {sample["model_id"] for sample in job["samples"]} == set(job["model_ids"])
    )


def score(rating: Any, fields: tuple[str, ...] = RATING_FIELDS) -> float | None:
    if not isinstance(rating, dict):
        return None
    values = [rating.get(field) for field in fields if type(rating.get(field)) is int]
    return statistics.mean(values) if values else None


def validate_rating(raw: Any, fields: tuple[str, ...] = RATING_FIELDS) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) - {*fields, "notes"}:
        raise ValidationError("A rating must contain only the criteria for this task and notes.")
    result: dict[str, Any] = {}
    for field in fields:
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
    result["operation"] = job.get("operation", "generate")
    result["pinned"] = job.get("pinned", False)
    result["reports"] = copy.deepcopy(job.get("reports", []))
    result["review_ready"] = review_ready(job)
    fields = rating_fields(job)
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
        scores = [value for row in successful if (value := score(row.get("rating"), fields)) is not None]
        dimensions = {}
        for field in fields:
            values = [
                row["rating"][field] for row in successful
                if isinstance(row.get("rating"), dict) and type(row["rating"].get(field)) is int
            ]
            dimensions[field] = {
                "mean": round(statistics.mean(values), 3) if values else None, "count": len(values),
            }
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
            "rating_mean": round(statistics.mean(scores), 3) if scores and result["operation"] != "edit" else None,
            "rated_count": len(scores),
            "rating_dimensions": dimensions,
        })
    return result


def filter_job(job: dict[str, Any], provider: str) -> dict[str, Any]:
    if provider not in ("all", "gpt", "flux", "mai"):
        raise ValidationError("provider must be all, gpt, flux, or mai.")
    if provider == "all":
        return summarize(job)
    selected = [model for model in job["models"] if model["provider"] == provider]
    if not selected:
        raise ValidationError("This experiment has no samples for the selected model family.")
    result = copy.deepcopy(job)
    ids = {model["id"] for model in selected}
    result["models"] = selected
    result["model_ids"] = [identifier for identifier in job["model_ids"] if identifier in ids]
    result["samples"] = [sample for sample in result["samples"] if sample["model_id"] in ids]
    result["view_filter"] = provider
    result["unfiltered_model_count"] = len(job["model_ids"])
    result["view_note"] = "Model/sample statistics are filtered; experiment-level metadata and the archive refer to the original complete run."
    filtered = summarize(result)
    filtered["review_ready"] = review_ready(job)
    return filtered


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
        if "pinned" in job and type(job["pinned"]) is not bool:
            raise ValidationError("Invalid saved pin state.")
        if "reports" in job and (
            not isinstance(job["reports"], list)
            or any(
                not isinstance(report, dict)
                or not isinstance(report.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", report["id"])
                or type(report.get("bytes")) is not int or not 0 < report["bytes"] <= MAX_REPORT_BYTES
                or not isinstance(report.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", report["sha256"])
                or report.get("provider") not in ("all", "gpt", "flux", "mai")
                or type(report.get("include_images")) is not bool or type(report.get("include_notes")) is not bool
                or not isinstance(report.get("created_at"), str)
                for report in job["reports"]
            )
        ):
            raise ValidationError("Invalid saved report records.")
        if job.get("operation", "generate") not in ("generate", "edit"):
            raise ValidationError("Unknown saved operation.")
        if job.get("operation") == "edit":
            reference = job.get("reference_image")
            if (
                not isinstance(reference, dict) or reference.get("filename") != "reference.png"
                or not isinstance(reference.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])
            ):
                raise ValidationError("The editing experiment has no valid source-image provenance.")
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
                validate_rating(sample["rating"], rating_fields(job))

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
        with self.lock:
            job = self.get(identifier)
            if not review_ready(job):
                raise ValidationError("Finish all editing models/rounds successfully before scoring this experiment.")
            rating = validate_rating(raw, rating_fields(job))
            sample = next((row for row in job["samples"] if row["id"] == sample_id), None)
            if sample is None:
                raise KeyError("Sample not found.")
            if sample["status"] != "success" or not sample.get("filename"):
                raise ValidationError("Only successful samples with a saved image can be rated.")
            return self.update_sample(identifier, sample_id, rating=rating)

    def pin(self, identifier: str, pinned: bool) -> dict[str, Any]:
        if type(pinned) is not bool:
            raise ValidationError("pinned must be a boolean.")
        with self.lock:
            self.get(identifier)
            return self.update_job(identifier, pinned=pinned)

    def save_report(
        self, identifier: str, content: bytes, *, provider: str, include_images: bool,
        include_notes: bool, source_updated_at: str,
    ) -> dict[str, Any]:
        if not 0 < len(content) <= MAX_REPORT_BYTES:
            raise ValidationError("The report exceeds the local 64 MiB file limit; export without embedded images.")
        report_id = uuid.uuid4().hex
        entry = {
            "id": report_id, "created_at": now_iso(), "provider": provider,
            "include_images": include_images, "include_notes": include_notes,
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "source_updated_at": source_updated_at,
            "download_url": f"/api/jobs/{identifier}/reports/{report_id}.html",
        }
        with self.lock:
            job = self.get(identifier)
            path = self.output / identifier / "reports" / f"{report_id}.html"
            write_bytes_atomic(path, content)
            try:
                self.update_job(identifier, reports=[*job.get("reports", []), entry])
            except OSError:
                path.unlink(missing_ok=True)
                raise
        return entry

    def report_content(self, identifier: str, report_id: str) -> bytes:
        job = self.get(identifier)
        if not re.fullmatch(r"[0-9a-f]{32}", report_id):
            raise KeyError("Report not found.")
        report = next((entry for entry in job.get("reports", []) if entry["id"] == report_id), None)
        if report is None:
            raise KeyError("Report not found in this experiment.")
        directory = (self.output / identifier / "reports").resolve()
        path = (directory / f"{report_id}.html").resolve()
        if path.parent != directory or not path.is_file():
            raise KeyError("The saved report file is missing.")
        if path.stat().st_size != report["bytes"] or not 0 < path.stat().st_size <= MAX_REPORT_BYTES:
            raise ValidationError("The saved report was modified. Export a new snapshot.")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != report["sha256"]:
            raise ValidationError("The saved report checksum no longer matches.")
        return content

    def history(self) -> list[dict[str, Any]]:
        with self.lock:
            result = []
            for job in sorted(self.jobs.values(), key=lambda item: item["created_at"], reverse=True):
                previews = {}
                for model in job["models"]:
                    image_url = next(
                        (sample["image_url"] for sample in job["samples"]
                         if sample["model_id"] == model["id"] and sample["status"] == "success" and sample.get("image_url")),
                        None,
                    )
                    if image_url:
                        previews.setdefault(model["provider"], image_url)
                result.append({
                    key: job[key] for key in (
                        "id", "created_at", "prompt", "mode", "runs", "size", "gpt_quality",
                        "status", "source", "model_ids", "label",
                    )
                })
                result[-1].update(
                    operation=job.get("operation", "generate"),
                    topic=job.get("topic", ""),
                    providers=sorted({model["provider"] for model in job["models"]}),
                    completed=sum(row["status"] in TERMINAL for row in job["samples"]),
                    total=len(job["samples"]),
                    ratings_count=sum(score(row.get("rating"), rating_fields(job)) is not None for row in job["samples"]),
                    pinned=job.get("pinned", False),
                    report_count=len(job.get("reports", [])),
                    preview_image_url=next(
                        (sample["image_url"] for sample in job["samples"] if sample["status"] == "success" and sample.get("image_url")),
                        job.get("reference_image", {}).get("image_url"),
                    ),
                    preview_images=previews,
                    reference_image_url=job.get("reference_image", {}).get("image_url"),
                )
            return result

    def image_path(self, identifier: str, filename: str) -> Path:
        job = self.get(identifier)
        reference = filename == "reference.png" and job.get("operation") == "edit" and job.get("reference_image")
        if not reference and not re.fullmatch(r"[0-9a-f]{16}\.(?:png|jpg)", filename):
            raise KeyError("Image not found.")
        if not reference and not any(sample.get("filename") == filename for sample in job["samples"]):
            raise KeyError("Image is not part of this experiment.")
        directory = (self.output / identifier).resolve()
        path = (directory / filename).resolve()
        if path.parent != directory or not path.is_file():
            raise KeyError("Image file is missing.")
        return path

    def reference_bytes(self, identifier: str) -> bytes:
        job = self.get(identifier)
        metadata = job.get("reference_image")
        if not metadata:
            raise ValidationError("This experiment has no editing reference.")
        path = self.image_path(identifier, "reference.png")
        if path.stat().st_size != metadata.get("bytes") or path.stat().st_size > 8 * 1024 * 1024:
            raise ValidationError("The experiment reference size no longer matches its metadata.")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != metadata["sha256"]:
            raise ValidationError("The experiment reference was modified; no editing request was sent.")
        return content

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
