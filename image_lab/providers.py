"""Synchronous Azure MAI, GPT Images, and FLUX Image API adapters."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


MODEL_PROVIDERS = {
    "mai-image-2-5": "mai",
    "gpt-image-2": "gpt",
    "gpt-image-2-5": "gpt",
    "flux-1-kontext-pro": "flux",
    "flux-2-pro": "flux",
    "flux-2-flex": "flux",
    "gpt-image-2-5-sunburst": "gpt",
    "mai-image-2-6": "mai",
}
LEGACY_MODEL_IDS = frozenset(("mai-image-2-5", "gpt-image-2", "gpt-image-2-5", "flux-1-kontext-pro"))
GPT_SIZES = ("1024x1024", "1536x1024", "1024x1536")
SIZES = (*GPT_SIZES, "2048x2048")
FLUX_PATHS = {
    "flux-1-kontext-pro": "/providers/blackforestlabs/v1/flux-kontext-pro",
    "flux-2-pro": "/providers/blackforestlabs/v1/flux-2-pro",
    "flux-2-flex": "/providers/blackforestlabs/v1/flux-2-flex",
}
QUALITIES = ("low", "medium", "high", "auto")
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_RESPONSE_BYTES = 48 * 1024 * 1024
AZURE_HOST = re.compile(
    r"^[a-z0-9][a-z0-9-]*\."
    r"(?:services\.ai\.azure\.com|openai\.azure\.com|"
    r"api\.cognitive\.microsoft\.com|cognitiveservices\.azure\.com)$"
)


class ValidationError(ValueError):
    pass


class ProviderError(RuntimeError):
    pass


def text_field(data: dict[str, Any], key: str, maximum: int = 512) -> str:
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > maximum:
        raise ValidationError(f"{key} must be a string of at most {maximum} characters.")
    return value.strip()


def validate_service_url(endpoint: str, auth_mode: str, allow_openai: bool = False) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("Invalid endpoint URL.") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValidationError("Use an HTTPS endpoint without credentials or a fragment.")
    azure = bool(AZURE_HOST.fullmatch(parsed.hostname.lower()))
    openai = parsed.hostname.lower() == "api.openai.com"
    if not azure and not (openai and auth_mode == "api_key_env" and allow_openai):
        raise ValidationError(
            "Use an Azure AI endpoint, or api.openai.com with an environment API key. "
            "Azure login tokens are never sent to other hosts."
        )
    if any(key != "api-version" for key, _ in urllib.parse.parse_qsl(parsed.query)):
        raise ValidationError("Only api-version is allowed in the endpoint query; never put keys there.")
    return parsed


def validate_endpoint(endpoint: str, auth_mode: str, provider: str) -> None:
    parsed = validate_service_url(endpoint, auth_mode, allow_openai=provider == "gpt")
    native_flux = provider == "flux" and parsed.path in FLUX_PATHS.values()
    if not parsed.path.endswith("/images/generations") and not native_flux:
        raise ValidationError("Enter the full synchronous Images API or supported FLUX BFL-provider URL.")
    if provider == "mai" and parsed.path != "/mai/v1/images/generations":
        raise ValidationError("MAI requires /mai/v1/images/generations.")
    if provider != "mai" and "/mai/" in parsed.path:
        raise ValidationError("GPT and FLUX require the Images API, not the MAI API.")


def parse_auth(raw: dict[str, Any]) -> tuple[str, str, str]:
    mode = text_field(raw, "auth_mode")
    if mode not in ("azure_cli", "api_key_env"):
        raise ValidationError("auth_mode must be azure_cli or api_key_env.")
    env_name = text_field(raw, "api_key_env", 128)
    if env_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValidationError("api_key_env must be an environment variable NAME, not a secret.")
    header = text_field(raw, "api_key_header")
    if header not in ("api-key", "Authorization"):
        raise ValidationError("api_key_header must be api-key or Authorization.")
    return mode, env_name, header


def configuration_problem(
    enabled: bool, endpoint: str, deployment: str, auth_mode: str, api_key_env: str,
) -> str | None:
    if not endpoint or not deployment:
        return "Set a real endpoint and deployment before using this slot."
    if not enabled:
        return "This service is disabled in settings."
    if auth_mode == "azure_cli" and not shutil.which("az"):
        return "Azure CLI is not installed or is not on PATH."
    if auth_mode == "api_key_env" and (not api_key_env or not os.environ.get(api_key_env, "").strip()):
        return f"Set the server environment variable {api_key_env or '(not specified)'} and restart."
    return None


@dataclass(frozen=True)
class ModelConfig:
    id: str
    name: str
    provider: str
    deployment: str
    endpoint: str
    auth_mode: str
    api_key_env: str
    api_key_header: str
    version: str
    supported_sizes: tuple[str, ...]
    enabled: bool
    prompt_policy: str = "original"
    flux_steps: int | None = None
    flux_guidance: float | None = None

    @classmethod
    def parse(cls, raw: Any) -> ModelConfig:
        if not isinstance(raw, dict):
            raise ValidationError("Each model configuration must be an object.")
        allowed = {
            "id", "name", "provider", "deployment", "endpoint", "auth_mode",
            "api_key_env", "api_key_header", "version", "supported_sizes",
            "enabled", "configured", "configuration_error", "prompt_policy", "flux_steps", "flux_guidance",
        }
        if set(raw) - allowed:
            raise ValidationError("Unknown model fields; store only an environment variable name, never a key.")
        identifier = text_field(raw, "id")
        provider = text_field(raw, "provider")
        if MODEL_PROVIDERS.get(identifier) != provider:
            raise ValidationError("Unknown model slot or incompatible provider adapter.")
        name = text_field(raw, "name", 80)
        deployment = text_field(raw, "deployment", 128)
        if not name or (deployment and not re.fullmatch(r"[A-Za-z0-9_.-]+", deployment)):
            raise ValidationError("A display name and a valid deployment/model identifier are required.")
        auth_mode, env_name, header = parse_auth(raw)
        endpoint = text_field(raw, "endpoint", 2048)
        if endpoint:
            validate_endpoint(endpoint, auth_mode, provider)
            path = urllib.parse.urlsplit(endpoint).path
            if provider == "flux":
                if path in FLUX_PATHS.values() and path != FLUX_PATHS[identifier]:
                    raise ValidationError("The BFL path must match the selected FLUX model slot.")
                if identifier in ("flux-2-pro", "flux-2-flex") and path != FLUX_PATHS[identifier]:
                    raise ValidationError("FLUX.2 requires its native BFL provider endpoint, not the Images API.")
            match = re.search(r"/deployments/([^/]+)/", path)
            if match and deployment and urllib.parse.unquote(match[1]) != deployment:
                raise ValidationError("The deployment in the endpoint URL must match the deployment field.")
            if urllib.parse.urlsplit(endpoint).hostname == "api.openai.com" and header != "Authorization":
                raise ValidationError("OpenAI API keys require the Authorization header.")
            if "/providers/blackforestlabs/" in path and auth_mode == "api_key_env" and header != "Authorization":
                raise ValidationError("The BFL provider API uses the Authorization header for API keys.")
        sizes = raw.get("supported_sizes")
        if (
            not isinstance(sizes, list) or not sizes
            or any(size not in SIZES for size in sizes)
            or len(sizes) != len(set(sizes))
        ):
            raise ValidationError("supported_sizes must contain unique supported image sizes.")
        if (provider == "mai" or identifier == "flux-1-kontext-pro") and sizes != ["1024x1024"]:
            raise ValidationError("MAI Image 2.5/2.6 and FLUX.1 Kontext use the common 1024x1024 size (1 MP limit).")
        if provider == "gpt" and any(size not in GPT_SIZES for size in sizes):
            raise ValidationError("This GPT adapter supports the configured 1024/1536 image sizes.")
        enabled = raw.get("enabled")
        if type(enabled) is not bool:
            raise ValidationError("enabled must be a boolean.")
        policy = raw.get("prompt_policy", "original")
        if policy not in ("original", "english"):
            raise ValidationError("prompt_policy must be original or english.")
        steps = raw.get("flux_steps", 50 if identifier == "flux-2-flex" else None)
        guidance = raw.get("flux_guidance", 4.5 if identifier == "flux-2-flex" else None)
        if identifier == "flux-2-flex":
            if type(steps) is not int or not 1 <= steps <= 50:
                raise ValidationError("FLUX.2 Flex steps must be an integer from 1 to 50.")
            if type(guidance) not in (int, float) or not math.isfinite(guidance) or not 1.5 <= guidance <= 10:
                raise ValidationError("FLUX.2 Flex guidance must be a finite number from 1.5 to 10.")
            guidance = float(guidance)
        elif steps is not None or guidance is not None:
            raise ValidationError("Steps/guidance controls are exposed only for FLUX.2 Flex.")
        return cls(
            identifier, name, provider, deployment, endpoint, auth_mode, env_name,
            header, text_field(raw, "version", 80), tuple(sizes), enabled, policy, steps, guidance,
        )

    def configuration_error(self) -> str | None:
        return configuration_problem(self.enabled, self.endpoint, self.deployment, self.auth_mode, self.api_key_env)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["supported_sizes"] = list(self.supported_sizes)
        data["configuration_error"] = self.configuration_error()
        data["configured"] = data["configuration_error"] is None
        return data


@dataclass(frozen=True)
class TranslationConfig:
    enabled: bool = False
    endpoint: str = ""
    deployment: str = ""
    auth_mode: str = "azure_cli"
    api_key_env: str = ""
    api_key_header: str = "api-key"
    timeout_seconds: int = 60

    @classmethod
    def parse(cls, raw: Any) -> TranslationConfig:
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw) - {
            "enabled", "endpoint", "deployment", "auth_mode", "api_key_env",
            "api_key_header", "timeout_seconds", "configured", "configuration_error",
        }:
            raise ValidationError("Invalid translation settings; do not store keys in configuration.")
        data = {**asdict(cls()), **raw}
        enabled = data["enabled"]
        if type(enabled) is not bool:
            raise ValidationError("Translation enabled must be a boolean.")
        endpoint = text_field(data, "endpoint", 2048)
        deployment = text_field(data, "deployment", 128)
        if deployment and not re.fullmatch(r"[A-Za-z0-9_.-]+", deployment):
            raise ValidationError("Invalid translation deployment identifier.")
        mode, env_name, header = parse_auth(data)
        timeout = data["timeout_seconds"]
        if type(timeout) is not int or not 10 <= timeout <= 120:
            raise ValidationError("Translation timeout must be an integer from 10 to 120 seconds.")
        if endpoint:
            parsed = validate_service_url(endpoint, mode)
            if not re.fullmatch(r"/openai/(?:v1|deployments/[^/]+)/chat/completions", parsed.path):
                raise ValidationError("Translation requires a full Azure OpenAI chat/completions endpoint.")
            match = re.search(r"/deployments/([^/]+)/", parsed.path)
            if match and deployment and urllib.parse.unquote(match[1]) != deployment:
                raise ValidationError("Translation endpoint deployment must match the deployment field.")
        return cls(enabled, endpoint, deployment, mode, env_name, header, timeout)

    def configuration_error(self) -> str | None:
        return configuration_problem(self.enabled, self.endpoint, self.deployment, self.auth_mode, self.api_key_env)

    def public(self) -> dict[str, Any]:
        return {
            **asdict(self), "configuration_error": self.configuration_error(),
            "configured": self.configuration_error() is None,
        }


def new_model_slot(identifier: str) -> ModelConfig:
    if identifier == "mai-image-2-6":
        return ModelConfig(
            id=identifier, name="MAI Image 2.6", provider="mai",
            deployment="MAI-Image-2.6", endpoint="", auth_mode="azure_cli",
            api_key_env="", api_key_header="api-key", version="",
            supported_sizes=("1024x1024",), enabled=False,
        )
    if identifier == "gpt-image-2-5-sunburst":
        return ModelConfig(
            id=identifier, name="GPT Image 2.5 Sunburst", provider="gpt",
            deployment="gpt-image-2.5-sunburst", endpoint="", auth_mode="azure_cli",
            api_key_env="", api_key_header="api-key", version="",
            supported_sizes=GPT_SIZES, enabled=False,
        )
    if identifier not in ("flux-2-pro", "flux-2-flex"):
        raise ValidationError("Unsupported missing model slot.")
    flex = identifier == "flux-2-flex"
    return ModelConfig(
        id=identifier, name="FLUX.2 Flex" if flex else "FLUX.2 Pro", provider="flux",
        deployment="FLUX.2-flex" if flex else "FLUX.2-pro", endpoint="",
        auth_mode="azure_cli", api_key_env="", api_key_header="Authorization",
        version="", supported_sizes=SIZES, enabled=False, prompt_policy="original",
        flux_steps=50 if flex else None, flux_guidance=4.5 if flex else None,
    )


@dataclass(frozen=True)
class AppConfig:
    subscription_id: str
    resource_group: str
    request_timeout_seconds: int
    models: tuple[ModelConfig, ...]
    translation: TranslationConfig = TranslationConfig()

    @classmethod
    def parse(cls, raw: Any) -> AppConfig:
        if not isinstance(raw, dict):
            raise ValidationError("Configuration must be a JSON object.")
        if set(raw) - {"subscription_id", "resource_group", "request_timeout_seconds", "models", "translation"}:
            raise ValidationError("Unknown configuration fields.")
        subscription = text_field(raw, "subscription_id", 36)
        try:
            uuid.UUID(subscription)
        except ValueError as exc:
            raise ValidationError("subscription_id must be an Azure subscription UUID.") from exc
        group = text_field(raw, "resource_group", 90)
        if not group or not re.fullmatch(r"[\w.()-]+", group):
            raise ValidationError("A valid Azure resource_group is required.")
        timeout = raw.get("request_timeout_seconds", 300)
        if type(timeout) is not int or not 30 <= timeout <= 600:
            raise ValidationError("request_timeout_seconds must be an integer from 30 to 600.")
        rows = raw.get("models")
        if not isinstance(rows, list) or not len(LEGACY_MODEL_IDS) <= len(rows) <= len(MODEL_PROVIDERS):
            raise ValidationError("Keep the original model slots; newer slots can be added automatically.")
        models = tuple(ModelConfig.parse(row) for row in rows)
        ids = {model.id for model in models}
        if len(ids) != len(models) or not LEGACY_MODEL_IDS.issubset(ids):
            raise ValidationError("Model slots must be unique and preserve the four original slots.")
        models += tuple(new_model_slot(identifier) for identifier in MODEL_PROVIDERS if identifier not in ids)
        return cls(subscription, group, timeout, models, TranslationConfig.parse(raw.get("translation")))

    def public(self) -> dict[str, Any]:
        return {
            "subscription_id": self.subscription_id,
            "resource_group": self.resource_group,
            "request_timeout_seconds": self.request_timeout_seconds,
            "models": [model.public() for model in self.models],
            "translation": self.translation.public(),
        }

    def persisted(self) -> dict[str, Any]:
        return asdict(self)


def redact_error(message: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
            if secret.lower().startswith("bearer ") and secret[7:]:
                message = message.replace(secret[7:], "[redacted]")
    message = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [redacted]", message)
    message = re.sub(r"https?://[^\s<>\"']+", "[upstream URL]", message)
    return message[:1800]


class Credentials:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}

    def headers(self, model: ModelConfig | TranslationConfig, subscription: str) -> dict[str, str]:
        if model.auth_mode == "api_key_env":
            value = os.environ.get(model.api_key_env, "").strip()
            if not value:
                raise ProviderError(f"Server environment variable {model.api_key_env} is not set.")
            if "\r" in value or "\n" in value:
                raise ProviderError("API key environment variables must contain a single-line value.")
            prefix = "Bearer " if model.api_key_header == "Authorization" else ""
            return {model.api_key_header: prefix + value}
        with self._lock:
            cached = self._tokens.get(subscription)
            if cached and cached[1] > time.time() + 120:
                token = cached[0]
            else:
                token, expires = self._get_azure_token(subscription)
                self._tokens[subscription] = (token, expires)
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _get_azure_token(subscription: str) -> tuple[str, float]:
        launcher = shutil.which("az")
        if not launcher:
            raise ProviderError("Azure CLI is missing. Install it and run az login.")
        args = [
            "account", "get-access-token", "--subscription", subscription,
            "--resource", "https://cognitiveservices.azure.com/",
            "--only-show-errors", "--output", "json",
        ]
        azure_python = Path(launcher).resolve().parent.parent / "python.exe"
        if os.name == "nt" and azure_python.is_file():
            command = [str(azure_python), "-I", "-m", "azure.cli", *args]
        elif os.name == "nt" and Path(launcher).suffix.lower() in (".cmd", ".bat"):
            command = [
                os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c",
                subprocess.list2cmdline([launcher, *args]),
            ]
        else:
            command = [launcher, *args]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8",
                timeout=60, check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProviderError("Azure sign-in could not complete. Run az login in a terminal.") from exc
        if result.returncode:
            detail = redact_error(result.stderr.strip())
            raise ProviderError(f"Azure authentication failed. Run az login. {detail}")
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError("Azure CLI returned an invalid token response.") from exc
        if not isinstance(data, dict) or not isinstance(data.get("accessToken"), str) or not data["accessToken"]:
            raise ProviderError("Azure CLI returned no access token.")
        # Without an expiry, use this token once instead of assuming it is cacheable.
        expiry = data.get("expires_on", 0)
        try:
            expires = float(expiry)
        except (ValueError, TypeError):
            expires = 0
        return data["accessToken"], expires


def build_request(model: ModelConfig, prompt: str, size: str, quality: str) -> dict[str, Any]:
    if size not in model.supported_sizes:
        raise ValidationError(f"{model.name} does not support the requested comparison size.")
    if quality not in QUALITIES:
        raise ValidationError("Unsupported GPT quality.")
    width, height = (int(part) for part in size.split("x"))
    if model.provider == "mai":
        body = {"model": model.deployment, "prompt": prompt, "width": width, "height": height}
        if model.id == "mai-image-2-6":
            body.update(auto_aspect_ratio=False, web_grounding=False)
        return body
    if model.provider == "flux" and "/providers/blackforestlabs/" in urllib.parse.urlsplit(model.endpoint).path:
        if model.id in ("flux-2-pro", "flux-2-flex"):
            body = {
                "model": model.deployment, "prompt": prompt, "width": width, "height": height,
                "num_images": 1, "output_format": "png",
            }
            if model.id == "flux-2-flex":
                body.update(steps=model.flux_steps, guidance=model.flux_guidance)
            return body
        return {
            "model": model.deployment, "prompt": prompt,
            "aspect_ratio": "1:1", "output_format": "png",
        }
    body: dict[str, Any] = {"prompt": prompt, "n": 1, "size": size}
    if "/deployments/" not in urllib.parse.urlsplit(model.endpoint).path:
        body["model"] = model.deployment
    if model.provider == "gpt":
        body.update(quality=quality, output_format="png")
    return body


def edit_endpoint(model: ModelConfig) -> str:
    parsed = urllib.parse.urlsplit(model.endpoint)
    if model.provider == "flux" and parsed.path in FLUX_PATHS.values():
        return model.endpoint
    if not parsed.path.endswith("/images/generations"):
        raise ValidationError(f"{model.name} has no supported image-editing route.")
    return urllib.parse.urlunsplit(parsed._replace(path=parsed.path.removesuffix("/generations") + "/edits"))


def build_edit_request(model: ModelConfig, prompt: str, size: str, quality: str) -> dict[str, Any]:
    body = build_request(model, prompt, size, quality)
    edit_endpoint(model)
    if model.provider == "mai":
        # The documented MAI edit contract takes model, prompt, and the uploaded image.
        return {key: value for key, value in body.items() if key not in ("width", "height")}
    return body


def multipart_edit(body: dict[str, Any], reference: bytes) -> tuple[bytes, str]:
    boundary = "ImageLab-" + uuid.uuid4().hex
    chunks = []
    for key, value in body.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValidationError("Invalid multipart field name.")
        chunks.extend((
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode("ascii"),
            (json.dumps(value) if isinstance(value, bool) else str(value)).encode("utf-8"),
            b"\r\n",
        ))
    chunks.extend((
        f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="reference.png"\r\nContent-Type: image/png\r\n\r\n'.encode("ascii"),
        reference, f"\r\n--{boundary}--\r\n".encode("ascii"),
    ))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def validate_image_url(url: str) -> None:
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ProviderError("The image service returned an invalid image URL.") from exc
    host = (parts.hostname or "").lower()
    trusted = bool(AZURE_HOST.fullmatch(host)) or any(
        host.endswith(suffix) and host != suffix[1:]
        for suffix in (".blob.core.windows.net", ".blob.storage.azure.net", ".bfl.ai", ".bfl.ml")
    )
    if (
        parts.scheme != "https" or not trusted or port not in (None, 443)
        or parts.username is not None or parts.password is not None
    ):
        raise ProviderError(
            "The service returned an image URL outside the supported Azure/BFL image hosts. "
            "No credentials were forwarded and no download was attempted."
        )


class _ImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        validate_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_response(response: Any, maximum: int) -> bytes:
    content = response.read(maximum + 1)
    if len(content) > maximum:
        raise ProviderError("The image service response exceeded the local size limit.")
    return content


def _http_error(exc: urllib.error.HTTPError, secrets: tuple[str, ...]) -> ProviderError:
    raw = exc.read(32768).decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    detail = ""
    if isinstance(payload, dict):
        error = payload.get("error", payload)
        if isinstance(error, dict):
            detail = str(error.get("message", error.get("code", "")))
        elif isinstance(error, str):
            detail = error
    if not detail:
        detail = str(exc.reason)
    hint = {
        401: " Check sign-in or the selected environment API key.",
        403: " Check the Entra data-plane role and network access. Account security settings were not changed.",
        404: " Check the full endpoint, API version, and deployment name.",
        429: " Rate limited; no automatic retry was made. Wait before starting a new run.",
    }.get(exc.code, "")
    return ProviderError(redact_error(f"HTTP {exc.code}: {detail}{hint}", secrets))


def image_info(content: bytes) -> tuple[str, int, int]:
    if content.startswith(b"\x89PNG\r\n\x1a\n") and len(content) >= 33 and content[12:16] == b"IHDR":
        width, height = struct.unpack(">II", content[16:24])
        if 0 < width <= 16384 and 0 < height <= 16384:
            return "png", width, height
    if content.startswith(b"\xff\xd8"):
        offset = 2
        frames = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
        while offset < len(content):
            if content[offset] != 0xFF:
                break
            while offset < len(content) and content[offset] == 0xFF:
                offset += 1
            if offset >= len(content):
                break
            marker = content[offset]
            offset += 1
            if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(content):
                break
            length = int.from_bytes(content[offset:offset + 2], "big")
            if length < 2 or offset + length > len(content):
                break
            if marker in frames and length >= 8:
                height, width = struct.unpack(">HH", content[offset + 3:offset + 7])
                if 0 < width <= 16384 and 0 < height <= 16384:
                    return "jpg", width, height
                break
            offset += length
    raise ProviderError("The response was not a supported PNG/JPEG image with valid dimensions.")


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    extension: str
    width: int
    height: int
    api_ms: float
    download_ms: float
    revised_prompt: str | None


def generate(
    model: ModelConfig, body: dict[str, Any], headers: dict[str, str], timeout: int,
) -> GeneratedImage:
    return _generate_or_edit(model, body, headers, timeout)


def edit(
    model: ModelConfig, body: dict[str, Any], headers: dict[str, str], timeout: int, reference: bytes,
) -> GeneratedImage:
    if not reference or not reference.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ProviderError("Editing requires a validated reference PNG; generation fallback is not allowed.")
    return _generate_or_edit(model, body, headers, timeout, reference)


def _generate_or_edit(
    model: ModelConfig, body: dict[str, Any], headers: dict[str, str], timeout: int,
    reference: bytes | None = None,
) -> GeneratedImage:
    secret_values = tuple(headers.values())
    started = time.perf_counter()
    deadline = time.monotonic() + timeout
    endpoint = model.endpoint
    content_type = "application/json; charset=utf-8"
    if reference is not None:
        endpoint = edit_endpoint(model)
        native_flux = model.provider == "flux" and urllib.parse.urlsplit(endpoint).path in FLUX_PATHS.values()
        if native_flux:
            payload = json.dumps(
                {**body, "input_image": base64.b64encode(reference).decode("ascii")}, ensure_ascii=False,
            ).encode("utf-8")
        else:
            payload, content_type = multipart_edit(body, reference)
    else:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=payload,
        headers={"Content-Type": content_type, **headers},
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = _read_response(response, MAX_RESPONSE_BYTES)
        api_ms = (time.perf_counter() - started) * 1000
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list) or not data["data"]:
            raise ProviderError("No image data returned. This adapter requires the synchronous Images API.")
        item = next(
            (row for row in data["data"] if isinstance(row, dict) and (row.get("b64_json") or row.get("url"))),
            None,
        )
        if item is None:
            raise ProviderError("The response did not contain data[].b64_json or data[].url.")
        download_ms = 0.0
        if isinstance(item.get("b64_json"), str) and item["b64_json"]:
            content = base64.b64decode(item["b64_json"], validate=True)
        elif isinstance(item.get("url"), str) and item["url"]:
            image_url = item["url"]
            validate_image_url(image_url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("No time remaining for image download.")
            download_started = time.perf_counter()
            # Signed image URLs are fetched without API keys or Entra headers.
            with urllib.request.build_opener(_ImageRedirect()).open(image_url, timeout=remaining) as response:
                content = _read_response(response, MAX_IMAGE_BYTES)
            download_ms = (time.perf_counter() - download_started) * 1000
        else:
            raise ProviderError("Image data has an unsupported type.")
        if len(content) > MAX_IMAGE_BYTES:
            raise ProviderError("The decoded image exceeded the local 32 MB image limit.")
        extension, width, height = image_info(content)
        revised = item.get("revised_prompt")
        return GeneratedImage(
            content, extension, width, height, round(api_ms, 3), round(download_ms, 3),
            revised if isinstance(revised, str) else None,
        )
    except urllib.error.HTTPError as exc:
        raise _http_error(exc, secret_values) from None
    except (TimeoutError, urllib.error.URLError) as exc:
        raise ProviderError(
            f"Network error or timeout (limit {timeout}s). The upstream request may still be billed. "
            + redact_error(str(exc), secret_values)
        ) from None
    except (json.JSONDecodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise ProviderError("The image service returned invalid JSON or base64 image data.") from exc
