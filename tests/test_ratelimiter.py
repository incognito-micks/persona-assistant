import asyncio
from copy import deepcopy
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

from config import (
    CONFIG_COSMOS_SETTINGS_CONTAINER,
    CONFIG_PUBLIC_ACCESS_ENABLED,
    CONFIG_PUBLIC_USAGE_LIMITER,
    CONFIG_RATE_LIMITER,
)
from core.ratelimiter import RateLimiter, check_rate_limit


def test_rate_limiter_enforces_window(monkeypatch):
    monkeypatch.setattr("core.ratelimiter.time.monotonic", lambda: 100)
    limiter = RateLimiter(max_requests=2)
    assert not limiter.is_rate_limited("user")
    assert not limiter.is_rate_limited("user")
    assert limiter.is_rate_limited("user")
    monkeypatch.setattr("core.ratelimiter.time.monotonic", lambda: 160)
    assert not limiter.is_rate_limited("user")


def test_rate_limiter_evicts_expired_users(monkeypatch):
    monkeypatch.setattr("core.ratelimiter.time.monotonic", lambda: 100)
    limiter = RateLimiter(max_users=2)
    assert not limiter.is_rate_limited("first")
    assert not limiter.is_rate_limited("second")
    monkeypatch.setattr("core.ratelimiter.time.monotonic", lambda: 160)
    assert not limiter.is_rate_limited("third")
    assert set(limiter.users) == {"third"}


def test_rate_limiter_preserves_active_limits_at_capacity():
    limiter = RateLimiter(max_requests=1, max_users=2)
    assert not limiter.is_rate_limited("first")
    assert not limiter.is_rate_limited("second")
    assert limiter.is_rate_limited("third")
    assert limiter.is_rate_limited("first")
    assert len(limiter.users) == 2


@pytest.mark.parametrize("setting", ["max_requests", "window_seconds", "max_users"])
def test_rate_limiter_requires_positive_limits(setting):
    with pytest.raises(ValueError):
        RateLimiter(**{setting: 0})


@pytest.mark.asyncio
async def test_check_rate_limit_uses_stable_authenticated_identity():
    application = Quart(__name__)
    limiter = RateLimiter(max_requests=1)
    application.config[CONFIG_RATE_LIMITER] = limiter
    async with application.app_context():
        assert not await check_rate_limit({"oid": "user-oid", "access_token": "first-token"}, "192.0.2.1")
        assert await check_rate_limit({"oid": "user-oid", "access_token": "second-token"}, "192.0.2.2")
        assert not await check_rate_limit({"oid": "other-user"}, "192.0.2.1")
    assert all(key.startswith("rate_limit:") and len(key) == 75 for key in limiter.users)
    assert all("user-oid" not in key and "token" not in key for key in limiter.users)


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{}, {"oid": None}, {"oid": ""}, {"oid": "anonymous"}])
async def test_check_rate_limit_limits_anonymous_addresses(claims):
    application = Quart(__name__)
    application.config[CONFIG_RATE_LIMITER] = RateLimiter(max_requests=1)
    async with application.app_context():
        assert not await check_rate_limit(claims, "192.0.2.1")
        assert await check_rate_limit(claims, "192.0.2.1")
        assert not await check_rate_limit(claims, "192.0.2.2")


@pytest.mark.asyncio
async def test_check_rate_limit_missing_address_uses_shared_fallback():
    application = Quart(__name__)
    application.config[CONFIG_RATE_LIMITER] = RateLimiter(max_requests=1)
    async with application.app_context():
        assert not await check_rate_limit({}, None)
        assert await check_rate_limit({}, "")


@pytest.mark.asyncio
async def test_check_rate_limit_initializes_local_default():
    application = Quart(__name__)
    async with application.app_context():
        assert not await check_rate_limit({}, None)
    assert isinstance(application.config[CONFIG_RATE_LIMITER], RateLimiter)


@pytest.mark.asyncio
async def test_public_kill_switch_prevents_all_admissions():
    application = Quart(__name__)
    application.config[CONFIG_PUBLIC_ACCESS_ENABLED] = False
    async with application.app_context():
        with pytest.raises(ServiceUnavailable, match="temporarily disabled"):
            await check_rate_limit({}, "192.0.2.1")
    assert CONFIG_RATE_LIMITER not in application.config


@pytest.mark.asyncio
async def test_daily_limit_is_independent_of_visitor_identity():
    application = Quart(__name__)
    application.config[CONFIG_PUBLIC_USAGE_LIMITER] = RateLimiter(max_requests=2, window_seconds=86400, max_users=1)
    async with application.app_context():
        assert not await check_rate_limit({}, "192.0.2.1")
        assert not await check_rate_limit({}, "192.0.2.2")
        assert await check_rate_limit({}, "192.0.2.3")


@pytest.fixture
def shared_rate_limit_container():
    container = AsyncMock()
    container.documents = {}

    async def read_item(*, item, partition_key):
        assert item == partition_key
        stored = deepcopy(container.documents.get(item))
        await asyncio.sleep(0)
        if stored is None:
            raise CosmosResourceNotFoundError(status_code=404)
        return stored

    async def create_item(*, body):
        if body["id"] in container.documents:
            raise CosmosResourceExistsError(status_code=409)
        container.documents[body["id"]] = {**deepcopy(body), "_etag": "1"}

    async def replace_item(*, item, body, etag, match_condition):
        assert item == body["id"]
        assert match_condition == MatchConditions.IfNotModified
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


@pytest.mark.asyncio
@pytest.mark.parametrize("claims", [{}, {"oid": "private-user", "access_token": "secret-token"}])
async def test_check_rate_limit_shared_workers_and_expiration(shared_rate_limit_container, monkeypatch, claims):
    monkeypatch.setattr("core.ratelimiter.time.time", lambda: 100)
    applications = [Quart("first-worker"), Quart("second-worker")]
    for application in applications:
        application.config[CONFIG_RATE_LIMITER] = RateLimiter(max_requests=2)
        application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = shared_rate_limit_container
        async with application.app_context():
            assert not await check_rate_limit(claims, "192.0.2.1")
        assert not application.config[CONFIG_RATE_LIMITER].users

    async with applications[0].app_context():
        assert await check_rate_limit(claims, "192.0.2.1")
        monkeypatch.setattr("core.ratelimiter.time.time", lambda: 160)
        assert not await check_rate_limit(claims, "192.0.2.1")

    stored = next(iter(shared_rate_limit_container.documents.values()))
    assert stored["type"] == "rate_limit"
    assert stored["id"].startswith("rate_limit:") and len(stored["id"]) == 75
    assert all(secret not in str(stored) for secret in ("private-user", "secret-token", "192.0.2.1"))
    assert stored["timestamps"] == [160]
    assert stored["expires_at"] == 220
    assert stored["ttl"] == 60


@pytest.mark.asyncio
async def test_check_rate_limit_shared_sliding_window(shared_rate_limit_container, monkeypatch):
    application = Quart(__name__)
    application.config[CONFIG_RATE_LIMITER] = RateLimiter(max_requests=2)
    application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = shared_rate_limit_container
    async with application.app_context():
        monkeypatch.setattr("core.ratelimiter.time.time", lambda: 100)
        assert not await check_rate_limit({}, None)
        monkeypatch.setattr("core.ratelimiter.time.time", lambda: 130)
        assert not await check_rate_limit({}, None)
        monkeypatch.setattr("core.ratelimiter.time.time", lambda: 159)
        assert await check_rate_limit({}, None)
        monkeypatch.setattr("core.ratelimiter.time.time", lambda: 160)
        assert not await check_rate_limit({}, None)
        assert await check_rate_limit({}, None)
    stored = next(iter(shared_rate_limit_container.documents.values()))
    assert stored["timestamps"] == [130, 160]


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting", [False, True])
async def test_check_rate_limit_shared_concurrent_requests(shared_rate_limit_container, preexisting):
    applications = [Quart("first-worker"), Quart("second-worker")]
    for application in applications:
        application.config[CONFIG_RATE_LIMITER] = RateLimiter(max_requests=5)
        application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = shared_rate_limit_container

    async def check(application):
        async with application.app_context():
            return await check_rate_limit({"oid": "same-user"}, "192.0.2.1")

    if preexisting:
        assert not await check(applications[0])
    results = await asyncio.gather(*(check(applications[index % 2]) for index in range(20)))
    assert results.count(False) == 5 - int(preexisting)
    assert results.count(True) == 15 + int(preexisting)
    stored = next(iter(shared_rate_limit_container.documents.values()))
    assert len(stored["timestamps"]) == 5
    if preexisting:
        assert shared_rate_limit_container.replace_item.await_count > 4
    else:
        assert shared_rate_limit_container.create_item.await_count > 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 409, 412])
async def test_check_rate_limit_retries_concurrent_changes(shared_rate_limit_container, status_code):
    application = Quart(__name__)
    application.config[CONFIG_RATE_LIMITER] = RateLimiter(max_requests=2)
    application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = shared_rate_limit_container
    async with application.app_context():
        assert not await check_rate_limit({}, None)
        replace = shared_rate_limit_container.replace_item.side_effect

        async def replace_after_conflict(**kwargs):
            shared_rate_limit_container.replace_item.side_effect = replace
            raise CosmosHttpResponseError(status_code=status_code)

        shared_rate_limit_container.replace_item.side_effect = replace_after_conflict
        assert not await check_rate_limit({}, None)
        assert await check_rate_limit({}, None)
    assert shared_rate_limit_container.replace_item.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [CosmosHttpResponseError(status_code=500), OSError("offline")])
async def test_check_rate_limit_shared_store_failure_is_closed(failure):
    application = Quart(__name__)
    limiter = RateLimiter()
    application.config[CONFIG_RATE_LIMITER] = limiter
    container = AsyncMock()
    container.read_item.side_effect = failure
    application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = container
    async with application.app_context():
        with pytest.raises(ServiceUnavailable):
            await check_rate_limit({}, "192.0.2.1")
    assert not limiter.users
    container.create_item.assert_not_awaited()
    container.replace_item.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create_item", "replace_item"])
async def test_check_rate_limit_shared_write_failure_is_closed(shared_rate_limit_container, operation):
    application = Quart(__name__)
    application.config[CONFIG_RATE_LIMITER] = RateLimiter(max_requests=2)
    application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = shared_rate_limit_container
    async with application.app_context():
        if operation == "replace_item":
            assert not await check_rate_limit({}, None)
        getattr(shared_rate_limit_container, operation).side_effect = CosmosHttpResponseError(status_code=503)
        with pytest.raises(ServiceUnavailable):
            await check_rate_limit({}, None)
    assert not application.config[CONFIG_RATE_LIMITER].users


@pytest.mark.asyncio
async def test_check_rate_limit_exhausted_conflicts_fail_closed(shared_rate_limit_container, monkeypatch):
    application = Quart(__name__)
    application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = shared_rate_limit_container
    shared_rate_limit_container.create_item.side_effect = CosmosResourceExistsError(status_code=409)
    monkeypatch.setattr("core.ratelimiter.random.uniform", lambda *args: 0)
    async with application.app_context():
        with pytest.raises(ServiceUnavailable):
            await check_rate_limit({}, None)
    assert shared_rate_limit_container.create_item.await_count == 8
    assert not application.config[CONFIG_RATE_LIMITER].users


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_fields",
    [{"_etag": "*"}, {"_etag": None}, {"type": "admin_settings"}, {"timestamps": ["bad"]}, {"expires_at": None}],
)
async def test_check_rate_limit_invalid_shared_record_fails_closed(shared_rate_limit_container, invalid_fields):
    application = Quart(__name__)
    application.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = shared_rate_limit_container
    async with application.app_context():
        assert not await check_rate_limit({}, None)
        stored = next(iter(shared_rate_limit_container.documents.values()))
        stored.update(invalid_fields)
        with pytest.raises(ServiceUnavailable):
            await check_rate_limit({}, None)
    shared_rate_limit_container.replace_item.assert_not_awaited()
