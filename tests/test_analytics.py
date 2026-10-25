from unittest.mock import AsyncMock

import pytest

from core.analytics import log_interaction


@pytest.mark.asyncio
async def test_analytics_keeps_operational_metrics_without_raw_content_or_user_identity():
    container = AsyncMock()
    await log_interaction(
        container,
        100,
        session_id="ephemeral-session",
        error="Detailed private failure",
        is_example_question=True,
    )
    document = container.upsert_item.await_args.args[0]
    assert document["user_oid"] == "session:ephemeral-session"
    assert document["error"] == "request_failed"
    assert document["is_example_question"] is True
    assert "query" not in document
    assert "confidence_level" not in document
    assert "Private visitor" not in str(document)
    assert "real-owner-id" not in str(document)


@pytest.mark.asyncio
async def test_analytics_storage_failure_is_logged_not_disguised_as_persisted(caplog):
    container = AsyncMock()
    container.upsert_item.side_effect = RuntimeError("Storage unavailable")
    await log_interaction(container, 100)
    assert "Failed to log analytics event" in caplog.text
    assert "Do not retain this question" not in caplog.text
