import json

import pytest

import video_report_agent.local_models as local_models


class Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.is_error = status_code >= 400

    def json(self):
        return self.payload


def test_discovery_reads_only_model_ids_and_uses_optional_bearer_key(monkeypatch):
    requests = []

    def get(url, *, headers, timeout):
        requests.append((url, headers, timeout))
        return Response({"data": [{"id": "model-a"}, {"id": "model-a"}, {"name": "ignored"}]})

    monkeypatch.setenv("PI_BASE_URL", "http://127.0.0.1:8080/v1/")
    monkeypatch.setenv("PI_API_KEY", "local-test-key")
    monkeypatch.setattr(local_models.httpx, "get", get)

    assert local_models.discover_models() == ["model-a"]
    assert requests == [
        ("http://127.0.0.1:8080/v1/models", {"Authorization": "Bearer local-test-key"}, 10.0)
    ]


def test_discovery_filters_terminal_control_characters(monkeypatch):
    monkeypatch.setattr(
        local_models.httpx,
        "get",
        lambda *args, **kwargs: Response({"data": [{"id": "good"}, {"id": "bad\nmodel"}]}),
    )
    assert local_models.discover_models(base_url="http://127.0.0.1:8080/v1", api_key="") == [
        "good"
    ]


def test_explicit_model_skips_discovery(monkeypatch):
    monkeypatch.setenv("PI_PROVIDER", "local")
    monkeypatch.delenv("PI_MODEL", raising=False)
    monkeypatch.setattr(
        local_models,
        "discover_models",
        lambda **kwargs: pytest.fail("explicit selections do not discover /models"),
    )
    assert local_models.resolve_model_selection("chosen-id") == {
        "provider": "local",
        "model": "chosen-id",
    }


def test_single_model_is_discovered_each_run(monkeypatch):
    calls = []
    monkeypatch.setenv("PI_PROVIDER", "local")
    monkeypatch.delenv("PI_MODEL", raising=False)
    monkeypatch.setattr(
        local_models, "discover_models", lambda **kwargs: calls.append(kwargs) or ["available"]
    )

    assert local_models.resolve_model_selection() == {
        "provider": "local",
        "model": "available",
    }
    assert local_models.resolve_model_selection() == {
        "provider": "local",
        "model": "available",
    }
    assert len(calls) == 2


def test_multiple_models_require_explicit_selection(monkeypatch):
    monkeypatch.setenv("PI_PROVIDER", "local")
    monkeypatch.delenv("PI_MODEL", raising=False)
    monkeypatch.setattr(local_models, "discover_models", lambda **kwargs: ["one", "two"])

    with pytest.raises(local_models.LocalModelError) as error:
        local_models.resolve_model_selection()
    assert "one" in str(error.value) and "two" in str(error.value)


def test_non_local_provider_keeps_existing_selection_path(monkeypatch):
    monkeypatch.setenv("PI_PROVIDER", "deepseek")
    monkeypatch.setenv("PI_MODEL", "configured-model")
    monkeypatch.setattr(
        local_models,
        "discover_models",
        lambda **kwargs: pytest.fail("non-local providers do not discover /models"),
    )

    assert local_models.resolve_model_selection() is None
    assert local_models.resolve_model_selection("override") == {
        "provider": "deepseek",
        "model": "override",
    }


def test_model_file_merge_preserves_other_providers_and_auth(tmp_path, monkeypatch):
    models_path = tmp_path / "models.json"
    original = {
        "providers": {"other": {"api": "openai-responses", "models": [{"id": "kept"}]}},
        "custom": "preserved",
    }
    models_path.write_text(json.dumps(original), encoding="utf-8")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"other":{"type":"api_key","key":"kept-secret"}}', encoding="utf-8")
    auth_before = auth_path.read_bytes()
    monkeypatch.setenv("PI_BASE_URL", "http://127.0.0.1:8080/v1")
    monkeypatch.setenv("PI_CONTEXT_WINDOW", "262144")
    monkeypatch.setenv("PI_MAX_TOKENS", "32768")
    monkeypatch.setenv("PI_REASONING", "true")
    monkeypatch.setenv("PI_SUPPORTS_REASONING_EFFORT", "false")
    monkeypatch.setenv("PI_API_KEY", "secret-that-must-not-be-saved")

    local_models.configure_local_provider(models_path, "model-id")
    saved = json.loads(models_path.read_text(encoding="utf-8"))

    assert saved["custom"] == "preserved"
    assert saved["providers"]["other"] == original["providers"]["other"]
    provider = saved["providers"]["local"]
    assert provider["baseUrl"] == "http://127.0.0.1:8080/v1"
    assert provider["apiKey"] == "$PI_API_KEY"
    assert provider["models"] == [
        {
            "id": "model-id",
            "name": "model-id",
            "input": ["text"],
            "reasoning": True,
            "contextWindow": 262144,
            "maxTokens": 32768,
        }
    ]
    assert provider["compat"]["supportsDeveloperRole"] is False
    assert provider["compat"]["supportsReasoningEffort"] is False
    assert "secret-that-must-not-be-saved" not in models_path.read_text(encoding="utf-8")
    assert auth_path.read_bytes() == auth_before


@pytest.mark.parametrize(
    "base_url",
    ["file:///tmp/models", "http://user:secret@localhost/v1", "http://localhost/v1?token=x"],
)
def test_invalid_base_url_is_rejected_without_echoing_credentials(base_url):
    with pytest.raises(local_models.LocalModelError) as error:
        local_models.local_base_url(base_url)
    assert "secret" not in str(error.value)
    assert "token" not in str(error.value)
