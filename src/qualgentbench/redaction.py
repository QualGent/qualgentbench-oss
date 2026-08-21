"""Turn a codex `--json` transcript into Test Run evidence (steps + screenshots).
The transcript inlines every MCP tool reply, so we harvest byte-identical step rows
to the product's telemetry bridge — the existing viewer needs zero new UI."""

from __future__ import annotations

import base64
import re
from typing import Any

# Tools whose calls become Test Run steps — mirrors the recorded-step rule.
_ROUTINE_TELEMETRY_TOOLS = {"apply_skill", "apply_routine"}
_REDACTED = "[REDACTED]"

# Faithful port of the SENSITIVE_TELEMETRY_* rules so credentials never reach
# test_runs.result. `text`/`value` cover typed passwords.
_SENSITIVE_KEY_RE = re.compile(
    r"(?:secret|token|credential|api[_-]?key|authorization|access[_-]?token|"
    r"refresh[_-]?token|test[_-]?case(?:[_-]?(?:id|uuid))?|"
    r"(?:image|screenshot)[_-]?(?:data|base64)|signed(?:[_-]?stream)?[_-]?url|"
    r"endpoint[_-]?websocket[_-]?url|target[_-]?http[_-]?port[_-]?url[_-]?prefix)",
    re.IGNORECASE,
)
_SENSITIVE_KEYS = {"password", "passcode", "pin", "text", "value"}
_SIGNED_URL_RE = re.compile(
    r"\bhttps?://[^\s\"',}<>\]]*[?&](?:AWSAccessKeyId|Signature|X-Amz-Algorithm|"
    r"X-Amz-Credential|X-Amz-Security-Token|X-Amz-Signature|access_token|api_key|"
    r"refresh_token|se|sig|sp|st|sv|token)=[^\s\"',}<>\]]+",
    re.IGNORECASE,
)
_WEBSOCKET_URL_RE = re.compile(r"\bwss?://[^\s\"',}<>\]]+", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_KV_SECRET_RE = re.compile(
    r"\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*\S+",
    re.IGNORECASE,
)


def _is_sensitive_key(key: str) -> bool:
    norm = re.sub(r"[-\s]", "_", key.lower())
    return norm in _SENSITIVE_KEYS or bool(_SENSITIVE_KEY_RE.search(norm))


def _redact_value(value: Any, key: str = "") -> tuple[Any, bool]:
    if key and _is_sensitive_key(key):
        return _REDACTED, True
    if isinstance(value, list):
        redacted = False
        out = []
        for entry in value:
            v, r = _redact_value(entry)
            redacted = redacted or r
            out.append(v)
        return out, redacted
    if isinstance(value, dict):
        redacted = False
        out: dict[str, Any] = {}
        for k, v in value.items():
            nv, r = _redact_value(v, k)
            redacted = redacted or r
            out[k] = nv
        return out, redacted
    return value, False


def _redact_record(record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    value, redacted = _redact_value(record)
    return (value if isinstance(value, dict) else {}), redacted


def _collect_sensitive_strings(value: Any, key: str = "", out: set[str] | None = None) -> set[str]:
    if out is None:
        out = set()
    if key and _is_sensitive_key(key):
        _collect_all_strings(value, out)
        return out
    if isinstance(value, list):
        for entry in value:
            _collect_sensitive_strings(entry, "", out)
    elif isinstance(value, dict):
        for k, v in value.items():
            _collect_sensitive_strings(v, k, out)
    return out


def _collect_all_strings(value: Any, out: set[str]) -> None:
    if isinstance(value, str):
        out.add(value)
    elif isinstance(value, list):
        for entry in value:
            _collect_all_strings(entry, out)
    elif isinstance(value, dict):
        for entry in value.values():
            _collect_all_strings(entry, out)


def _sanitize_summary(value: str, args: dict[str, Any]) -> str:
    sanitized = _SIGNED_URL_RE.sub("[REDACTED_URL]", value)
    sanitized = _WEBSOCKET_URL_RE.sub("[REDACTED_URL]", sanitized)
    sanitized = _BEARER_RE.sub("Bearer [REDACTED]", sanitized)
    sanitized = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED]", sanitized)
    for secret in _collect_sensitive_strings(args):
        if len(secret) < 4:
            continue
        sanitized = sanitized.replace(secret, _REDACTED)
    return sanitized


def _sanitize_payload(value: Any, args: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _sanitize_summary(value, args)
    if isinstance(value, list):
        return [_sanitize_payload(v, args) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_payload(v, args) for k, v in value.items()}
    return value


# Long base64 blobs are the screenshots (stored separately) — strip them so the
# stored transcript stays small and readable.
_B64_IMAGE_RE = re.compile(r'("data"\s*:\s*")[A-Za-z0-9+/]{200,}={0,2}(")')
_SK_KEY_RE = re.compile(r"sk-[A-Za-z0-9._-]{16,}")


def extract_mcp_images(content: list[dict[str, Any]]) -> list[tuple[bytes, str]]:
    """Decode base64 JPEG/PNG image blocks from an MCP reply's content array."""
    images: list[tuple[bytes, str]] = []
    for block in content or []:
        if block.get("type") != "image" or not isinstance(block.get("data"), str):
            continue
        mime = block.get("mimeType") or block.get("mime_type")
        if mime not in ("image/jpeg", "image/png"):
            continue
        try:
            data = base64.b64decode(block["data"], validate=True)
        except (ValueError, TypeError):
            continue
        if data:
            images.append((data, mime))
    return images


# ── bare (n-dl) arm ───────────────────────────────────────────────────────────
# The bare agent shells `adb shell input …`; the adb shim snapshots after each
# input action, and pairing the k-th action with shot k rebuilds the same rows.
_BARE_INPUT_RE = re.compile(r"shell\s+input\s+([a-zA-Z]+)\s*([^;&|\n']*)")
