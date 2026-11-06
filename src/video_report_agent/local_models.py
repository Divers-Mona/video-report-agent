"""Resolve OpenAI-compatible local models without coupling runs to one model ID."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"
DEFAULT_CONTEXT_WINDOW = 32_768
DEFAULT_MAX_TOKENS = 4_096


class LocalModelError(ValueError):
    """A local OpenAI-compatible service or its model selection is unusable."""


def local_base_url(value: str | None = None) -> str:
    raw = (value if value is not None else os.getenv("PI_BASE_URL")) or DEFAULT_LOCAL_BASE_URL
    raw = raw.strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and parsed.port != 0
        )
    except ValueError:
        valid = False
    if not valid:
        raise LocalModelError(
            "PI_BASE_URL must be an HTTP(S) base URL without credentials or query"
        )
    return raw


def discover_models(*, base_url: str | None = None, api_key: str | None = None) -> list[str]:
    """Read model IDs from the standard OpenAI-compatible GET /models endpoint."""
    base = local_base_url(base_url)
    headers = {}
    key = (api_key if api_key is not None else os.getenv("PI_API_KEY", "")).strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = httpx.get(f"{base}/models", headers=headers, timeout=10.0)
    except httpx.HTTPError as exc:
        raise LocalModelError(
            f"Could not reach the configured model service ({type(exc).__name__})"
        ) from exc
    if response.is_error:
        raise LocalModelError(f"The model service returned HTTP {response.status_code} for /models")
    try:
        payload = response.json()
    except ValueError as exc:
        raise LocalModelError("The model service returned invalid JSON for /models") from exc
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise LocalModelError("The model service /models response has no data list")
    model_ids = []
    seen = set()
    for row in rows:
        model_id = row.get("id") if isinstance(row, dict) else None
        safe_id = (
            isinstance(model_id, str)
            and model_id.strip()
            and len(model_id) <= 512
            and all(ord(character) >= 32 and ord(character) != 127 for character in model_id)
        )
        if safe_id and model_id not in seen:
            model_ids.append(model_id)
            seen.add(model_id)
    return model_ids


def resolve_model_selection(
    model_override: str | None = None,
    *,
    default_provider: str = "deepseek",
    provider_override: str | None = None,
) -> dict[str, str] | None:
    """Resolve a model for a run; discover only when local selection is omitted.

    ``provider_override`` (CLI ``--provider``) replaces PI_PROVIDER and ignores
    PI_MODEL, which belongs to the environment's provider. A non-local override
    therefore needs an explicit model.
    """
    if provider_override is not None:
        provider = provider_override.strip()
        explicit_model = (model_override or "").strip()
        if not provider:
            raise LocalModelError("--provider must not be empty")
        if provider != "local" and not explicit_model:
            raise LocalModelError(f"--provider {provider} requires --model")
        if explicit_model:
            return {"provider": provider, "model": explicit_model}
        return _discover_single_model(provider)
    provider = (os.getenv("PI_PROVIDER") or default_provider).strip()
    explicit_model = (model_override or os.getenv("PI_MODEL") or "").strip()
    if provider != "local":
        return {"provider": provider, "model": explicit_model} if model_override else None
    if explicit_model:
        return {"provider": provider, "model": explicit_model}
    return _discover_single_model(provider)


def _discover_single_model(provider: str) -> dict[str, str]:
    model_ids = discover_models()
    if not model_ids:
        raise LocalModelError("The model service returned no model IDs; set PI_MODEL explicitly")
    if len(model_ids) > 1:
        choices = "\n".join(f"  - {model_id}" for model_id in model_ids)
        raise LocalModelError(
            "The model service exposes multiple models. Choose one with PI_MODEL or --model:\n"
            f"{choices}"
        )
    return {"provider": provider, "model": model_ids[0]}


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise LocalModelError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise LocalModelError(f"{name} must be a positive integer")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise LocalModelError(f"{name} must be true or false")


def local_provider_config(model_id: str, *, base_url: str | None = None) -> dict:
    context_window = _positive_int("PI_CONTEXT_WINDOW", DEFAULT_CONTEXT_WINDOW)
    max_tokens = _positive_int("PI_MAX_TOKENS", DEFAULT_MAX_TOKENS)
    if max_tokens > context_window:
        raise LocalModelError("PI_MAX_TOKENS cannot exceed PI_CONTEXT_WINDOW")
    return {
        "baseUrl": local_base_url(base_url),
        "api": "openai-completions",
        "apiKey": "$PI_API_KEY",
        "compat": {
            "supportsStore": False,
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": _boolean("PI_SUPPORTS_REASONING_EFFORT"),
            "maxTokensField": "max_tokens",
        },
        "models": [
            {
                "id": model_id,
                "name": model_id,
                "input": ["text"],
                "reasoning": _boolean("PI_REASONING"),
                "contextWindow": context_window,
                "maxTokens": max_tokens,
            }
        ],
    }


def configure_local_provider(
    models_path: Path, model_id: str, *, base_url: str | None = None
) -> None:
    """Merge the chosen local model while retaining other models and providers."""
    if not model_id.strip():
        raise LocalModelError("A local model ID is required")
    if models_path.is_file():
        try:
            payload = json.loads(models_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalModelError("Pi's models.json could not be read") from exc
    else:
        payload = {}
    if not isinstance(payload, dict):
        raise LocalModelError("Pi's models.json must contain an object")
    providers = payload.setdefault("providers", {})
    if not isinstance(providers, dict):
        raise LocalModelError("Pi's models.json providers field must contain an object")
    existing = providers.get("local", {})
    if not isinstance(existing, dict):
        raise LocalModelError("Pi's local provider must contain an object")
    models = existing.get("models", [])
    if not isinstance(models, list) or any(not isinstance(model, dict) for model in models):
        raise LocalModelError("Pi's local provider models must contain a list of objects")
    updated = local_provider_config(model_id, base_url=base_url)
    selected = updated["models"][0]
    merged = [
        {**model, **selected} if model.get("id") == model_id else model
        for model in models
    ]
    if not any(model.get("id") == model_id for model in models):
        merged.append(selected)
    providers["local"] = {**existing, **updated, "models": merged}
    models_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = models_path.with_suffix(models_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(models_path)
