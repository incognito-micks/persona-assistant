import asyncio
import dataclasses
import html
import json
import logging
import mimetypes
import os
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

from azure.identity.aio import (
    AzureDeveloperCliCredential,
    ManagedIdentityCredential,
    get_bearer_token_provider,
)
from azure.monitor.opentelemetry import configure_azure_monitor
from openai import AsyncAzureOpenAI, AsyncOpenAI
from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from quart import (
    Blueprint,
    Quart,
    current_app,
    jsonify,
    make_response,
    request,
    send_from_directory,
)
from quart_cors import cors
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge, ServiceUnavailable
from werkzeug.local import LocalProxy

from admin.settings_cosmosdb import admin_settings_bp, get_current_settings
from approaches.approach import Approach, DataPoints, ExtraInfo
from approaches.chat import PersonaChatApproach
from approaches.promptmanager import PromptyManager
from chat_history.cosmosdb import chat_history_cosmosdb_bp
from config import (
    CONFIG_ADMIN_USER_OID,
    CONFIG_AUTH_CLIENT,
    CONFIG_CHAT_APPROACH,
    CONFIG_CHAT_HISTORY_BROWSER_ENABLED,
    CONFIG_CHAT_HISTORY_COSMOS_ENABLED,
    CONFIG_COSMOS_ANALYTICS_CONTAINER,
    CONFIG_CREDENTIAL,
    CONFIG_KNOWLEDGE_SOURCES,
    CONFIG_LANGUAGE_PICKER_ENABLED,
    CONFIG_OPENAI_CLIENT,
    CONFIG_PROFILE,
    CONFIG_PUBLIC_ACCESS_ENABLED,
    CONFIG_RATE_LIMITER,
    CONFIG_REALTIME_DEPLOYMENT,
    CONFIG_REALTIME_ENABLED,
    CONFIG_REALTIME_TRANSCRIPTION_DEPLOYMENT,
    CONFIG_REALTIME_VOICE,
    CONFIG_SPEECH_INPUT_ENABLED,
    CONFIG_SPEECH_OUTPUT_AZURE_ENABLED,
    CONFIG_SPEECH_OUTPUT_BROWSER_ENABLED,
    CONFIG_SPEECH_SERVICE_ID,
    CONFIG_SPEECH_SERVICE_LOCATION,
    CONFIG_SPEECH_SERVICE_TOKEN,
    CONFIG_SPEECH_SERVICE_VOICE,
    CONFIG_STREAMING_ENABLED,
)
from core.analytics import log_interaction
from core.authentication import create_owner_authentication_helper
from core.knowledgebase import get_knowledge_base_path, load_knowledge_base
from core.profile import load_profile
from core.ratelimiter import RateLimiter, check_rate_limit
from core.sessionhelper import create_session_id
from decorators import authenticated
from error import PromptTooLargeError, error_dict, error_response
from realtime.relay import realtime_bp

bp = Blueprint("routes", __name__, static_folder="static")
# Fix Windows registry issue with mimetypes
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

MAX_SPEECH_TEXT_LENGTH = 5000
MAX_SPEECH_REQUEST_BYTES = 64 * 1024


@bp.route("/")
async def index():
    settings = await get_current_settings()
    source = (Path(__file__).resolve().parent / "static" / "index.html").read_text(encoding="utf-8")
    for placeholder, field in (
        ("__PROFILE_TITLE__", "assistant_name"),
        ("__PROFILE_DESCRIPTION__", "site_description"),
        ("__PROFILE_IMAGE__", "social_image_url"),
    ):
        source = source.replace(placeholder, html.escape(settings[field], quote=True))
    return await make_response(source, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"})


# Empty page is recommended for login redirect to work.
@bp.route("/redirect")
async def redirect():
    return ""


@bp.route("/favicon.ico")
async def favicon():
    return await bp.send_static_file("avatar.svg")


@bp.route("/assets/<path:path>")
async def assets(path):
    return await send_from_directory(Path(__file__).resolve().parent / "static" / "assets", path)


@bp.route("/<path:path>")
async def public_assets(path: str):
    return await send_from_directory(Path(__file__).resolve().parent / "static", path)


class JSONEncoder(json.JSONEncoder):
    def default(self, o):
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            as_dict = dataclasses.asdict(o)
            if isinstance(o, DataPoints):
                return {k: v for k, v in as_dict.items() if v is not None}
            if isinstance(o, ExtraInfo) and not o.history_truncated:
                as_dict.pop("history_truncated")
            return as_dict
        return super().default(o)


async def format_as_ndjson(r: AsyncGenerator[dict, None]) -> AsyncGenerator[str, None]:
    try:
        async for event in r:
            yield json.dumps(event, ensure_ascii=False, cls=JSONEncoder) + "\n"
    except Exception as error:
        logging.exception("Exception while generating response stream: %s", error)
        yield json.dumps(error_dict(error)) + "\n"


def _extract_confidence_from_result(result: dict) -> str:
    return result.get("context", {}).get("confidence_level", "unknown")


def validate_chat_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise BadRequest("Request must be a JSON object")
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise BadRequest("messages must be a nonempty list")
    if any(
        not isinstance(message, dict)
        or message.get("role") not in ("user", "assistant")
        or not isinstance(message.get("content"), str)
        or not message["content"].strip()
        for message in messages
    ):
        raise BadRequest("Each message must have a user or assistant role and nonempty text content")
    if messages[-1]["role"] != "user":
        raise BadRequest("The last message must have the user role")
    context = payload.get("context", {})
    if not isinstance(context, dict) or not isinstance(context.get("overrides", {}), dict):
        raise BadRequest("context and context.overrides must be JSON objects")
    if any(
        field in context.get("overrides", {})
        for field in ("prompt_template", "prompt_template_prefix", "prompt_template_suffix")
    ):
        raise BadRequest("Public requests cannot override the owner's system instructions.")
    return payload


def is_profile_example(context: dict[str, Any], query: str, settings: dict[str, Any]) -> bool:
    example_id = context.get("example_question_id")
    if not isinstance(example_id, str) or not example_id.startswith("example-"):
        return False
    suffix = example_id.removeprefix("example-")
    if not suffix.isdecimal() or len(suffix) > 2:
        return False
    index = int(suffix) - 1
    examples = settings.get("example_questions", [])
    return 0 <= index < len(examples) and query == examples[index]


@bp.route("/chat", methods=["POST"])
@authenticated
async def chat(auth_claims: dict[str, Any]):
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415

    user_oid = auth_claims.get("oid", "anonymous")
    start_time = time.time()
    user_query = ""
    message_index = 0
    session_state = None
    analytics_container = current_app.config.get(CONFIG_COSMOS_ANALYTICS_CONTAINER)
    try:
        if await check_rate_limit(auth_claims, request.remote_addr):
            return jsonify({"error": "Rate limit exceeded. Please try again shortly."}), 429
        request_json = validate_chat_request(await request.get_json())
        messages = request_json["messages"]
        user_query = messages[-1]["content"]
        message_index = sum(message["role"] == "user" for message in messages)
        session_state = request_json.get("session_state")
        context = request_json.get("context", {})
        context["auth_claims"] = auth_claims
        context["admin_settings"] = await get_current_settings()
        is_example = is_profile_example(context, user_query, context["admin_settings"])
        approach: Approach = cast(Approach, current_app.config[CONFIG_CHAT_APPROACH])

        if session_state is None:
            session_state = create_session_id(
                current_app.config[CONFIG_CHAT_HISTORY_COSMOS_ENABLED],
                current_app.config[CONFIG_CHAT_HISTORY_BROWSER_ENABLED],
            )
        result = await approach.run(
            messages,
            context=context,
            session_state=session_state,
        )
        elapsed_ms = (time.time() - start_time) * 1000
        confidence = _extract_confidence_from_result(result)
        response_content = result.get("message", {}).get("content", "")
        current_app.add_background_task(
            log_interaction,
            analytics_container,
            user_oid,
            user_query,
            confidence,
            elapsed_ms,
            session_id=session_state if isinstance(session_state, str) else None,
            message_index=message_index,
            response_length=len(response_content),
            is_example_question=is_example,
        )
        return jsonify(result)
    except BadRequest as error:
        return jsonify({"error": error.description}), 400
    except PromptTooLargeError as error:
        return error_response(error, "/chat", status_code=413)
    except RequestEntityTooLarge:
        return jsonify({"error": "Request is too large"}), 413
    except ServiceUnavailable as error:
        return error_response(error, "/chat", status_code=503)
    except Exception as error:
        elapsed_ms = (time.time() - start_time) * 1000
        current_app.add_background_task(
            log_interaction,
            analytics_container,
            user_oid,
            user_query,
            "error",
            elapsed_ms,
            session_id=session_state if isinstance(session_state, str) else None,
            message_index=message_index,
            error=str(error),
        )
        return error_response(error, "/chat")


@bp.route("/chat/stream", methods=["POST"])
@authenticated
async def chat_stream(auth_claims: dict[str, Any]):
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415

    user_oid = auth_claims.get("oid", "anonymous")
    start_time = time.time()
    user_query = ""
    message_index = 0
    session_state = None
    analytics_container = current_app.config.get(CONFIG_COSMOS_ANALYTICS_CONTAINER)
    try:
        if await check_rate_limit(auth_claims, request.remote_addr):
            return jsonify({"error": "Rate limit exceeded. Please try again shortly."}), 429
        request_json = validate_chat_request(await request.get_json())
        messages = request_json["messages"]
        user_query = messages[-1]["content"]
        message_index = sum(message["role"] == "user" for message in messages)
        session_state = request_json.get("session_state")
        context = request_json.get("context", {})
        context["auth_claims"] = auth_claims
        context["admin_settings"] = await get_current_settings()
        is_example = is_profile_example(context, user_query, context["admin_settings"])
        approach: Approach = cast(Approach, current_app.config[CONFIG_CHAT_APPROACH])

        if session_state is None:
            session_state = create_session_id(
                current_app.config[CONFIG_CHAT_HISTORY_COSMOS_ENABLED],
                current_app.config[CONFIG_CHAT_HISTORY_BROWSER_ENABLED],
            )
        result = await approach.run_stream(
            messages,
            context=context,
            session_state=session_state,
        )

        app = cast(LocalProxy[Quart], current_app)._get_current_object()
        sid = session_state if isinstance(session_state, str) else None

        async def stream_with_analytics(stream: AsyncGenerator[dict, None]) -> AsyncGenerator[dict, None]:
            confidence = "unknown"
            response_length = 0
            try:
                async for event in stream:
                    ctx = event.get("context")
                    if ctx and hasattr(ctx, "confidence_level") and ctx.confidence_level:
                        confidence = ctx.confidence_level
                    delta_content = event.get("delta", {}).get("content")
                    if delta_content:
                        response_length += len(delta_content)
                    yield event
            except Exception as stream_error:
                app.add_background_task(
                    log_interaction,
                    analytics_container,
                    user_oid,
                    user_query,
                    "error",
                    (time.time() - start_time) * 1000,
                    session_id=sid,
                    message_index=message_index,
                    response_length=response_length,
                    is_example_question=is_example,
                    error=str(stream_error),
                )
                raise
            else:
                app.add_background_task(
                    log_interaction,
                    analytics_container,
                    user_oid,
                    user_query,
                    confidence,
                    (time.time() - start_time) * 1000,
                    session_id=sid,
                    message_index=message_index,
                    response_length=response_length,
                    is_example_question=is_example,
                )

        response = await make_response(format_as_ndjson(stream_with_analytics(result)))
        response.timeout = None  # type: ignore
        response.mimetype = "application/json-lines"
        return response
    except BadRequest as error:
        return jsonify({"error": error.description}), 400
    except PromptTooLargeError as error:
        return error_response(error, "/chat/stream", status_code=413)
    except RequestEntityTooLarge:
        return jsonify({"error": "Request is too large"}), 413
    except ServiceUnavailable as error:
        return error_response(error, "/chat/stream", status_code=503)
    except Exception as error:
        elapsed_ms = (time.time() - start_time) * 1000
        current_app.add_background_task(
            log_interaction,
            analytics_container,
            user_oid,
            user_query,
            "error",
            elapsed_ms,
            session_id=session_state if isinstance(session_state, str) else None,
            message_index=message_index,
            error=str(error),
        )
        return error_response(error, "/chat/stream")


@bp.route("/auth_setup", methods=["GET"])
def auth_setup():
    auth_helper = current_app.config[CONFIG_AUTH_CLIENT]
    return jsonify(auth_helper.get_auth_setup_for_client())


@bp.route("/auth_setup_admin", methods=["GET"])
def auth_setup_admin():
    auth_helper = current_app.config[CONFIG_AUTH_CLIENT]
    return jsonify(auth_helper.get_auth_setup_for_admin())


@bp.route("/config", methods=["GET"])
def config():
    return jsonify(
        {
            "streamingEnabled": current_app.config[CONFIG_STREAMING_ENABLED],
            "showLanguagePicker": current_app.config[CONFIG_LANGUAGE_PICKER_ENABLED],
            "showSpeechInput": current_app.config[CONFIG_SPEECH_INPUT_ENABLED],
            "showSpeechOutputBrowser": current_app.config[CONFIG_SPEECH_OUTPUT_BROWSER_ENABLED],
            "showSpeechOutputAzure": current_app.config[CONFIG_SPEECH_OUTPUT_AZURE_ENABLED],
            "showChatHistoryBrowser": current_app.config[CONFIG_CHAT_HISTORY_BROWSER_ENABLED],
            "showChatHistoryCosmos": current_app.config[CONFIG_CHAT_HISTORY_COSMOS_ENABLED],
            "showRealtimeVoice": current_app.config[CONFIG_REALTIME_ENABLED],
        }
    )


@bp.get("/health")
def health():
    return jsonify(
        {
            "status": "ready",
            "voice_configured": bool(current_app.config.get(CONFIG_REALTIME_DEPLOYMENT)),
            "public_access_enabled": current_app.config.get(CONFIG_PUBLIC_ACCESS_ENABLED, True),
        }
    )


@bp.route("/speech", methods=["POST"])
@authenticated
async def speech(auth_claims: dict[str, Any]):
    if not current_app.config.get(CONFIG_SPEECH_OUTPUT_AZURE_ENABLED, False):
        return jsonify({"error": "Speech synthesis is disabled."}), 404
    if not request.is_json:
        return jsonify({"error": "request must be json"}), 415

    try:
        if await check_rate_limit(auth_claims, request.remote_addr):
            return jsonify({"error": "Rate limit exceeded. Please try again shortly."}), 429
        if (request.content_length or 0) > MAX_SPEECH_REQUEST_BYTES:
            return jsonify({"error": "Speech request is too large"}), 413
        if len(await request.get_data()) > MAX_SPEECH_REQUEST_BYTES:
            return jsonify({"error": "Speech request is too large"}), 413
        request_json = await request.get_json()
        if not isinstance(request_json, dict):
            return jsonify({"error": "Request must be a JSON object"}), 400
        text = request_json.get("text")
        if not isinstance(text, str) or not text.strip():
            return jsonify({"error": "text must be a nonempty string"}), 400
        if len(text) > MAX_SPEECH_TEXT_LENGTH:
            return jsonify({"error": f"text must not exceed {MAX_SPEECH_TEXT_LENGTH} characters"}), 413

        speech_token = current_app.config.get(CONFIG_SPEECH_SERVICE_TOKEN)
        if speech_token is None or speech_token.expires_on < time.time() + 60:
            speech_token = await current_app.config[CONFIG_CREDENTIAL].get_token(
                "https://cognitiveservices.azure.com/.default"
            )
            current_app.config[CONFIG_SPEECH_SERVICE_TOKEN] = speech_token

        from azure.cognitiveservices.speech import (
            ResultReason,
            SpeechConfig,
            SpeechSynthesisOutputFormat,
            SpeechSynthesizer,
        )

        auth_token = "aad#" + current_app.config[CONFIG_SPEECH_SERVICE_ID] + "#" + speech_token.token
        region = current_app.config[CONFIG_SPEECH_SERVICE_LOCATION]
        voice = current_app.config[CONFIG_SPEECH_SERVICE_VOICE]

        def synthesize():
            speech_config = SpeechConfig(auth_token=auth_token, region=region)
            speech_config.speech_synthesis_voice_name = voice
            speech_config.speech_synthesis_output_format = SpeechSynthesisOutputFormat.Audio16Khz32KBitRateMonoMp3
            synthesizer = SpeechSynthesizer(speech_config=speech_config, audio_config=None)
            return synthesizer.speak_text_async(text).get()

        result = await asyncio.to_thread(synthesize)
        if result.reason == ResultReason.SynthesizingAudioCompleted:
            return result.audio_data, 200, {"Content-Type": "audio/mp3"}
        elif result.reason == ResultReason.Canceled:
            cancellation_details = result.cancellation_details
            current_app.logger.error(
                "Speech synthesis canceled: %s %s", cancellation_details.reason, cancellation_details.error_details
            )
            return jsonify({"error": "Speech synthesis canceled. Check logs for details."}), 500
        else:
            current_app.logger.error("Unexpected result reason: %s", result.reason)
            return jsonify({"error": "Speech synthesis failed. Check logs for details."}), 500
    except BadRequest as error:
        return jsonify({"error": error.description}), 400
    except RequestEntityTooLarge:
        return jsonify({"error": "Speech request is too large"}), 413
    except ServiceUnavailable as error:
        return error_response(error, "/speech", status_code=503)
    except Exception as error:
        return error_response(error, "/speech")


@bp.before_app_serving
async def setup_clients():
    OPENAI_CHATGPT_MODEL = os.getenv("AZURE_OPENAI_CHATGPT_MODEL", "gpt-4.1-mini")
    AZURE_OPENAI_SERVICE = os.getenv("AZURE_OPENAI_SERVICE")
    AZURE_OPENAI_CHATGPT_DEPLOYMENT = os.getenv("AZURE_OPENAI_CHATGPT_DEPLOYMENT", "chat")
    AZURE_OPENAI_CUSTOM_URL = os.getenv("AZURE_OPENAI_CUSTOM_URL")
    AZURE_OPENAI_API_KEY_OVERRIDE = os.getenv("AZURE_OPENAI_API_KEY_OVERRIDE")
    OPENAI_REASONING_EFFORT = os.getenv("AZURE_OPENAI_REASONING_EFFORT")
    OPENAI_CHATGPT_CONTEXT_WINDOW = int(os.getenv("AZURE_OPENAI_CHATGPT_CONTEXT_WINDOW", "16384"))
    current_app.config[CONFIG_PROFILE] = load_profile(os.getenv("PROFILE_PATH"))
    disable_public_access = os.getenv("DISABLE_PUBLIC_ACCESS", "false").lower()
    if disable_public_access not in {"true", "false"}:
        raise ValueError("DISABLE_PUBLIC_ACCESS must be true or false")
    current_app.config[CONFIG_PUBLIC_ACCESS_ENABLED] = disable_public_access != "true"

    AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID")
    AZURE_SPEECH_SERVICE_ID = os.getenv("AZURE_SPEECH_SERVICE_ID")
    AZURE_SPEECH_SERVICE_LOCATION = os.getenv("AZURE_SPEECH_SERVICE_LOCATION")
    AZURE_SPEECH_SERVICE_VOICE = os.getenv("AZURE_SPEECH_SERVICE_VOICE") or "en-US-AndrewMultilingualNeural"

    ENABLE_LANGUAGE_PICKER = os.getenv("ENABLE_LANGUAGE_PICKER", "").lower() == "true"
    USE_SPEECH_INPUT_BROWSER = os.getenv("USE_SPEECH_INPUT_BROWSER", "").lower() == "true"
    USE_SPEECH_OUTPUT_BROWSER = os.getenv("USE_SPEECH_OUTPUT_BROWSER", "").lower() == "true"
    USE_SPEECH_OUTPUT_AZURE = os.getenv("USE_SPEECH_OUTPUT_AZURE", "").lower() == "true"
    USE_CHAT_HISTORY_BROWSER = os.getenv("USE_CHAT_HISTORY_BROWSER", "true").lower() == "true"
    USE_CHAT_HISTORY_COSMOS = os.getenv("USE_CHAT_HISTORY_COSMOS", "").lower() == "true"
    USE_REALTIME_VOICE = os.getenv("USE_REALTIME_VOICE", "true").lower() == "true"
    AZURE_OPENAI_REALTIME_DEPLOYMENT = os.getenv("AZURE_OPENAI_REALTIME_DEPLOYMENT", "voice")
    AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT = os.getenv("AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT", "transcription")
    AZURE_OPENAI_REALTIME_VOICE = os.getenv("AZURE_OPENAI_REALTIME_VOICE", "alloy")

    RUNNING_ON_AZURE = os.getenv("WEBSITE_HOSTNAME") is not None or os.getenv("RUNNING_IN_PRODUCTION") is not None

    # Set up Azure credential
    azure_credential: AzureDeveloperCliCredential | ManagedIdentityCredential
    if RUNNING_ON_AZURE:
        current_app.logger.info("Setting up Azure credential using ManagedIdentityCredential")
        if AZURE_CLIENT_ID := os.getenv("AZURE_CLIENT_ID"):
            current_app.logger.info("Using ManagedIdentityCredential with client_id %s", AZURE_CLIENT_ID)
            azure_credential = ManagedIdentityCredential(client_id=AZURE_CLIENT_ID)
        else:
            azure_credential = ManagedIdentityCredential()
    elif AZURE_TENANT_ID:
        current_app.logger.info("Using AzureDeveloperCliCredential with tenant_id %s", AZURE_TENANT_ID)
        azure_credential = AzureDeveloperCliCredential(tenant_id=AZURE_TENANT_ID, process_timeout=60)
    else:
        current_app.logger.info("Using AzureDeveloperCliCredential for home tenant")
        azure_credential = AzureDeveloperCliCredential(process_timeout=60)

    current_app.config[CONFIG_CREDENTIAL] = azure_credential

    # Set up OpenAI client
    azure_openai_endpoint = None
    if AZURE_OPENAI_CUSTOM_URL:
        azure_openai_endpoint = AZURE_OPENAI_CUSTOM_URL
        if AZURE_OPENAI_API_KEY_OVERRIDE:
            openai_client: AsyncOpenAI = AsyncAzureOpenAI(
                azure_endpoint=azure_openai_endpoint,
                api_key=AZURE_OPENAI_API_KEY_OVERRIDE,
                api_version="2024-12-01-preview",
            )
        else:
            token_provider = get_bearer_token_provider(azure_credential, "https://cognitiveservices.azure.com/.default")
            openai_client = AsyncAzureOpenAI(
                azure_endpoint=azure_openai_endpoint,
                azure_ad_token_provider=token_provider,
                api_version="2024-12-01-preview",
            )
    elif AZURE_OPENAI_SERVICE:
        azure_openai_endpoint = f"https://{AZURE_OPENAI_SERVICE}.openai.azure.com"
        token_provider = get_bearer_token_provider(azure_credential, "https://cognitiveservices.azure.com/.default")
        openai_client = AsyncAzureOpenAI(
            azure_endpoint=azure_openai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2024-12-01-preview",
        )
    else:
        raise ValueError("AZURE_OPENAI_SERVICE or AZURE_OPENAI_CUSTOM_URL must be set")

    current_app.config[CONFIG_OPENAI_CLIENT] = openai_client

    # Set up authentication helper
    auth_helper = create_owner_authentication_helper(os.environ)
    current_app.config[CONFIG_AUTH_CLIENT] = auth_helper

    # Speech service setup
    if USE_SPEECH_OUTPUT_AZURE:
        current_app.logger.info("USE_SPEECH_OUTPUT_AZURE is true, setting up Azure speech service")
        if not AZURE_SPEECH_SERVICE_ID:
            raise ValueError("Azure speech resource not configured correctly, missing AZURE_SPEECH_SERVICE_ID")
        if not AZURE_SPEECH_SERVICE_LOCATION:
            raise ValueError("Azure speech resource not configured correctly, missing AZURE_SPEECH_SERVICE_LOCATION")
        current_app.config[CONFIG_SPEECH_SERVICE_ID] = AZURE_SPEECH_SERVICE_ID
        current_app.config[CONFIG_SPEECH_SERVICE_LOCATION] = AZURE_SPEECH_SERVICE_LOCATION
        current_app.config[CONFIG_SPEECH_SERVICE_VOICE] = AZURE_SPEECH_SERVICE_VOICE
        current_app.config[CONFIG_SPEECH_SERVICE_TOKEN] = None

    # Feature flags
    current_app.config[CONFIG_STREAMING_ENABLED] = (
        OPENAI_CHATGPT_MODEL not in Approach.GPT_REASONING_MODELS
        or Approach.GPT_REASONING_MODELS[OPENAI_CHATGPT_MODEL].streaming
    )
    current_app.config[CONFIG_LANGUAGE_PICKER_ENABLED] = ENABLE_LANGUAGE_PICKER
    current_app.config[CONFIG_SPEECH_INPUT_ENABLED] = USE_SPEECH_INPUT_BROWSER
    current_app.config[CONFIG_SPEECH_OUTPUT_BROWSER_ENABLED] = USE_SPEECH_OUTPUT_BROWSER
    current_app.config[CONFIG_SPEECH_OUTPUT_AZURE_ENABLED] = USE_SPEECH_OUTPUT_AZURE
    current_app.config[CONFIG_CHAT_HISTORY_BROWSER_ENABLED] = USE_CHAT_HISTORY_BROWSER
    current_app.config[CONFIG_CHAT_HISTORY_COSMOS_ENABLED] = USE_CHAT_HISTORY_COSMOS

    # Realtime voice config
    current_app.config[CONFIG_REALTIME_ENABLED] = USE_REALTIME_VOICE
    current_app.config[CONFIG_REALTIME_DEPLOYMENT] = AZURE_OPENAI_REALTIME_DEPLOYMENT
    current_app.config[CONFIG_REALTIME_TRANSCRIPTION_DEPLOYMENT] = AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT
    current_app.config[CONFIG_REALTIME_VOICE] = AZURE_OPENAI_REALTIME_VOICE
    current_app.config["azure_openai_endpoint"] = azure_openai_endpoint

    # Admin config
    current_app.config[CONFIG_ADMIN_USER_OID] = os.getenv("ADMIN_USER_OID", "").strip()

    # Rate limiter
    current_app.config[CONFIG_RATE_LIMITER] = RateLimiter(
        max_requests=int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "20")),
        window_seconds=int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60")),
    )

    # Load knowledge base files directly into memory
    knowledge_data_dir = get_knowledge_base_path(os.environ.get("KNOWLEDGE_DATA_DIR"))
    knowledge_sources = load_knowledge_base(knowledge_data_dir)
    current_app.config[CONFIG_KNOWLEDGE_SOURCES] = knowledge_sources

    # Set up the chat approach
    prompt_manager = PromptyManager()
    chat_approach = PersonaChatApproach(
        openai_client=openai_client,
        chatgpt_model=OPENAI_CHATGPT_MODEL,
        chatgpt_deployment=AZURE_OPENAI_CHATGPT_DEPLOYMENT,
        prompt_manager=prompt_manager,
        reasoning_effort=OPENAI_REASONING_EFFORT,
        knowledge_sources=knowledge_sources,
        chatgpt_context_window=OPENAI_CHATGPT_CONTEXT_WINDOW,
    )
    current_app.config[CONFIG_CHAT_APPROACH] = chat_approach


@bp.after_app_serving
async def close_clients():
    await current_app.config[CONFIG_OPENAI_CLIENT].close()
    await current_app.config[CONFIG_CREDENTIAL].close()


def create_app():
    app = Quart(__name__)
    app.register_blueprint(bp)
    app.register_blueprint(chat_history_cosmosdb_bp)
    app.register_blueprint(admin_settings_bp)
    app.register_blueprint(realtime_bp)

    if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING"):
        app.logger.info("APPLICATIONINSIGHTS_CONNECTION_STRING is set, enabling Azure Monitor")
        configure_azure_monitor(
            instrumentation_options={
                "django": {"enabled": False},
                "psycopg2": {"enabled": False},
                "fastapi": {"enabled": False},
            }
        )
        AioHttpClientInstrumentor().instrument()
        HTTPXClientInstrumentor().instrument()
        app.asgi_app = OpenTelemetryMiddleware(app.asgi_app)  # type: ignore[assignment]

    logging.basicConfig(level=logging.WARNING)
    app_level = os.getenv("APP_LOG_LEVEL", "INFO")
    app.logger.setLevel(app_level)
    logging.getLogger("scripts").setLevel(app_level)

    if allowed_origin := os.getenv("ALLOWED_ORIGIN"):
        allowed_origins = allowed_origin.split(";")
        if len(allowed_origins) > 0:
            app.logger.info("CORS enabled for %s", allowed_origins)
            cors(app, allow_origin=allowed_origins, allow_methods=["GET", "POST"])

    return app
