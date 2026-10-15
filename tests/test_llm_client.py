from types import SimpleNamespace

import pytest

import llm_client
from fakes import FakeClient


def test_chat_json_parses_the_reply_and_sends_json_mode():
    client = FakeClient([{"ok": True}])
    assert llm_client.chat_json(client, "hi", deployment="dep") == {"ok": True}
    call = client.calls[0]
    assert call["model"] == "dep" and call["response_format"] == {"type": "json_object"}
    assert call["messages"][-1]["content"] == "hi"


def test_chat_json_attaches_an_image():
    client = FakeClient([{"ok": True}])
    llm_client.chat_json(client, "look", image_b64="QUJD", deployment="dep")
    content = client.calls[0]["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


@pytest.mark.parametrize("reply", [None, ""])
def test_chat_json_rejects_an_empty_reply(reply):
    with pytest.raises(RuntimeError):
        llm_client.chat_json(FakeClient([reply]), "hi", deployment="dep")


def test_chat_json_rejects_invalid_json():
    with pytest.raises(ValueError):
        llm_client.chat_json(FakeClient(["not json"]), "hi", deployment="dep")


def _patch_azure(monkeypatch, created):
    monkeypatch.setattr(llm_client, "DefaultAzureCredential", lambda: "cred")
    monkeypatch.setattr(llm_client, "get_bearer_token_provider", lambda cred, scope: "provider")
    monkeypatch.setattr(llm_client, "AzureOpenAI", lambda **kw: created.append(("azure", kw)) or "azure-client")
    monkeypatch.setattr(llm_client, "OpenAI", lambda **kw: created.append(("v1", kw)) or "v1-client")


def test_make_client_keeps_the_legacy_path_when_api_version_is_set(monkeypatch):
    created = []
    _patch_azure(monkeypatch, created)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")
    assert llm_client.make_client() == "azure-client"
    assert created[0][1]["api_version"] == "2025-04-01-preview"
    assert created[0][1]["azure_endpoint"] == "https://x.openai.azure.com/"


def test_make_client_uses_the_v1_route_without_api_version(monkeypatch):
    created = []
    _patch_azure(monkeypatch, created)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
    assert llm_client.make_client() == "v1-client"
    assert created[0][1]["base_url"] == "https://x.openai.azure.com/openai/v1/"
    assert created[0][1]["api_key"] == "provider"


AZURE_PATHS = [("2025-04-01-preview", "azure"), (None, "v1")]


def _set_endpoint(monkeypatch, api_version):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://x.openai.azure.com/")
    if api_version:
        monkeypatch.setenv("AZURE_OPENAI_API_VERSION", api_version)
    else:
        monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)


@pytest.mark.parametrize("api_version, kind", AZURE_PATHS)
def test_make_client_requests_a_cognitive_services_token(monkeypatch, api_version, kind):
    created, requested = [], []
    _patch_azure(monkeypatch, created)
    monkeypatch.setattr(llm_client, "get_bearer_token_provider",
                        lambda cred, scope: requested.append((cred, scope)) or "provider")
    _set_endpoint(monkeypatch, api_version)
    llm_client.make_client()
    assert requested == [("cred", "https://cognitiveservices.azure.com/.default")]
    assert created[0][0] == kind
    token_key = "azure_ad_token_provider" if kind == "azure" else "api_key"
    assert created[0][1][token_key] == "provider"


@pytest.mark.parametrize("api_version, kind", AZURE_PATHS)
def test_make_client_passes_the_timeout_and_retries_on_both_paths(monkeypatch, api_version, kind):
    created = []
    _patch_azure(monkeypatch, created)
    _set_endpoint(monkeypatch, api_version)
    llm_client.make_client()
    llm_client.make_client(timeout=42.0)
    assert [c[0] for c in created] == [kind, kind]
    assert [c[1]["timeout"] for c in created] == [300.0, 42.0]
    assert [c[1]["max_retries"] for c in created] == [5, 5]


def test_chat_json_defaults_to_gpt_5_mini_with_low_reasoning(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_GPT_DEPLOYMENT", raising=False)
    client = FakeClient([{"ok": True}])
    llm_client.chat_json(client, "hi")
    assert client.calls[0]["model"] == "gpt-5-mini"
    assert client.calls[0]["reasoning_effort"] == "low"


def test_chat_json_reads_the_deployment_from_the_environment(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_GPT_DEPLOYMENT", "my-deployment")
    client = FakeClient([{"ok": True}, {"ok": True}])
    llm_client.chat_json(client, "hi")
    llm_client.chat_json(client, "hi", deployment="explicit")
    assert [c["model"] for c in client.calls] == ["my-deployment", "explicit"]
