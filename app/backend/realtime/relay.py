import asyncio
import base64
import binascii
import json
import logging
from time import monotonic
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import tiktoken
from quart import Blueprint, current_app, websocket

from admin.settings_cosmosdb import get_current_settings
from config import (
    CONFIG_AUTH_CLIENT,
    CONFIG_CREDENTIAL,
    CONFIG_KNOWLEDGE_SOURCES,
    CONFIG_REALTIME_DEPLOYMENT,
    CONFIG_REALTIME_ENABLED,
    CONFIG_REALTIME_VOICE,
)
from core.authentication import AuthError
from core.profile import build_persona_instructions
from core.ratelimiter import check_rate_limit
from core.voiceusage import (
    MAX_SESSION_SECONDS,
    VoiceUsageLimitExceeded,
    voice_session_usage,
)

logger = logging.getLogger(__name__)

realtime_bp = Blueprint("realtime", __name__)

MAX_CLIENT_MESSAGE_BYTES = 64 * 1024
MAX_AUDIO_CHUNK_BYTES = 32 * 1024
MAX_CLIENT_BYTES_PER_SECOND = 128 * 1024
MAX_CLIENT_EVENTS_PER_SECOND = 100
CLIENT_IDLE_TIMEOUT_SECONDS = 60
SETUP_TIMEOUT_SECONDS = 20
MAX_SETUP_EVENTS = 16
MAX_UPSTREAM_MESSAGE_BYTES = 1024 * 1024
MAX_UPSTREAM_ITEM_ID_LENGTH = 256
REALTIME_CONTEXT_TOKENS = 256_000
REALTIME_TOKEN_BUDGET = 16_384
MAX_RESPONSE_OUTPUT_TOKENS = 1024
TOKEN_SAFETY_HEADROOM = 1024
SUPPORTED_VOICES = {"alloy", "ash", "ballad", "coral", "cedar", "echo", "marin", "sage", "shimmer", "verse"}
SERVER_EVENT_ALIASES = {
    "response.output_audio.delta": "response.audio.delta",
    "response.output_audio.done": "response.audio.done",
    "response.output_audio_transcript.delta": "response.audio_transcript.delta",
    "response.output_audio_transcript.done": "response.audio_transcript.done",
}
SERVER_EVENT_FIELDS = {
    "response.audio.delta": "delta",
    "response.audio.done": None,
    "response.audio_transcript.delta": "delta",
    "response.audio_transcript.done": None,
    "conversation.item.input_audio_transcription.completed": "transcript",
    "response.created": None,
    "input_audio_buffer.speech_started": None,
    "input_audio_buffer.speech_stopped": None,
    "input_audio_buffer.committed": "item_id",
}


class RealtimeProtocolError(Exception):
    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code = code


def parse_client_event(data: str | bytes) -> dict[str, str]:
    if not isinstance(data, str):
        raise RealtimeProtocolError(1003, "Text messages required")
    if len(data) > MAX_CLIENT_MESSAGE_BYTES or len(data.encode("utf-8")) > MAX_CLIENT_MESSAGE_BYTES:
        raise RealtimeProtocolError(1009, "Message too large")
    try:
        message = json.loads(data)
    except (ValueError, RecursionError):
        raise RealtimeProtocolError(1007, "Invalid JSON") from None
    if (
        not isinstance(message, dict)
        or set(message) != {"type", "audio"}
        or message["type"] != "input_audio_buffer.append"
    ):
        raise RealtimeProtocolError(1008, "Unsupported client event")
    audio = message["audio"]
    if not isinstance(audio, str) or not audio:
        raise RealtimeProtocolError(1007, "Invalid audio")
    if len(audio) > 4 * ((MAX_AUDIO_CHUNK_BYTES + 2) // 3):
        raise RealtimeProtocolError(1009, "Audio chunk too large")
    try:
        decoded_audio = base64.b64decode(audio, validate=True)
    except (ValueError, binascii.Error):
        raise RealtimeProtocolError(1007, "Invalid audio") from None
    if len(decoded_audio) > MAX_AUDIO_CHUNK_BYTES:
        raise RealtimeProtocolError(1009, "Audio chunk too large")
    if not decoded_audio or len(decoded_audio) % 2 or base64.b64encode(decoded_audio).decode("ascii") != audio:
        raise RealtimeProtocolError(1007, "Invalid PCM16 audio")
    return {"type": "input_audio_buffer.append", "audio": audio}


async def get_auth_claims_for_websocket(token: Optional[str]) -> dict[str, Any]:
    """Authenticate a WebSocket connection using a token from the query string."""
    auth_helper = current_app.config[CONFIG_AUTH_CLIENT]
    if token is None and (not auth_helper.use_authentication or auth_helper.enable_unauthenticated_access):
        return {}
    if not token:
        raise AuthError("No authentication token provided", 401)
    headers = {"Authorization": f"Bearer {token}"}
    claims = await auth_helper.get_auth_claims_if_enabled(headers)
    identity = claims.get("oid") if isinstance(claims, dict) else None
    if not isinstance(identity, str) or not identity.strip() or identity == "anonymous":
        raise AuthError("Invalid authentication claims", 401)
    return claims


def parse_server_event(data: str) -> Optional[dict[str, str]]:
    try:
        message = json.loads(data)
    except (ValueError, RecursionError):
        raise RealtimeProtocolError(1011, "Realtime service unavailable") from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise RealtimeProtocolError(1011, "Realtime service unavailable")
    event_type = SERVER_EVENT_ALIASES.get(message["type"], message["type"])
    response = message.get("response")
    if (
        event_type in {"error", "conversation.item.input_audio_transcription.failed"}
        or message.get("error") is not None
        or (event_type == "response.done" and isinstance(response, dict) and response.get("status") == "failed")
    ):
        raise RealtimeProtocolError(1011, "Realtime service unavailable")
    if event_type not in SERVER_EVENT_FIELDS:
        return None
    event = {"type": event_type}
    field = SERVER_EVENT_FIELDS[event_type]
    if field is not None:
        value = message.get(field)
        if not isinstance(value, str) or (
            field == "item_id" and (not value.strip() or len(value) > MAX_UPSTREAM_ITEM_ID_LENGTH)
        ):
            raise RealtimeProtocolError(1011, "Realtime service unavailable")
        event[field] = value
    return event


async def wait_for_session_update(upstream_ws: aiohttp.ClientWebSocketResponse) -> None:
    remaining_events = MAX_SETUP_EVENTS
    while remaining_events > 0:
        remaining_events -= 1
        message = await upstream_ws.receive()
        if message.type != aiohttp.WSMsgType.TEXT:
            raise RealtimeProtocolError(1011, "Realtime service unavailable")
        parse_server_event(message.data)
        event = json.loads(message.data)
        if event["type"] == "session.updated":
            session = event.get("session")
            if (
                not isinstance(session, dict)
                or session.get("type") != "realtime"
                or session.get("max_output_tokens") != MAX_RESPONSE_OUTPUT_TOKENS
            ):
                raise RealtimeProtocolError(1011, "Realtime service unavailable")
            return
    raise RealtimeProtocolError(1011, "Realtime service unavailable")


async def relay_messages(upstream_ws: aiohttp.ClientWebSocketResponse) -> None:
    async def client_to_server():
        event_budget = float(MAX_CLIENT_EVENTS_PER_SECOND)
        byte_budget = float(MAX_CLIENT_BYTES_PER_SECOND)
        last_refill = monotonic()
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive(), timeout=CLIENT_IDLE_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                raise RealtimeProtocolError(1008, "Session idle timeout") from None
            message = parse_client_event(data)
            now = monotonic()
            elapsed = now - last_refill
            last_refill = now
            event_budget = min(MAX_CLIENT_EVENTS_PER_SECOND, event_budget + elapsed * MAX_CLIENT_EVENTS_PER_SECOND)
            byte_budget = min(MAX_CLIENT_BYTES_PER_SECOND, byte_budget + elapsed * MAX_CLIENT_BYTES_PER_SECOND)
            event_budget -= 1
            byte_budget -= len(data.encode("utf-8"))
            if event_budget < 0 or byte_budget < 0:
                raise RealtimeProtocolError(1008, "Audio rate limit exceeded")
            await upstream_ws.send_json(message)

    async def server_to_client():
        async for message in upstream_ws:
            if message.type == aiohttp.WSMsgType.TEXT:
                event = parse_server_event(message.data)
                if event is not None:
                    await websocket.send_json(event)
            elif message.type == aiohttp.WSMsgType.ERROR:
                raise RealtimeProtocolError(1011, "Realtime service unavailable")
            elif message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED):
                break

    tasks = {
        asyncio.create_task(client_to_server(), name="realtime-client-to-server"),
        asyncio.create_task(server_to_client(), name="realtime-server-to-client"),
    }
    try:
        done = (await asyncio.wait(tasks, timeout=MAX_SESSION_SECONDS, return_when=asyncio.FIRST_COMPLETED))[0]
        if not done:
            raise RealtimeProtocolError(1008, "Session time limit reached")
        for task in done:
            task.result()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def build_system_instructions(admin_settings: dict[str, Any], knowledge_sources: list[str]) -> str:
    """Use the shared persona and complete knowledge without silently truncating either."""
    if not knowledge_sources or any(not isinstance(source, str) or not source.strip() for source in knowledge_sources):
        raise RealtimeProtocolError(1011, "Voice knowledge is unavailable")
    instructions = (
        build_persona_instructions(admin_settings)
        + "\nThis is a voice conversation. Speak naturally in brief, clear sentences, not a written essay. "
        "Do not read markdown, source filenames, or citations aloud. "
        "When asked about a role, briefly explain supported strengths and honest gaps without inventing experience.\n"
        "\n## Knowledge Sources\n" + "\n\n".join(knowledge_sources)
    )
    instruction_budget = min(REALTIME_TOKEN_BUDGET, REALTIME_CONTEXT_TOKENS)
    instruction_budget -= MAX_RESPONSE_OUTPUT_TOKENS + TOKEN_SAFETY_HEADROOM
    encoding = tiktoken.get_encoding("o200k_base")
    if len(encoding.encode(instructions, disallowed_special=())) > instruction_budget:
        raise RealtimeProtocolError(1009, "Knowledge and profile exceed voice token budget")
    return instructions


@realtime_bp.websocket("/realtime")
async def realtime_relay():
    """WebSocket relay between the frontend and Azure OpenAI Realtime API."""
    if current_app.config.get(CONFIG_REALTIME_ENABLED) is not True:
        await websocket.close(1008, "Realtime is disabled")
        return

    try:
        auth_claims = await asyncio.wait_for(
            get_auth_claims_for_websocket(websocket.args.get("token")), timeout=SETUP_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning("Realtime authentication failed")
        await websocket.close(1008, "Authentication failed")
        return

    try:
        if await asyncio.wait_for(check_rate_limit(auth_claims, websocket.remote_addr), timeout=SETUP_TIMEOUT_SECONDS):
            raise RealtimeProtocolError(1008, "Rate limit exceeded")

        azure_openai_endpoint = current_app.config.get("azure_openai_endpoint")
        realtime_deployment = current_app.config.get(CONFIG_REALTIME_DEPLOYMENT)
        realtime_voice = current_app.config.get(CONFIG_REALTIME_VOICE, "alloy")
        if (
            not isinstance(azure_openai_endpoint, str)
            or not isinstance(realtime_deployment, str)
            or not realtime_deployment.strip()
            or realtime_voice not in SUPPORTED_VOICES
        ):
            raise RealtimeProtocolError(1011, "Realtime service unavailable")
        endpoint = urlsplit(azure_openai_endpoint)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise RealtimeProtocolError(1011, "Realtime service unavailable")
        realtime_url = urlunsplit(("wss", endpoint.netloc, "/openai/v1/realtime", "", ""))
        admin_settings = await asyncio.wait_for(get_current_settings(), timeout=SETUP_TIMEOUT_SECONDS)
        knowledge_sources = current_app.config.get(CONFIG_KNOWLEDGE_SOURCES, [])
        instructions = build_system_instructions(admin_settings, knowledge_sources)
        azure_credential = current_app.config[CONFIG_CREDENTIAL]
        token_obj = await asyncio.wait_for(
            azure_credential.get_token("https://cognitiveservices.azure.com/.default"), timeout=SETUP_TIMEOUT_SECONDS
        )

        async with voice_session_usage() as usage:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=SETUP_TIMEOUT_SECONDS)) as session:
                async with session.ws_connect(
                    realtime_url,
                    params={"model": realtime_deployment},
                    headers={"Authorization": f"Bearer {token_obj.token}"},
                    max_msg_size=MAX_UPSTREAM_MESSAGE_BYTES,
                ) as upstream_ws:
                    session_update = {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "output_modalities": ["audio"],
                            "instructions": instructions,
                            "max_output_tokens": MAX_RESPONSE_OUTPUT_TOKENS,
                            "audio": {
                                "input": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "turn_detection": {
                                        "type": "server_vad",
                                        "threshold": 0.5,
                                        "prefix_padding_ms": 300,
                                        "silence_duration_ms": 500,
                                        "create_response": True,
                                        "interrupt_response": True,
                                    },
                                },
                                "output": {
                                    "voice": realtime_voice,
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                },
                            },
                        },
                    }
                    await asyncio.wait_for(upstream_ws.send_json(session_update), timeout=SETUP_TIMEOUT_SECONDS)
                    await asyncio.wait_for(wait_for_session_update(upstream_ws), timeout=SETUP_TIMEOUT_SECONDS)
                    usage.admitted = True
                    await relay_messages(upstream_ws)
    except VoiceUsageLimitExceeded as error:
        await websocket.close(1008, str(error))
    except RealtimeProtocolError as error:
        await websocket.close(error.code, str(error))
    except Exception:
        logger.warning("Realtime connection failed")
        await websocket.close(1011, "Realtime service unavailable")
    else:
        await websocket.close(1000, "Session ended")
