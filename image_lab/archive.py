"""Readable, annotated image archives without connection or credential fields."""

from __future__ import annotations

import hashlib
import re
import unicodedata
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from .providers import MAX_IMAGE_BYTES
from .store import JOB_ID, RATING_FIELDS, write_bytes_atomic, write_json_atomic


class ArchiveError(ValueError):
    pass


def prompt_topic(prompt: str, explicit: str = "") -> str:
    if explicit.strip():
        return explicit.strip()
    normalized = " ".join(prompt.split())
    return normalized[:32] if normalized else "unrecorded-prompt"


def component(value: str, maximum: int = 32) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE).strip(".-_")
    normalized = normalized.encode("utf-16-le")[:maximum * 2].decode("utf-16-le", errors="ignore")
    normalized = normalized.rstrip(".-_") or "untitled"
    if re.fullmatch(r"(?i)(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", normalized):
        normalized = "topic-" + normalized
    return normalized


def markdown_text(value: Any) -> str:
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in ("\\", "`", "*", "_", "[", "]", "|"):
        text = text.replace(char, "\\" + char)
    return text.replace("\r", " ").replace("\n", " ")


def public_model(model: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": model["id"],
        "name": model["name"],
        "provider": model["provider"],
        "version": model.get("version") or None,
    }


def write_if_changed(path: Path, content: bytes) -> None:
    if not path.is_file() or path.read_bytes() != content:
        write_bytes_atomic(path, content)


class ImageArchive:
    def __init__(self, source: Path, destination: Path) -> None:
        self.source = source.resolve()
        self.destination = destination.resolve()
        self.destination.mkdir(parents=True, exist_ok=True)
        write_if_changed(self.destination / "README.md", (
            "# Image archive\n\n"
            "This directory is generated locally and is excluded from Git.\n\n"
            "Layout: prompt topic / experiment / model / round image + JSON notes.\n\n"
            "Each experiment contains README.md and manifest.json. Each sample has JSON notes, "
            "including its model, topic, full prompt, request parameters, timing, and human review. "
            "Failed or missing legacy images have notes but no invented replacement image.\n\n"
            "Connection endpoints, deployment aliases, subscription IDs, credentials, raw provider "
            "errors, and absolute source paths are omitted. Images, prompts, topics, and reviewer "
            "notes can still contain confidential business data: review them before sharing.\n\n"
            "Do not edit generated metadata here; use the application for ratings. "
            "Keep annotated/edited image copies elsewhere. Existing different images are not overwritten.\n"
        ).encode("utf-8"))

    def folder(self, job: dict[str, Any]) -> Path:
        if not JOB_ID.fullmatch(job["id"]):
            raise ArchiveError("Invalid experiment identifier for archiving.")
        topic = prompt_topic(job.get("prompt", ""), job.get("topic", ""))
        digest = hashlib.sha256(topic.encode("utf-8")).hexdigest()[:6]
        topic_folder = f"{component(topic, 26)}-{digest}"
        try:
            created = datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
        except (ValueError, TypeError) as exc:
            raise ArchiveError("The experiment has no valid creation timestamp.") from exc
        batch = created.strftime("%Y%m%d-%H%M%S") + "-" + job["id"][-12:]
        path = (self.destination / topic_folder / batch).resolve()
        if not path.is_relative_to(self.destination):
            raise ArchiveError("Archive path is outside the archive directory.")
        return path

    def export(self, job: dict[str, Any], include_reviews: bool = True) -> dict[str, Any]:
        folder = self.folder(job)
        topic = prompt_topic(job.get("prompt", ""), job.get("topic", ""))
        models = {model["id"]: model for model in job["models"]}
        samples = []
        available = 0
        missing = 0
        for sample in job["samples"]:
            model = models[sample["model_id"]]
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,79}", model["id"]):
                raise ArchiveError("Unsafe model identifier in archive data.")
            model_folder = (folder / model["id"]).resolve()
            if not model_folder.is_relative_to(folder):
                raise ArchiveError("Model directory is outside the experiment archive.")
            basename = f"round-{sample['round']:02d}_{component(model['id'], 24)}_{component(topic, 18)}"
            filename = sample.get("filename")
            archived_image = None
            checksum = None
            image_status = "not_generated"
            if sample["status"] == "success":
                image_status = "not_retained"
                if filename:
                    if not re.fullmatch(r"[0-9a-f]{16}\.(?:png|jpg)", filename):
                        raise ArchiveError("Unexpected source image filename.")
                    source = (self.source / job["id"] / filename).resolve()
                    if not source.is_relative_to(self.source):
                        raise ArchiveError("Source image is outside the results directory.")
                    if source.is_file():
                        if not 0 < source.stat().st_size <= MAX_IMAGE_BYTES:
                            raise ArchiveError("Source image has an unsupported file size.")
                        content = source.read_bytes()
                        if len(content) != sample.get("output_bytes"):
                            raise ArchiveError("Source image byte count does not match the stored result.")
                        checksum = hashlib.sha256(content).hexdigest()
                        image = model_folder / (basename + source.suffix)
                        if image.is_file():
                            if hashlib.sha256(image.read_bytes()).hexdigest() != checksum:
                                raise ArchiveError("An archived image was changed. Move that edited copy before regenerating the archive.")
                        else:
                            write_bytes_atomic(image, content)
                        archived_image = image.relative_to(folder).as_posix()
                        image_status = "available"
                        available += 1
                if image_status != "available":
                    missing += 1
            request = sample.get("request") or {}
            error_match = re.search(r"\bHTTP\s+(\d{3})\b", sample.get("error") or "")
            review = sample.get("rating") if include_reviews else None
            record = {
                "experiment_id": job["id"],
                "topic": topic,
                "prompt": job.get("prompt") or None,
                "prompt_recorded": bool(job.get("prompt")),
                "model": public_model(model),
                "round": sample["round"],
                "status": sample["status"],
                "created_at": job["created_at"],
                "source": job["source"],
                "mode": job["mode"],
                "requested_size": job["size"],
                "request_parameters": {
                    key: request[key] for key in (
                        "size", "width", "height", "n", "quality", "output_format", "aspect_ratio",
                    ) if key in request
                },
                "timing": {
                    "scope": job["timing_scope"],
                    **{key: sample.get(key) for key in ("elapsed_ms", "api_ms", "download_ms", "save_ms")},
                },
                "image": {
                    "status": image_status,
                    "file": archived_image,
                    "width": sample.get("width"),
                    "height": sample.get("height"),
                    "bytes": sample.get("output_bytes", 0) if archived_image else None,
                    "sha256": checksum,
                },
                "http_error_code": int(error_match[1]) if error_match else None,
                "diagnostic_note": (
                    "The original image is missing or was overwritten by an older script; no replacement was invented."
                    if image_status == "not_retained" else
                    "No image was generated. Full private diagnostics remain in the original local run.json."
                    if image_status == "not_generated" else None
                ),
                "human_review": {
                    **{key: review.get(key) for key in RATING_FIELDS},
                    "notes": review.get("notes", ""),
                } if review else None,
                "reviews_included": include_reviews,
            }
            notes = model_folder / (basename + ".json")
            write_json_atomic(notes, record)
            samples.append({**record, "notes_file": notes.relative_to(folder).as_posix()})
        manifest = {
            "schema_version": 1,
            "experiment_id": job["id"],
            "topic": topic,
            "prompt": job.get("prompt") or None,
            "created_at": job["created_at"],
            "status": job["status"],
            "source": job["source"],
            "mode": job["mode"],
            "requested_size": job["size"],
            "gpt_quality": job["gpt_quality"],
            "timing_scope": job["timing_scope"],
            "rounds": job["runs"],
            "models": [public_model(model) for model in job["models"]],
            "sample_count": len(samples),
            "image_count": available,
            "missing_successful_images": missing,
            "reviews_included": include_reviews,
            "privacy": "Connection/credential fields and full errors are excluded; manually review images, prompts, topics, and any included human notes before sharing.",
            "samples": samples,
        }
        write_json_atomic(folder / "manifest.json", manifest)
        rows = [
            "# " + markdown_text(topic), "",
            "| Field | Value |", "| --- | --- |",
            f"| Experiment | `{job['id']}` |",
            f"| Created | {markdown_text(job['created_at'])} |",
            f"| Source / status | {markdown_text(job['source'])} / {markdown_text(job['status'])} |",
            f"| Requested size | {markdown_text(job['size'])} |",
            f"| Execution | {markdown_text(job['mode'])}, {job['runs']} round(s) |",
            f"| Timing scope | `{job['timing_scope']}` |",
            f"| Images retained | {available} / {len(samples)} samples |", "",
            "## Full prompt", "",
        ]
        rows.extend("    " + line for line in (job.get("prompt") or "[Not recorded in the original benchmark]").splitlines())
        rows += [
            "", "## Images and per-image notes", "",
            "| Model | Version | Round | Status | Image | Time (s) | Notes |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for sample in samples:
            image_file = sample["image"]["file"]
            image_link = f"[Original image]({urllib.parse.quote(image_file)})" if image_file else sample["image"]["status"]
            elapsed = sample["timing"]["elapsed_ms"]
            elapsed_text = f"{elapsed / 1000:.3f}" if elapsed is not None else "not measured"
            rows.append(
                f"| {markdown_text(sample['model']['name'])} | {markdown_text(sample['model']['version'] or 'unknown')} "
                f"| {sample['round']} | {sample['status']} | {image_link} "
                f"| {elapsed_text} | [JSON notes]({urllib.parse.quote(sample['notes_file'])}) |"
            )
        rows += [
            "", "## Interpretation and sharing", "",
            "- Each image is a byte-for-byte copy of its retained source; SHA-256 is recorded in its JSON notes.",
            "- End-to-end latency includes generation, network, decoding, and original image saving, not this secondary archive copy.",
            "- Legacy API-response timing excludes decoding/saving. Do not pool the two timing scopes.",
            "- A small sample is a demonstration, not a reliable quality/speed ranking.",
            "- Missing legacy images and failed calls remain explicit; no synthetic replacement is created.",
            "- JSON notes contain the full prompt, model, topic, parameters, timing, and any included human review.",
            "- Connection endpoints, deployment aliases, subscription IDs, credentials, full errors, and absolute source paths are not included.",
            "- Images, prompts, topics, and human notes may still contain private data. Review before sharing.",
            "",
        ]
        write_if_changed(folder / "README.md", "\n".join(rows).encode("utf-8"))
        return {
            "status": "ready",
            "relative_dir": str(folder.relative_to(self.destination)),
            "root_name": self.destination.name,
            "image_count": available,
            "sample_count": len(samples),
            "missing_images": missing,
            "error": None,
        }
