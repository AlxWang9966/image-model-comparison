"""Private, validated single-image editing inputs with explicit normalization."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import re
import threading
import uuid
import warnings
from pathlib import Path
from typing import Any

from .providers import ValidationError
from .store import now_iso, write_bytes_atomic, write_json_atomic


MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_REFERENCE_PIXELS = 4096 * 4096
MAX_REFERENCE_EDGE = 4096
REFERENCE_ID = re.compile(r"[0-9a-f]{32}")


def capabilities() -> dict[str, Any]:
    available = importlib.util.find_spec("PIL") is not None
    return {
        "available": available,
        "reason": None if available else "Image editing requires Pillow. Run: py -3 -m pip install -r requirements-edit.txt",
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "max_pixels": MAX_REFERENCE_PIXELS,
        "max_edge": MAX_REFERENCE_EDGE,
        "formats": ["image/png", "image/jpeg"],
        "input_count": 1,
    }


def normalize(content: bytes) -> tuple[bytes, dict[str, Any]]:
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValidationError("Upload one PNG/JPEG of at most 8 MiB.")
    if not capabilities()["available"]:
        raise ValidationError(capabilities()["reason"])
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as source:
                if source.format not in ("PNG", "JPEG") or getattr(source, "n_frames", 1) != 1:
                    raise ValidationError("Only a single-frame PNG or JPEG is supported; no animated images.")
                width, height = source.size
                if (
                    width < 64 or height < 64 or width > MAX_REFERENCE_EDGE or height > MAX_REFERENCE_EDGE
                    or width * height > MAX_REFERENCE_PIXELS
                ):
                    raise ValidationError("Reference dimensions must be between 64 and 4096 per edge and at most 16 MP.")
                source.verify()
            with Image.open(io.BytesIO(content)) as source:
                oriented = ImageOps.exif_transpose(source)
                oriented.load()
                alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                converted = oriented.convert("RGBA" if alpha else "RGB")
                clean = Image.frombytes(converted.mode, converted.size, converted.tobytes())
                output = io.BytesIO()
                clean.save(output, format="PNG", compress_level=6)
                normalized = output.getvalue()
                actual_width, actual_height = clean.size
                clean.close()
                converted.close()
                oriented.close()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError("The reference image is invalid, truncated, or exceeds the safe decode limit.") from exc
    if len(normalized) > MAX_UPLOAD_BYTES:
        raise ValidationError("The metadata-free PNG exceeds 8 MiB. Export a smaller source image and upload it again.")
    return normalized, {
        "width": actual_width, "height": actual_height, "bytes": len(normalized),
        "mime_type": "image/png",
        "sha256": hashlib.sha256(normalized).hexdigest(),
        "upload_sha256": hashlib.sha256(content).hexdigest(),
        "normalization": "EXIF orientation applied; converted to RGB/RGBA PNG; metadata removed; no resize or crop.",
        "normalization_version": 1,
    }


class ReferenceStore:
    def __init__(self, output: Path) -> None:
        self.root = output.resolve() / "_references"
        self.lock = threading.Lock()

    def _directory(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or not REFERENCE_ID.fullmatch(identifier):
            raise ValidationError("Invalid reference image identifier.")
        directory = (self.root / identifier).resolve()
        if directory.parent != self.root.resolve():
            raise ValidationError("Reference path is outside the private reference store.")
        return directory

    def add(self, content: bytes) -> dict[str, Any]:
        # Serialize image decoding so simultaneous uploads cannot multiply pixel memory without a bound.
        with self.lock:
            image, metadata = normalize(content)
            identifier = uuid.uuid4().hex
            directory = self._directory(identifier)
            result = {
                **metadata, "id": identifier, "created_at": now_iso(),
                "image_url": f"/references/{identifier}/reference.png",
            }
            write_bytes_atomic(directory / "reference.png", image)
            write_json_atomic(directory / "reference.json", result)
            return result

    def get(self, identifier: str) -> tuple[dict[str, Any], bytes]:
        directory = self._directory(identifier)
        metadata_path = directory / "reference.json"
        image_path = directory / "reference.png"
        if not metadata_path.is_file() or not image_path.is_file():
            raise ValidationError("The reference image is missing. Upload it again before starting an edit.")
        if metadata_path.stat().st_size > 16384 or image_path.stat().st_size > MAX_UPLOAD_BYTES:
            raise ValidationError("The saved reference is larger than the supported limit.")
        if metadata_path.resolve().parent != directory or image_path.resolve().parent != directory:
            raise ValidationError("Reference files must remain inside their private directory.")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValidationError("The saved reference metadata is not readable.") from exc
        image = image_path.read_bytes()
        if (
            not isinstance(metadata, dict) or metadata.get("id") != identifier
            or metadata.get("bytes") != len(image)
            or metadata.get("sha256") != hashlib.sha256(image).hexdigest()
            or type(metadata.get("width")) is not int or type(metadata.get("height")) is not int
            or not 64 <= metadata["width"] <= MAX_REFERENCE_EDGE
            or not 64 <= metadata["height"] <= MAX_REFERENCE_EDGE
        ):
            raise ValidationError("The saved reference image or metadata changed. Upload a fresh copy.")
        return metadata, image
