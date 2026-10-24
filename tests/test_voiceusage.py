import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from quart import Quart
from werkzeug.exceptions import ServiceUnavailable

from config import CONFIG_COSMOS_SETTINGS_CONTAINER
from core import voiceusage


@pytest.fixture
def voice_clock(monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(voiceusage.time, "time", lambda: clock.now)
    return clock


@pytest.fixture
def voice_container():
    container = AsyncMock()
    container.documents = {}

    async def read_item(*, item, partition_key):
        assert item == partition_key == voiceusage.VOICE_USAGE_DOCUMENT_ID
        stored = deepcopy(container.documents.get(item))
        await asyncio.sleep(0)
        if stored is None:
            raise CosmosResourceNotFoundError(status_code=404)
        return stored

    async def create_item(*, body):
        await asyncio.sleep(0)
        if body["id"] in container.documents:
            raise CosmosResourceExistsError(status_code=409)
        container.documents[body["id"]] = {**deepcopy(body), "_etag": "1"}

    async def replace_item(*, item, body, etag, match_condition):
        assert item == body["id"] == voiceusage.VOICE_USAGE_DOCUMENT_ID
        assert match_condition == MatchConditions.IfNotModified
        await asyncio.sleep(0)
        stored = container.documents.get(item)
        if stored is None:
            raise CosmosResourceNotFoundError(status_code=404)
        if etag != stored["_etag"]:
            raise CosmosHttpResponseError(status_code=412)
        container.documents[item] = {**deepcopy(body), "_etag": str(int(etag) + 1)}

    container.read_item.side_effect = read_item
    container.create_item.side_effect = create_item
    container.replace_item.side_effect = replace_item
    return container


def application_for(container=None):
    application = Quart(__name__)
    if container is not None:
        application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = container
    return application


def stored_record(application, container):
    if container is not None:
        return container.documents[voiceusage.VOICE_USAGE_DOCUMENT_ID]
    return application.config[voiceusage.VOICE_USAGE_LOCAL_KEY].record


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_daily_admissions_are_bounded_and_successful_disconnects_do_not_refund(
    voice_clock, voice_container, shared
):
    container = voice_container if shared else None
    application = application_for(container)
    async with application.app_context():
        for count in range(voiceusage.MAX_DAILY_VOICE_SESSIONS):
            async with voiceusage.voice_session_usage() as lease:
                lease.admitted = True
                record = stored_record(application, container)
                assert record["admitted"] == count + 1
                assert list(record["leases"]) == [lease.identifier]
                assert record["leases"][lease.identifier]["expires_at"] == voice_clock.now + 400
            assert stored_record(application, container)["leases"] == {}
        with pytest.raises(voiceusage.VoiceUsageLimitExceeded, match="Daily voice"):
            async with voiceusage.voice_session_usage():
                pytest.fail("Daily voice cap must reject before entry")

    record = stored_record(application, container)
    assert record["admitted"] == 20
    assert set(record) - {"_etag"} == {"id", "type", "day", "admitted", "leases", "ttl"}
    assert record["ttl"] == -1


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_concurrent_workers_cannot_exceed_active_session_cap(voice_clock, voice_container, shared):
    container = voice_container if shared else None
    applications = [application_for(container) for worker in range(3)] if shared else [application_for()]
    finished_attempts = []
    all_attempted = asyncio.Event()
    release = asyncio.Event()

    async def admit(index):
        application = applications[index % len(applications)]
        async with application.app_context():
            try:
                async with voiceusage.voice_session_usage() as lease:
                    lease.admitted = True
                    finished_attempts.append(True)
                    if len(finished_attempts) == 10:
                        all_attempted.set()
                    await release.wait()
                    return True
            except voiceusage.VoiceUsageLimitExceeded:
                finished_attempts.append(False)
                if len(finished_attempts) == 10:
                    all_attempted.set()
                return False

    tasks = [asyncio.create_task(admit(index)) for index in range(10)]
    try:
        await asyncio.wait_for(all_attempted.wait(), timeout=3)
        assert finished_attempts.count(True) == 3
        assert len(stored_record(applications[0], container)["leases"]) == 3
    finally:
        release.set()
        results = await asyncio.gather(*tasks)
    assert sum(results) == 3
    assert stored_record(applications[0], container)["leases"] == {}
    assert stored_record(applications[0], container)["admitted"] == 3
    if shared:
        assert all(application.config[voiceusage.VOICE_USAGE_LOCAL_KEY].record is None for application in applications)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("admitted", [False, True])
async def test_failed_setup_refunds_but_started_sessions_count(voice_clock, voice_container, shared, admitted):
    container = voice_container if shared else None
    application = application_for(container)
    async with application.app_context():
        with pytest.raises(RuntimeError, match="setup failed"):
            async with voiceusage.voice_session_usage() as lease:
                lease.admitted = admitted
                raise RuntimeError("setup failed")
    record = stored_record(application, container)
    assert record["leases"] == {}
    assert record["admitted"] == int(admitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_expired_crashed_leases_free_capacity_without_refunding_daily_usage(voice_clock, voice_container, shared):
    container = voice_container if shared else None
    application = application_for(container)
    local = voiceusage.LocalVoiceUsage()
    application.config[voiceusage.VOICE_USAGE_LOCAL_KEY] = local
    crashed = [voiceusage.VoiceSessionLease(container, local) for count in range(3)]
    for lease in crashed:
        await lease.acquire()
    async with application.app_context():
        with pytest.raises(voiceusage.VoiceUsageLimitExceeded, match="capacity"):
            async with voiceusage.voice_session_usage():
                pytest.fail("Unexpired reservations must hold capacity")
        voice_clock.now += voiceusage.VOICE_LEASE_SECONDS
        async with voiceusage.voice_session_usage() as replacement:
            replacement.admitted = True
            assert set(stored_record(application, container)["leases"]) == {replacement.identifier}
            for lease in crashed:
                await lease.release()
            assert set(stored_record(application, container)["leases"]) == {replacement.identifier}
    assert stored_record(application, container)["admitted"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("shared", [False, True])
async def test_utc_rollover_keeps_active_leases_and_old_refunds_cannot_reduce_new_day_usage(
    voice_clock, voice_container, shared
):
    container = voice_container if shared else None
    application = application_for(container)
    voice_clock.now = voiceusage.SECONDS_PER_DAY - 1
    async with application.app_context():
        async with voiceusage.voice_session_usage() as yesterday:
            voice_clock.now += 2
            async with voiceusage.voice_session_usage() as today:
                today.admitted = True
                record = stored_record(application, container)
                assert record["admitted"] == 1
                assert set(record["leases"]) == {yesterday.identifier, today.identifier}
                assert record["day"] == 1
        assert stored_record(application, container)["admitted"] == 1
        assert stored_record(application, container)["leases"] == {}


@pytest.mark.asyncio
async def test_clock_moving_backwards_cannot_reset_daily_counter(voice_clock):
    application = application_for()
    voice_clock.now = voiceusage.SECONDS_PER_DAY + 10
    async with application.app_context():
        async with voiceusage.voice_session_usage() as first:
            first.admitted = True
        voice_clock.now -= 20
        async with voiceusage.voice_session_usage() as second:
            second.admitted = True
    assert stored_record(application, None)["admitted"] == 2
    assert stored_record(application, None)["day"] == 1


@pytest.mark.asyncio
async def test_release_is_idempotent(voice_clock, voice_container):
    application = application_for(voice_container)
    async with application.app_context():
        async with voiceusage.voice_session_usage() as lease:
            await lease.release()
            await lease.release()
    assert stored_record(application, voice_container)["admitted"] == 0
    assert stored_record(application, voice_container)["leases"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 429, 500])
async def test_configured_store_failure_is_explicit_without_local_fallback(voice_container, status):
    voice_container.read_item.side_effect = CosmosHttpResponseError(status_code=status, message="private-store-secret")
    application = application_for(voice_container)
    async with application.app_context():
        with pytest.raises(ServiceUnavailable, match="Voice capacity storage is unavailable"):
            async with voiceusage.voice_session_usage():
                pytest.fail("Storage failures must not admit a voice session")
    assert application.config[voiceusage.VOICE_USAGE_LOCAL_KEY].record is None
    voice_container.create_item.assert_not_awaited()
    voice_container.replace_item.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 409, 412])
async def test_contention_retries_are_bounded(voice_clock, voice_container, monkeypatch, status):
    monkeypatch.setattr(voiceusage.random, "uniform", lambda lower, upper: 0)
    voice_container.create_item.side_effect = CosmosHttpResponseError(status_code=status)
    application = application_for(voice_container)
    async with application.app_context():
        with pytest.raises(ServiceUnavailable):
            async with voiceusage.voice_session_usage():
                pytest.fail("Exhausted retries must fail closed")
    assert voice_container.create_item.await_count == voiceusage.MAX_STORE_ATTEMPTS
    assert voice_container.read_item.await_count == voiceusage.MAX_STORE_ATTEMPTS + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "other"),
        ("type", "settings"),
        ("day", True),
        ("day", -1),
        ("admitted", True),
        ("admitted", -1),
        ("admitted", 21),
        ("leases", []),
        ("leases", {"a" * 32: {"day": 0, "expires_at": float("nan")}}),
        ("leases", {"invalid": {"day": 0, "expires_at": 1000}}),
        ("leases", {"a" * 32: {"day": True, "expires_at": 1000}}),
        ("leases", {"a" * 32: {"day": 0, "expires_at": True}}),
        ("leases", {"a" * 32: {"day": 0, "expires_at": 1000}}),
        ("ttl", 60),
        ("_etag", "*"),
        ("_etag", ""),
        ("_etag", None),
    ],
)
async def test_corrupt_store_records_fail_closed(voice_clock, voice_container, field, value):
    record = {
        "id": voiceusage.VOICE_USAGE_DOCUMENT_ID,
        "type": "voice_usage",
        "day": 0,
        "admitted": 0,
        "leases": {},
        "ttl": -1,
        "_etag": "1",
    }
    record[field] = value
    voice_container.documents[voiceusage.VOICE_USAGE_DOCUMENT_ID] = record
    application = application_for(voice_container)
    async with application.app_context():
        with pytest.raises(ServiceUnavailable):
            async with voiceusage.voice_session_usage():
                pytest.fail("Malformed records must not bypass limits")
    voice_container.create_item.assert_not_awaited()
    voice_container.replace_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_uncertain_reservation_is_found_and_refunded(voice_clock, voice_container):
    committed = asyncio.Event()
    cancelled_write = asyncio.Event()
    create = voice_container.create_item.side_effect

    async def create_then_block(*, body):
        await create(body=body)
        committed.set()
        try:
            await asyncio.Future()
        finally:
            cancelled_write.set()

    voice_container.create_item.side_effect = create_then_block
    application = application_for(voice_container)

    async def connect():
        async with application.app_context():
            async with voiceusage.voice_session_usage():
                pytest.fail("Cancelled reservation must never enter")

    task = asyncio.create_task(connect())
    await asyncio.wait_for(committed.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert cancelled_write.is_set()
    assert stored_record(application, voice_container)["admitted"] == 0
    assert stored_record(application, voice_container)["leases"] == {}


@pytest.mark.asyncio
async def test_cancellation_waits_for_inflight_release(voice_clock, voice_container):
    releasing = asyncio.Event()
    allow_release = asyncio.Event()
    replace = voice_container.replace_item.side_effect

    async def delayed_release(**kwargs):
        releasing.set()
        await allow_release.wait()
        await replace(**kwargs)

    voice_container.replace_item.side_effect = delayed_release
    application = application_for(voice_container)

    async def connect():
        async with application.app_context():
            async with voiceusage.voice_session_usage() as lease:
                lease.admitted = True

    task = asyncio.create_task(connect())
    await asyncio.wait_for(releasing.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    allow_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert stored_record(application, voice_container)["leases"] == {}
    assert stored_record(application, voice_container)["admitted"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("original_failure", [False, True])
async def test_release_failure_is_explicit_sanitized_and_does_not_mask_original_failure(
    voice_clock, voice_container, caplog, original_failure
):
    voice_container.replace_item.side_effect = RuntimeError("private-store-secret")
    application = application_for(voice_container)
    failure_type = RuntimeError if original_failure else ServiceUnavailable
    async with application.app_context():
        with pytest.raises(failure_type):
            async with voiceusage.voice_session_usage() as lease:
                lease.admitted = True
                if original_failure:
                    raise RuntimeError("original failure")
    assert "reservation will expire" in caplog.text
    assert "private-store-secret" not in caplog.text
    assert len(stored_record(application, voice_container)["leases"]) == 1
    assert application.config[voiceusage.VOICE_USAGE_LOCAL_KEY].record is None


@pytest.mark.asyncio
async def test_store_timeout_is_bounded_and_cancels_pending_read(voice_container, monkeypatch):
    cancelled = asyncio.Event()

    async def block(**kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    voice_container.read_item.side_effect = block
    monkeypatch.setattr(voiceusage, "STORE_OPERATION_TIMEOUT_SECONDS", 0.02)
    application = application_for(voice_container)
    async with application.app_context():
        with pytest.raises(asyncio.TimeoutError):
            async with voiceusage.voice_session_usage():
                pytest.fail("Storage timeout must stop admission")
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_disabled_store_retry_budget_cannot_admit_a_session(voice_container, monkeypatch):
    monkeypatch.setattr(voiceusage, "MAX_STORE_ATTEMPTS", 0)
    application = application_for(voice_container)
    async with application.app_context():
        with pytest.raises(ServiceUnavailable):
            async with voiceusage.voice_session_usage():
                pytest.fail("No retry budget must fail closed")
    voice_container.read_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_release_failure_during_cancellation_preserves_cancellation(voice_clock, voice_container, caplog):
    releasing = asyncio.Event()
    allow_failure = asyncio.Event()

    async def fail_release(**kwargs):
        releasing.set()
        await allow_failure.wait()
        raise RuntimeError("private-store-secret")

    voice_container.replace_item.side_effect = fail_release
    application = application_for(voice_container)

    async def connect():
        async with application.app_context():
            async with voiceusage.voice_session_usage() as lease:
                lease.admitted = True

    task = asyncio.create_task(connect())
    await asyncio.wait_for(releasing.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    allow_failure.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
    assert len(stored_record(application, voice_container)["leases"]) == 1
    assert "reservation will expire" in caplog.text
    assert "private-store-secret" not in caplog.text
