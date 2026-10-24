import os
from typing import Any
from unittest import mock

import azure.cognitiveservices.speech
import pytest
import pytest_asyncio
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletion, ChatCompletionChunk
from openai.types.chat.chat_completion import (
    ChatCompletionMessage,
    Choice,
)

import app
import core
from approaches.chat import PersonaChatApproach
from approaches.promptmanager import PromptyManager

from .mocks import (
    MockAzureCredential,
    MockAzureCredentialExpired,
    mock_speak_text_cancelled,
    mock_speak_text_failed,
    mock_speak_text_success,
)


@pytest.fixture
def mock_speech_success(monkeypatch):
    monkeypatch.setattr(azure.cognitiveservices.speech.SpeechSynthesizer, "speak_text_async", mock_speak_text_success)


@pytest.fixture
def mock_speech_cancelled(monkeypatch):
    monkeypatch.setattr(azure.cognitiveservices.speech.SpeechSynthesizer, "speak_text_async", mock_speak_text_cancelled)


@pytest.fixture
def mock_speech_failed(monkeypatch):
    monkeypatch.setattr(azure.cognitiveservices.speech.SpeechSynthesizer, "speak_text_async", mock_speak_text_failed)


@pytest.fixture
def mock_openai_chatcompletion(monkeypatch):
    class AsyncChatCompletionIterator:
        def __init__(self, answer: str, reasoning: bool, usage: dict[str, Any]):
            chunk_id = "test-id"
            model = "gpt-4.1-mini" if not reasoning else "o3-mini"
            self.responses = [
                {"object": "chat.completion.chunk", "choices": [], "id": chunk_id, "model": model, "created": 1},
                {
                    "object": "chat.completion.chunk",
                    "choices": [{"delta": {"role": "assistant"}, "index": 0, "finish_reason": None}],
                    "id": chunk_id,
                    "model": model,
                    "created": 1,
                },
            ]
            # Split at << to simulate chunked responses
            if answer.find("<<") > -1:
                parts = answer.split("<<")
                self.responses.append(
                    {
                        "object": "chat.completion.chunk",
                        "choices": [
                            {
                                "delta": {"role": "assistant", "content": parts[0] + "<<"},
                                "index": 0,
                                "finish_reason": None,
                            }
                        ],
                        "id": chunk_id,
                        "model": model,
                        "created": 1,
                    }
                )
                self.responses.append(
                    {
                        "object": "chat.completion.chunk",
                        "choices": [
                            {"delta": {"role": "assistant", "content": parts[1]}, "index": 0, "finish_reason": None}
                        ],
                        "id": chunk_id,
                        "model": model,
                        "created": 1,
                    }
                )
                self.responses.append(
                    {
                        "object": "chat.completion.chunk",
                        "choices": [{"delta": {"role": None, "content": None}, "index": 0, "finish_reason": "stop"}],
                        "id": chunk_id,
                        "model": model,
                        "created": 1,
                    }
                )
            else:
                self.responses.append(
                    {
                        "object": "chat.completion.chunk",
                        "choices": [{"delta": {"content": answer}, "index": 0, "finish_reason": None}],
                        "id": chunk_id,
                        "model": model,
                        "created": 1,
                    }
                )

            self.responses.append(
                {
                    "object": "chat.completion.chunk",
                    "choices": [],
                    "id": chunk_id,
                    "model": model,
                    "created": 1,
                    "usage": usage,
                }
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.responses:
                return ChatCompletionChunk.model_validate(self.responses.pop(0))
            else:
                raise StopAsyncIteration

    async def mock_acreate(*args, **kwargs):
        # The only two possible values for seed:
        assert kwargs.get("seed") is None or kwargs.get("seed") == 42

        messages = kwargs["messages"]
        model = kwargs["model"]
        reasoning = model == "o3-mini"
        completion_usage: dict[str, any] = {
            "completion_tokens": 896,
            "prompt_tokens": 23,
            "total_tokens": 919,
            "completion_tokens_details": {
                "accepted_prediction_tokens": 0,
                "audio_tokens": 0,
                "reasoning_tokens": 384 if reasoning else 0,
                "rejected_prediction_tokens": 0,
            },
        }
        answer = "The capital of France is Paris. [Benefit_Options-2.pdf]."
        # Check if system prompt asks for followup questions
        for msg in messages:
            if msg.get("role") == "system":
                content = str(msg.get("content", ""))
                if "Generate 3 very brief follow-up questions" in content:
                    answer = (
                        "The capital of France is Paris. [Benefit_Options-2.pdf]. <<What is the capital of Spain?>>"
                    )
                    break
        if "stream" in kwargs and kwargs["stream"] is True:
            return AsyncChatCompletionIterator(answer, reasoning, completion_usage)
        else:
            return ChatCompletion(
                object="chat.completion",
                choices=[
                    Choice(
                        message=ChatCompletionMessage(role="assistant", content=answer), finish_reason="stop", index=0
                    )
                ],
                id="test-123",
                created=0,
                model="test-model",
                usage=CompletionUsage.model_validate(completion_usage),
            )

    def patch(openai_client):
        monkeypatch.setattr(openai_client.chat.completions, "create", mock_acreate)

    return patch


envs = [
    {
        "OPENAI_HOST": "azure",
        "AZURE_OPENAI_SERVICE": "test-openai-service",
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT": "test-chatgpt",
    },
]

reasoning_envs = [
    {
        "OPENAI_HOST": "azure",
        "AZURE_OPENAI_SERVICE": "test-openai-service",
        "AZURE_OPENAI_CHATGPT_MODEL": "o3-mini",
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT": "o3-mini",
    },
    {
        "OPENAI_HOST": "azure",
        "AZURE_OPENAI_SERVICE": "test-openai-service",
        "AZURE_OPENAI_CHATGPT_MODEL": "o3-mini",
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT": "o3-mini",
        "AZURE_OPENAI_REASONING_EFFORT": "low",
    },
]

auth_envs = [
    {
        "OPENAI_HOST": "azure",
        "AZURE_OPENAI_SERVICE": "test-openai-service",
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT": "test-chatgpt",
        "AZURE_USE_AUTHENTICATION": "true",
        "AZURE_ENFORCE_ACCESS_CONTROL": "true",
        "AZURE_SERVER_APP_ID": "SERVER_APP",
        "AZURE_SERVER_APP_SECRET": "SECRET",
        "AZURE_CLIENT_APP_ID": "CLIENT_APP",
        "AZURE_TENANT_ID": "TENANT_ID",
        "ADMIN_USER_OID": "OID_X",
    },
]

auth_public_envs = [
    {
        "OPENAI_HOST": "azure",
        "AZURE_OPENAI_SERVICE": "test-openai-service",
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT": "test-chatgpt",
        "AZURE_USE_AUTHENTICATION": "true",
        "AZURE_ENFORCE_ACCESS_CONTROL": "true",
        "AZURE_ENABLE_GLOBAL_DOCUMENT_ACCESS": "true",
        "AZURE_ENABLE_UNAUTHENTICATED_ACCESS": "true",
        "AZURE_SERVER_APP_ID": "SERVER_APP",
        "AZURE_SERVER_APP_SECRET": "SECRET",
        "AZURE_CLIENT_APP_ID": "CLIENT_APP",
        "AZURE_TENANT_ID": "TENANT_ID",
        "ADMIN_USER_OID": "OID_X",
    },
]


@pytest.fixture
def mock_knowledge_base(monkeypatch):
    loader = mock.Mock(return_value=[])
    monkeypatch.setattr(app, "load_knowledge_base", loader)
    return loader


@pytest.fixture(params=envs, ids=["client0"])
def mock_env(monkeypatch, request, mock_knowledge_base):
    with mock.patch.dict(os.environ, clear=True):
        monkeypatch.setenv("ENABLE_LANGUAGE_PICKER", "true")
        monkeypatch.setenv("USE_SPEECH_INPUT_BROWSER", "true")
        monkeypatch.setenv("USE_SPEECH_OUTPUT_AZURE", "true")
        monkeypatch.setenv("AZURE_SPEECH_SERVICE_ID", "test-id")
        monkeypatch.setenv("AZURE_SPEECH_SERVICE_LOCATION", "eastus")
        monkeypatch.setenv("AZURE_OPENAI_CHATGPT_MODEL", "gpt-4.1-mini")
        monkeypatch.setenv("ALLOWED_ORIGIN", "https://frontend.com")
        for key, value in request.param.items():
            monkeypatch.setenv(key, value)
        if os.getenv("AZURE_USE_AUTHENTICATION") is not None:
            monkeypatch.delenv("AZURE_USE_AUTHENTICATION")

        with mock.patch("app.AzureDeveloperCliCredential") as mock_default_azure_credential:
            mock_default_azure_credential.return_value = MockAzureCredential()
            yield


@pytest.fixture(params=reasoning_envs, ids=["reasoning_client0", "reasoning_client1"])
def mock_reasoning_env(monkeypatch, request, mock_knowledge_base):
    with mock.patch.dict(os.environ, clear=True):
        monkeypatch.setenv("ENABLE_LANGUAGE_PICKER", "true")
        monkeypatch.setenv("USE_SPEECH_INPUT_BROWSER", "true")
        monkeypatch.setenv("USE_SPEECH_OUTPUT_AZURE", "true")
        monkeypatch.setenv("AZURE_SPEECH_SERVICE_ID", "test-id")
        monkeypatch.setenv("AZURE_SPEECH_SERVICE_LOCATION", "eastus")
        monkeypatch.setenv("ALLOWED_ORIGIN", "https://frontend.com")
        monkeypatch.setenv("TEST_ENABLE_REASONING", "true")
        for key, value in request.param.items():
            monkeypatch.setenv(key, value)

        with mock.patch("app.AzureDeveloperCliCredential") as mock_default_azure_credential:
            mock_default_azure_credential.return_value = MockAzureCredential()
            yield


@pytest_asyncio.fixture(scope="function")
async def client(
    monkeypatch,
    mock_env,
    mock_openai_chatcompletion,
):
    quart_app = app.create_app()

    async with quart_app.test_app() as test_app:
        test_app.app.config.update({"TESTING": True})
        mock_openai_chatcompletion(test_app.app.config[app.CONFIG_OPENAI_CLIENT])
        yield test_app.test_client()


@pytest_asyncio.fixture(scope="function")
async def reasoning_client(
    monkeypatch,
    mock_reasoning_env,
    mock_openai_chatcompletion,
):
    quart_app = app.create_app()

    async with quart_app.test_app() as test_app:
        test_app.app.config.update({"TESTING": True})
        mock_openai_chatcompletion(test_app.app.config[app.CONFIG_OPENAI_CLIENT])
        yield test_app.test_client()


@pytest_asyncio.fixture(scope="function")
async def client_with_expiring_token(
    monkeypatch,
    mock_env,
    mock_openai_chatcompletion,
):
    quart_app = app.create_app()

    async with quart_app.test_app() as test_app:
        test_app.app.config.update({"TESTING": True})
        test_app.app.config.update({"azure_credential": MockAzureCredentialExpired()})
        mock_openai_chatcompletion(test_app.app.config[app.CONFIG_OPENAI_CLIENT])
        yield test_app.test_client()


@pytest_asyncio.fixture(params=auth_envs, scope="function")
async def auth_client(
    monkeypatch,
    mock_knowledge_base,
    mock_openai_chatcompletion,
    mock_validate_token_success,
    request,
):
    monkeypatch.setenv("AZURE_OPENAI_CHATGPT_MODEL", "gpt-4.1-mini")
    for key, value in request.param.items():
        monkeypatch.setenv(key, value)

    with mock.patch("app.AzureDeveloperCliCredential") as mock_default_azure_credential:
        mock_default_azure_credential.return_value = MockAzureCredential()
        quart_app = app.create_app()

        async with quart_app.test_app() as test_app:
            quart_app.config.update({"TESTING": True})
            mock_openai_chatcompletion(test_app.app.config[app.CONFIG_OPENAI_CLIENT])
            client = test_app.test_client()
            client.config = quart_app.config

            yield client


@pytest_asyncio.fixture(params=auth_public_envs, scope="function")
async def auth_public_documents_client(
    monkeypatch,
    mock_knowledge_base,
    mock_openai_chatcompletion,
    mock_validate_token_success,
    request,
):
    monkeypatch.setenv("AZURE_OPENAI_CHATGPT_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("USE_CHAT_HISTORY_COSMOS", "true")
    monkeypatch.setenv("AZURE_COSMOSDB_ACCOUNT", "test-cosmosdb-account")
    monkeypatch.setenv("AZURE_CHAT_HISTORY_DATABASE", "test-cosmosdb-database")
    monkeypatch.setenv("AZURE_ADMIN_SETTINGS_DATABASE", "")
    monkeypatch.setenv("AZURE_CHAT_HISTORY_CONTAINER", "test-cosmosdb-container")
    monkeypatch.setenv("AZURE_CHAT_HISTORY_VERSION", "cosmosdb-v2")

    for key, value in request.param.items():
        monkeypatch.setenv(key, value)

    with mock.patch("app.AzureDeveloperCliCredential") as mock_default_azure_credential:
        mock_default_azure_credential.return_value = MockAzureCredential()
        quart_app = app.create_app()

        async with quart_app.test_app() as test_app:
            quart_app.config.update({"TESTING": True})
            mock_openai_chatcompletion(test_app.app.config[app.CONFIG_OPENAI_CLIENT])
            client = test_app.test_client()
            client.config = quart_app.config

            yield client


@pytest.fixture
def mock_validate_token_success(monkeypatch):
    import jwt as pyjwt

    original_decode = pyjwt.decode

    def mock_jwt_decode(token, *args, **kwargs):
        # If the token isn't a real JWT (e.g. "MockToken"), return a mock payload
        if isinstance(token, str) and token.count(".") < 2:
            return {
                "iss": "https://login.microsoftonline.com/TENANT_ID/v2.0",
                "aud": "CLIENT_APP",
                "tid": "TENANT_ID",
                "oid": "OID_X",
                "sub": "test-subject",
                "nonce": "test-nonce",
                "iat": 1,
                "exp": 9999999999,
                "preferred_username": "test@example.com",
                "name": "Test User",
            }
        return original_decode(token, *args, **kwargs)

    monkeypatch.setattr(pyjwt, "decode", mock_jwt_decode)

    async def mock_validate_access_token(self, token):
        return False

    monkeypatch.setattr(core.authentication.AuthenticationHelper, "validate_access_token", mock_validate_access_token)


@pytest.fixture
def chat_approach():
    return PersonaChatApproach(
        openai_client=None,
        chatgpt_model="gpt-4.1-mini",
        chatgpt_deployment="chat",
        prompt_manager=PromptyManager(),
        knowledge_sources=["Test knowledge content."],
    )
