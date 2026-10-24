import logging
import time
import uuid
from typing import Optional

from azure.cosmos.aio import ContainerProxy
from opentelemetry import trace

tracer = trace.get_tracer(__name__)


async def log_interaction(
    container: Optional[ContainerProxy],
    user_oid: str,
    query: str,
    confidence_level: str,
    response_time_ms: float,
    session_id: Optional[str] = None,
    message_index: int = 0,
    response_length: int = 0,
    error: Optional[str] = None,
    is_example_question: bool = False,
):
    visitor_id = f"session:{session_id}" if session_id else "anonymous"

    # Emit App Insights custom event via OpenTelemetry
    with tracer.start_as_current_span("chat_interaction") as span:
        span.set_attribute("custom.visitor_id", visitor_id)
        span.set_attribute("custom.confidence_level", confidence_level)
        span.set_attribute("custom.response_time_ms", response_time_ms)
        span.set_attribute("custom.is_example_question", is_example_question)
        span.set_attribute("custom.message_index", message_index)
        span.set_attribute("custom.response_length", response_length)
        if session_id:
            span.set_attribute("custom.session_id", session_id)
        if error:
            span.set_attribute("custom.error", "request_failed")
            span.set_status(trace.StatusCode.ERROR)

    if container is None:
        return
    try:
        doc = {
            "id": str(uuid.uuid4()),
            "user_oid": visitor_id,
            "confidence_level": confidence_level,
            "response_time_ms": response_time_ms,
            "session_id": session_id,
            "message_index": message_index,
            "is_example_question": is_example_question,
            "response_length": response_length,
            "error": "request_failed" if error else None,
            "timestamp": int(time.time() * 1000),
        }
        await container.upsert_item(doc)
    except Exception:
        logging.exception("Failed to log analytics event")
