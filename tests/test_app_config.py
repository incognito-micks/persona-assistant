import html
import json
import os
from unittest import mock

import pytest
from quart.testing.app import LifespanError

import app
from core import knowledgebase
from tests.mocks import MockAzureCredential


@pytest.fixture
def minimal_env(monkeypatch, mock_knowledge_base):
    with mock.patch.dict(os.environ, clear=True):
        monkeypatch.setenv("AZURE_OPENAI_SERVICE", "test-openai-service")
        monkeypatch.setenv("AZURE_OPENAI_CHATGPT_MODEL", "gpt-4.1-mini")
        monkeypatch.setenv("AZURE_OPENAI_CHATGPT_DEPLOYMENT", "test-chat-deployment")

        with mock.patch("app.AzureDeveloperCliCredential") as mock_cred:
            mock_cred.return_value = MockAzureCredential()
            yield


@pytest.mark.asyncio
async def test_app_loads_packaged_knowledge(monkeypatch, minimal_env, tmp_path):
    backend_dir = tmp_path / "deployment" / "backend"
    data_dir = backend_dir / "data" / "work"
    data_dir.mkdir(parents=True)
    (data_dir / "profile.md").write_text("Packaged knowledge.", encoding="utf-8")
    monkeypatch.setattr(knowledgebase, "__file__", str(backend_dir / "core" / "knowledgebase.py"))
    monkeypatch.setattr(app, "load_knowledge_base", knowledgebase.load_knowledge_base)

    quart_app = app.create_app()
    async with quart_app.test_app():
        assert quart_app.config[app.CONFIG_KNOWLEDGE_SOURCES] == ["[profile]: Packaged knowledge."]


@pytest.mark.asyncio
async def test_app_respects_knowledge_path_override(monkeypatch, minimal_env, mock_knowledge_base, tmp_path):
    configured_path = str(tmp_path / "custom-knowledge")
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", configured_path)
    quart_app = app.create_app()
    async with quart_app.test_app():
        mock_knowledge_base.assert_called_once_with(configured_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_app_rejects_missing_or_empty_knowledge(monkeypatch, minimal_env, tmp_path, missing):
    data_dir = tmp_path / "knowledge"
    if not missing:
        data_dir.mkdir()
    monkeypatch.setenv("KNOWLEDGE_DATA_DIR", str(data_dir))
    monkeypatch.setattr(app, "load_knowledge_base", knowledgebase.load_knowledge_base)
    quart_app = app.create_app()
    with pytest.raises(LifespanError, match="Required knowledge base"):
        async with quart_app.test_app():
            pass


@pytest.mark.asyncio
async def test_app_azure_custom_key(monkeypatch, minimal_env):
    monkeypatch.setenv("AZURE_OPENAI_CUSTOM_URL", "http://azureapi.com/api/v1")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY_OVERRIDE", "azure-api-key")

    quart_app = app.create_app()
    async with quart_app.test_app():
        assert quart_app.config[app.CONFIG_OPENAI_CLIENT].api_key == "azure-api-key"
        assert str(quart_app.config[app.CONFIG_OPENAI_CLIENT].base_url) == "http://azureapi.com/api/v1/openai/"


@pytest.mark.asyncio
async def test_app_azure_custom_identity(monkeypatch, minimal_env):
    monkeypatch.setenv("AZURE_OPENAI_CUSTOM_URL", "http://azureapi.com/api/v1")

    quart_app = app.create_app()
    async with quart_app.test_app():
        openai_client = quart_app.config[app.CONFIG_OPENAI_CLIENT]
        assert str(openai_client.base_url) == "http://azureapi.com/api/v1/openai/"


@pytest.mark.asyncio
async def test_app_config_default(monkeypatch, minimal_env):
    quart_app = app.create_app()
    async with quart_app.test_app() as test_app:
        client = test_app.test_client()
        response = await client.get("/config")
        assert response.status_code == 200
        result = await response.get_json()
        assert result["streamingEnabled"] is True
        assert test_app.app.config[app.CONFIG_CHAT_APPROACH].chatgpt_context_window == 16384


@pytest.mark.asyncio
async def test_app_respects_context_window(monkeypatch, minimal_env):
    monkeypatch.setenv("AZURE_OPENAI_CHATGPT_CONTEXT_WINDOW", "16000")
    quart_app = app.create_app()
    async with quart_app.test_app() as test_app:
        assert test_app.app.config[app.CONFIG_CHAT_APPROACH].chatgpt_context_window == 16000


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "-1", "1024", "not-a-number"])
async def test_app_rejects_invalid_context_window(monkeypatch, minimal_env, value):
    monkeypatch.setenv("AZURE_OPENAI_CHATGPT_CONTEXT_WINDOW", value)
    with pytest.raises(LifespanError):
        async with app.create_app().test_app():
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["Jamie Example", "Taylor <Example> & Co"])
async def test_second_persona_needs_only_profile_configuration(monkeypatch, minimal_env, tmp_path, owner):
    source = tmp_path / "profile.json"
    source.write_text(
        json.dumps(
            {
                "owner_name": owner,
                "assistant_name": f"{owner}'s assistant",
                "site_description": f"Learn about {owner}.",
                "example_questions": [f"What does {owner} build?"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("PROFILE_PATH", str(source))
    async with app.create_app().test_app() as application:
        client = application.test_client()
        response = await client.get("/api/status")
        profile = await response.get_json()
        assert profile["owner_name"] == owner
        assert profile["example_questions"] == [f"What does {owner} build?"]
        assert "manager_email" not in profile
        response = await client.get("/")
        document = (await response.get_data()).decode()
        assert html.escape(f"{owner}'s assistant", quote=True) in document
        assert "__PROFILE_TITLE__" not in document
        assert (await (await client.get("/auth_setup_admin")).get_json()) == {"enabled": False}


@pytest.mark.asyncio
async def test_invalid_public_kill_switch_fails_at_startup(monkeypatch, minimal_env):
    monkeypatch.setenv("DISABLE_PUBLIC_ACCESS", "maybe")
    with pytest.raises(LifespanError, match="DISABLE_PUBLIC_ACCESS"):
        async with app.create_app().test_app():
            pass


@pytest.mark.asyncio
async def test_app_config_for_client(client):
    response = await client.get("/config")
    assert response.status_code == 200
    result = await response.get_json()
    assert result["streamingEnabled"] is True


@pytest.mark.asyncio
async def test_app_config_for_reasoning(monkeypatch, minimal_env):
    monkeypatch.setenv("AZURE_OPENAI_CHATGPT_MODEL", "o3-mini")
    monkeypatch.setenv("AZURE_OPENAI_CHATGPT_DEPLOYMENT", "o3-mini")
    quart_app = app.create_app()
    async with quart_app.test_app() as test_app:
        client = test_app.test_client()
        response = await client.get("/config")
        assert response.status_code == 200
        result = await response.get_json()
        assert result["streamingEnabled"] is True


@pytest.mark.asyncio
async def test_app_config_for_reasoning_without_streaming(monkeypatch, minimal_env):
    monkeypatch.setenv("AZURE_OPENAI_CHATGPT_MODEL", "o1")
    monkeypatch.setenv("AZURE_OPENAI_CHATGPT_DEPLOYMENT", "o1")
    quart_app = app.create_app()
    async with quart_app.test_app() as test_app:
        client = test_app.test_client()
        response = await client.get("/config")
        assert response.status_code == 200
        result = await response.get_json()
        assert result["streamingEnabled"] is False


def test_app_enables_azure_monitor_when_connection_string_set(monkeypatch):
    mock_connection_string = "InstrumentationKey=12345678-1234-1234-1234-123456789012"
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", mock_connection_string)
    configure = mock.Mock()
    aiohttp_instrumentor = mock.Mock()
    httpx_instrumentor = mock.Mock()
    middleware = mock.Mock(side_effect=lambda application: application)
    monkeypatch.setattr(app, "configure_azure_monitor", configure)
    monkeypatch.setattr(app, "AioHttpClientInstrumentor", aiohttp_instrumentor)
    monkeypatch.setattr(app, "HTTPXClientInstrumentor", httpx_instrumentor)
    monkeypatch.setattr(app, "OpenTelemetryMiddleware", middleware)

    app.create_app()

    configure.assert_called_once_with(
        instrumentation_options={
            "django": {"enabled": False},
            "psycopg2": {"enabled": False},
            "fastapi": {"enabled": False},
        }
    )
    aiohttp_instrumentor.return_value.instrument.assert_called_once_with()
    httpx_instrumentor.return_value.instrument.assert_called_once_with()
    middleware.assert_called_once()
