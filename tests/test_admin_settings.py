import json
from copy import deepcopy
from datetime import UTC, datetime
from unittest import mock

import pytest
from azure.core import MatchConditions
from azure.core.pipeline.transport import AsyncHttpResponse, AsyncHttpTransport
from azure.cosmos import PartitionKey
from azure.cosmos.aio import ContainerProxy, CosmosClient
from azure.cosmos.exceptions import (
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from quart import Quart

from admin import settings_cosmosdb
from config import (
    CONFIG_ADMIN_SETTINGS_CACHE,
    CONFIG_ADMIN_USER_OID,
    CONFIG_AUTH_CLIENT,
    CONFIG_COSMOS_ANALYTICS_CONTAINER,
    CONFIG_COSMOS_SETTINGS_CONTAINER,
    CONFIG_CREDENTIAL,
)
from tests.mocks import MockAzureCredential


@pytest.fixture(autouse=True)
def isolate_settings_storage(monkeypatch):
    for variable in (
        "AZURE_COSMOSDB_ACCOUNT",
        "AZURE_ADMIN_SETTINGS_DATABASE",
        "AZURE_CHAT_HISTORY_DATABASE",
        "AZURE_ADMIN_SETTINGS_CONTAINER",
        "AZURE_ANALYTICS_CONTAINER",
    ):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(
        settings_cosmosdb,
        "CosmosClient",
        mock.Mock(side_effect=AssertionError("Cosmos must be mocked in settings tests")),
    )


@pytest.fixture
def admin_app_factory():
    def create_app(container=None, settings=None):
        app = Quart(__name__)
        app.config.update(
            {
                "TESTING": True,
                CONFIG_ADMIN_USER_OID: "admin-oid",
                CONFIG_AUTH_CLIENT: mock.Mock(
                    get_auth_claims_if_enabled=mock.AsyncMock(return_value={"oid": "admin-oid", "auth_provider": "aad"})
                ),
                CONFIG_CREDENTIAL: mock.Mock(),
                CONFIG_ADMIN_SETTINGS_CACHE: deepcopy(
                    settings_cosmosdb.DEFAULT_SETTINGS if settings is None else settings
                ),
                CONFIG_COSMOS_SETTINGS_CONTAINER: container,
            }
        )
        app.register_blueprint(settings_cosmosdb.admin_settings_bp)
        return app

    return create_app


@pytest.fixture
def settings_container():
    container = mock.Mock(spec=ContainerProxy)
    container.document = {**deepcopy(settings_cosmosdb.DEFAULT_SETTINGS), "_etag": '"version-1"'}
    container.revision = 1

    async def read_item(item, partition_key):
        assert item == partition_key == "admin_settings"
        return deepcopy(container.document)

    async def replace_item(item, body, *, etag, match_condition):
        assert item == body["id"] == "admin_settings"
        assert match_condition == MatchConditions.IfNotModified
        if etag != container.document["_etag"]:
            raise CosmosHttpResponseError(status_code=412, message="Settings changed")
        container.revision += 1
        container.document = {**deepcopy(body), "_etag": f'"version-{container.revision}"'}
        return deepcopy(container.document)

    container.read_item = mock.AsyncMock(side_effect=read_item)
    container.replace_item = mock.AsyncMock(side_effect=replace_item)
    return container


@pytest.fixture
def cosmos_setup(monkeypatch, settings_container):
    analytics_container = mock.Mock(read=mock.AsyncMock())
    database = mock.Mock(create_container_if_not_exists=mock.AsyncMock(return_value=analytics_container))
    database.get_container_client.side_effect = {
        "admin_settings": settings_container,
        "analytics": analytics_container,
    }.__getitem__
    client = mock.Mock(close=mock.AsyncMock())
    client.get_database_client.return_value = database
    monkeypatch.setattr(settings_cosmosdb, "CosmosClient", mock.Mock(return_value=client))
    monkeypatch.setenv("AZURE_COSMOSDB_ACCOUNT", "mock-account")
    monkeypatch.setenv("AZURE_ADMIN_SETTINGS_DATABASE", "settings-db")
    return client, database, analytics_container


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/api/admin/settings", "/api/status"])
async def test_settings_reads_persisted_document(admin_app_factory, route):
    stored_settings = {
        **deepcopy(settings_cosmosdb.DEFAULT_SETTINGS),
        "vacation_mode": True,
        "owner_name": "Current owner",
        "_etag": '"version-2"',
        "_rid": "internal-resource-id",
        "confidence_hard_threshold": 1.0,
        "confidence_soft_threshold": 2.0,
    }
    container = mock.Mock(read_item=mock.AsyncMock(return_value=stored_settings))
    app = admin_app_factory(container=container)

    response = await app.test_client().get(route)

    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["vacation_mode"] is True
    assert payload["owner_name"] == "Current owner"
    assert "_etag" not in payload
    assert "_rid" not in payload
    assert "id" not in payload
    assert "confidence_hard_threshold" not in payload
    assert "confidence_soft_threshold" not in payload
    container.read_item.assert_awaited_once_with(item="admin_settings", partition_key="admin_settings")


@pytest.mark.asyncio
async def test_mentorship_profile_default_persisted_update_and_clear_preserve_general_contacts(
    admin_app_factory, settings_container
):
    settings_container.document.pop("mentorship_url", None)
    settings_container.document["calendar_url"] = "https://calendar.example.test/general"
    settings_container.document["owner_email"] = "owner@example.test"
    app = admin_app_factory(container=settings_container)
    app.config[settings_cosmosdb.CONFIG_PROFILE] = {"mentorship_url": "https://mentoring.example.test/profile-default"}
    client = app.test_client()

    for route in ("/api/admin/settings", "/api/status"):
        response = await client.get(route)
        assert response.status_code == 200
        assert (await response.get_json())["mentorship_url"] == "https://mentoring.example.test/profile-default"

    for value in ("https://mentoring.example.test/updated", ""):
        previous = await (await client.get("/api/admin/settings")).get_json()
        response = await client.put("/api/admin/settings", json={"mentorship_url": value})
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload == previous | {"mentorship_url": value}
        assert payload["mentorship_url"] == value
        assert payload["calendar_url"] == "https://calendar.example.test/general"
        assert payload["owner_email"] == "owner@example.test"
        assert settings_container.document["mentorship_url"] == value
        public = await (await client.get("/api/status")).get_json()
        assert public["mentorship_url"] == value
        assert "manager_email" not in public and "fun_facts" not in public
    assert settings_container.replace_item.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["http://example.test", "javascript:alert(1)", "//example.test/book", "https://user:secret@example.test"]
)
async def test_invalid_mentorship_url_is_rejected_before_storage(admin_app_factory, settings_container, value):
    app = admin_app_factory(container=settings_container)
    response = await app.test_client().put("/api/admin/settings", json={"mentorship_url": value})
    assert response.status_code == 400
    settings_container.read_item.assert_not_awaited()
    settings_container.replace_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_current_settings_fallback_is_sanitized_and_independent(admin_app_factory):
    app = admin_app_factory(
        settings={"manager_email": "manager@example.com", "fun_facts": ["A fact"], "_etag": "private"}
    )

    async with app.app_context():
        settings = await settings_cosmosdb.get_current_settings()
        assert set(settings) == settings_cosmosdb.SETTINGS_FIELDS
        assert settings["manager_email"] == "manager@example.com"
        assert settings["vacation_mode"] is False
        settings["fun_facts"].append("Another fact")
        assert app.config[CONFIG_ADMIN_SETTINGS_CACHE]["fun_facts"] == ["A fact"]

        app.config.pop(CONFIG_ADMIN_SETTINGS_CACHE)
        original_facts = deepcopy(settings_cosmosdb.DEFAULT_SETTINGS["fun_facts"])
        defaults = await settings_cosmosdb.get_current_settings()
        defaults["fun_facts"].clear()
        assert settings_cosmosdb.DEFAULT_SETTINGS["fun_facts"] == original_facts


@pytest.mark.asyncio
async def test_two_workers_read_latest_settings_and_merge_partial_updates(admin_app_factory, settings_container):
    first_worker = admin_app_factory(container=settings_container)
    second_worker = admin_app_factory(container=settings_container)

    response = await first_worker.test_client().put(
        "/api/admin/settings", json={"vacation_mode": True, "manager_email": "manager@example.com"}
    )
    assert response.status_code == 200
    assert second_worker.config[CONFIG_ADMIN_SETTINGS_CACHE]["vacation_mode"] is False

    for route in ("/api/admin/settings", "/api/status"):
        response = await second_worker.test_client().get(route)
        assert response.status_code == 200
        assert (await response.get_json())["vacation_mode"] is True

    response = await second_worker.test_client().put("/api/admin/settings", json={"owner_name": "Updated owner"})
    assert response.status_code == 200
    payload = await response.get_json()
    assert payload["vacation_mode"] is True
    assert payload["manager_email"] == "manager@example.com"
    assert payload["owner_name"] == "Updated owner"
    assert set(payload) == settings_cosmosdb.SETTINGS_FIELDS
    settings_container.upsert_item.assert_not_called()
    assert settings_container.replace_item.await_args.kwargs["etag"] == '"version-2"'
    assert set(settings_container.replace_item.await_args.kwargs["body"]) == settings_cosmosdb.SETTINGS_FIELDS | {"id"}

    response = await first_worker.test_client().get("/api/admin/settings")
    assert (await response.get_json())["owner_name"] == "Updated owner"


@pytest.mark.asyncio
async def test_conditional_write_conflict_preserves_other_changes(admin_app_factory, settings_container):
    original_replace = settings_container.replace_item.side_effect

    async def concurrent_replace(*args, **kwargs):
        settings_container.document["manager_email"] = "concurrent@example.com"
        settings_container.document["_etag"] = '"concurrent-version"'
        return await original_replace(*args, **kwargs)

    settings_container.replace_item.side_effect = concurrent_replace
    app = admin_app_factory(container=settings_container)
    cache_before = deepcopy(app.config[CONFIG_ADMIN_SETTINGS_CACHE])

    response = await app.test_client().put("/api/admin/settings", json={"owner_name": "Conflicting owner"})

    assert response.status_code == 409
    assert "Reload settings" in (await response.get_json())["error"]
    assert settings_container.document["manager_email"] == "concurrent@example.com"
    assert settings_container.document["owner_name"] == settings_cosmosdb.DEFAULT_SETTINGS["owner_name"]
    assert app.config[CONFIG_ADMIN_SETTINGS_CACHE] == cache_before
    assert settings_container.replace_item.await_count == 1
    settings_container.upsert_item.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["/api/admin/settings", "/api/status"])
async def test_settings_read_failure_does_not_use_cache(admin_app_factory, settings_container, route):
    settings_container.read_item.side_effect = CosmosHttpResponseError(status_code=503, message="Storage unavailable")
    app = admin_app_factory(container=settings_container)

    response = await app.test_client().get(route)

    assert response.status_code == 500
    assert "vacation_mode" not in await response.get_json()
    async with app.app_context():
        with pytest.raises(CosmosHttpResponseError):
            await settings_cosmosdb.get_current_settings()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        [],
        "settings",
        {"unknown": True},
        {"id": "different"},
        {"_etag": "client-version"},
        {"confidence_hard_threshold": 1.0},
        {"confidence_soft_threshold": 2.0},
        {"vacation_mode": 1},
        {"vacation_mode": "false"},
        {"vacation_mode": None},
        {"vacation_return_date": False},
        {"vacation_return_date": "2026-02-30"},
        {"vacation_return_date": "20260917"},
        {"fun_facts": None},
        {"fun_facts": "a fact"},
        {"fun_facts": {}},
        {"fun_facts": [None]},
        {"fun_facts": [1]},
        {"fun_facts": ["fact"] * (settings_cosmosdb.MAX_FUN_FACTS + 1)},
        {"fun_facts": ["a" * (settings_cosmosdb.MAX_FUN_FACT_LENGTH + 1)]},
        *[{field: None} for field in settings_cosmosdb.SETTINGS_TEXT_LIMITS],
        *[{field: "a" * (limit + 1)} for field, limit in settings_cosmosdb.SETTINGS_TEXT_LIMITS.items()],
    ],
)
async def test_invalid_settings_rejected_before_storage(admin_app_factory, settings_container, payload):
    app = admin_app_factory(container=settings_container)

    response = await app.test_client().put("/api/admin/settings", json=payload)

    assert response.status_code == 400
    assert (await response.get_json())["error"]
    settings_container.read_item.assert_not_awaited()
    settings_container.replace_item.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type", "expected_status"),
    [
        ("{}", "text/plain", 415),
        ("{", "application/json", 400),
        ("null", "application/json", 400),
        ("", "application/json", 400),
        (" " * (settings_cosmosdb.MAX_SETTINGS_REQUEST_BYTES + 1), "application/json", 413),
    ],
)
async def test_invalid_settings_request_body(
    admin_app_factory, settings_container, body, content_type, expected_status
):
    app = admin_app_factory(container=settings_container)

    response = await app.test_client().put("/api/admin/settings", data=body, headers={"Content-Type": content_type})

    assert response.status_code == expected_status
    settings_container.read_item.assert_not_awaited()
    settings_container.replace_item.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claims", "admin_oid"),
    [
        ({}, "admin-oid"),
        ({"oid": "other-oid", "auth_provider": "aad"}, "admin-oid"),
        ({"oid": "admin-oid", "auth_provider": "aad"}, None),
        ({"oid": "admin-oid", "auth_provider": "aad"}, ""),
        ({"oid": "admin-oid", "auth_provider": "b2c"}, "admin-oid"),
    ],
)
@pytest.mark.parametrize(
    ("method", "route"),
    [("get", "/api/admin/settings"), ("put", "/api/admin/settings"), ("get", "/api/admin/analytics")],
)
async def test_admin_routes_forbid_non_admins(admin_app_factory, settings_container, claims, admin_oid, method, route):
    app = admin_app_factory(container=settings_container)
    app.config[CONFIG_ADMIN_USER_OID] = admin_oid
    app.config[CONFIG_AUTH_CLIENT].get_auth_claims_if_enabled.return_value = claims

    response = await getattr(app.test_client(), method)(route, json={})

    assert response.status_code == 403
    settings_container.read_item.assert_not_awaited()
    settings_container.replace_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_public_status_does_not_require_auth_or_expose_admin_fields(admin_app_factory, settings_container):
    settings_container.document.update(
        {"manager_email": "private@example.com", "fun_facts": ["Private fact"], "vacation_mode": True}
    )
    app = admin_app_factory(container=settings_container)
    auth = app.config[CONFIG_AUTH_CLIENT].get_auth_claims_if_enabled
    auth.side_effect = AssertionError("Public status must not authenticate")

    response = await app.test_client().get("/api/status")

    assert response.status_code == 200
    payload = await response.get_json()
    public_fields = settings_cosmosdb.SETTINGS_FIELDS - {"manager_email", "fun_facts"}
    assert payload == {field: settings_container.document[field] for field in public_fields}
    auth.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("variable", [None, "AZURE_COSMOSDB_ACCOUNT", "AZURE_ADMIN_SETTINGS_DATABASE"])
async def test_setup_without_storage_uses_independent_defaults(admin_app_factory, monkeypatch, variable):
    if variable:
        monkeypatch.setenv(variable, "configured")
    app = admin_app_factory(settings={"owner_name": "Cached owner"})

    async with app.app_context():
        await settings_cosmosdb.setup_clients()
        assert app.config[CONFIG_ADMIN_SETTINGS_CACHE] == settings_cosmosdb.DEFAULT_SETTINGS
        original_facts = deepcopy(settings_cosmosdb.DEFAULT_SETTINGS["fun_facts"])
        app.config[CONFIG_ADMIN_SETTINGS_CACHE]["fun_facts"].clear()
        assert settings_cosmosdb.DEFAULT_SETTINGS["fun_facts"] == original_facts
        await settings_cosmosdb.close_clients()

    settings_cosmosdb.CosmosClient.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("variable", ["RUNNING_IN_PRODUCTION", "WEBSITE_HOSTNAME"])
async def test_hosted_setup_requires_shared_usage_storage(admin_app_factory, monkeypatch, variable):
    monkeypatch.setenv(variable, "true" if variable == "RUNNING_IN_PRODUCTION" else "example.invalid")
    app = admin_app_factory()
    async with app.app_context():
        with pytest.raises(ValueError, match="shared usage storage"):
            await settings_cosmosdb.setup_clients()
    settings_cosmosdb.CosmosClient.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_history_database", [False, True])
async def test_setup_loads_sanitized_settings_and_closes_clients(
    admin_app_factory, settings_container, cosmos_setup, monkeypatch, use_history_database
):
    client, database, analytics_container = cosmos_setup
    settings_container.document.update(
        {"owner_name": "Persisted owner", "confidence_hard_threshold": 1.0, "_rid": "id"}
    )
    if use_history_database:
        monkeypatch.delenv("AZURE_ADMIN_SETTINGS_DATABASE")
        monkeypatch.setenv("AZURE_CHAT_HISTORY_DATABASE", "history-db")
    app = admin_app_factory()

    async with app.app_context():
        await settings_cosmosdb.setup_clients()
        assert app.config[CONFIG_COSMOS_SETTINGS_CONTAINER] is settings_container
        assert app.config[CONFIG_COSMOS_ANALYTICS_CONTAINER] is analytics_container
        cached = app.config[CONFIG_ADMIN_SETTINGS_CACHE]
        assert cached["owner_name"] == "Persisted owner"
        assert set(cached) == settings_cosmosdb.SETTINGS_FIELDS
        await settings_cosmosdb.close_clients()

    client.get_database_client.assert_called_once_with("history-db" if use_history_database else "settings-db")
    analytics_container.read.assert_awaited_once()
    client.close.assert_awaited_once()
    settings_container.upsert_item.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_seed", [False, True])
async def test_setup_seeding_does_not_overwrite_another_worker(
    admin_app_factory, settings_container, cosmos_setup, concurrent_seed
):
    stored = deepcopy(settings_container.document)
    if concurrent_seed:
        stored["manager_email"] = "concurrent@example.com"
        stored["vacation_mode"] = True
        settings_container.create_item.side_effect = CosmosResourceExistsError(
            status_code=409, message="Already seeded"
        )
    settings_container.read_item.side_effect = [
        CosmosResourceNotFoundError(status_code=404, message="Not seeded"),
        stored,
    ]
    app = admin_app_factory()

    async with app.app_context():
        await settings_cosmosdb.setup_clients()

    assert app.config[CONFIG_ADMIN_SETTINGS_CACHE] == settings_cosmosdb.sanitize_settings(stored)
    settings_container.create_item.assert_awaited_once_with(settings_cosmosdb.DEFAULT_SETTINGS)
    settings_container.upsert_item.assert_not_called()
    settings_container.replace_item.assert_not_called()


@pytest.mark.asyncio
async def test_configured_storage_setup_failure_is_not_a_default_fallback(
    admin_app_factory, settings_container, cosmos_setup
):
    client, database, analytics_container = cosmos_setup
    settings_container.read_item.side_effect = CosmosHttpResponseError(status_code=503, message="Unavailable")
    app = admin_app_factory()

    async with app.app_context():
        with pytest.raises(CosmosHttpResponseError):
            await settings_cosmosdb.setup_clients()
        with pytest.raises(CosmosHttpResponseError):
            await settings_cosmosdb.get_current_settings()

    client.close.assert_awaited_once()
    assert settings_cosmosdb.COSMOS_SETTINGS_CLIENT_KEY not in app.config
    analytics_container.read.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("creation_fails", [False, True])
async def test_setup_analytics_container_creation(admin_app_factory, settings_container, cosmos_setup, creation_fails):
    client, database, analytics_container = cosmos_setup
    analytics_container.read.side_effect = CosmosResourceNotFoundError(status_code=404, message="Missing analytics")
    if creation_fails:
        database.create_container_if_not_exists.side_effect = CosmosHttpResponseError(
            status_code=503, message="Unavailable"
        )
    app = admin_app_factory()

    async with app.app_context():
        await settings_cosmosdb.setup_clients()

    database.create_container_if_not_exists.assert_awaited_once_with(
        id="analytics", partition_key=PartitionKey(path="/user_oid")
    )
    assert app.config[CONFIG_COSMOS_ANALYTICS_CONTAINER] is (None if creation_fails else analytics_container)
    assert app.config[CONFIG_ADMIN_SETTINGS_CACHE] == settings_cosmosdb.sanitize_settings(settings_container.document)


async def analytics_rows(rows):
    for row in rows:
        yield row


@pytest.mark.asyncio
async def test_analytics_summarizes_every_projected_record_and_bounds_recent_activity(admin_app_factory, monkeypatch):
    monkeypatch.setattr(settings_cosmosdb.time, "time", lambda: 2000000000)
    records = [
        {
            "timestamp": 2000000000000 - (index % 2) * 86400000 - index,
            "session_id": f"session-{index % 2}",
            "response_time_ms": 125.7,
            "is_example_question": index % 4 == 0,
            "error": "Legacy private error" if index % 100 == 0 else None,
            "query": "Legacy private question",
            "confidence_level": "high",
            "user_oid": "Legacy owner identity",
        }
        for index in range(1003)
    ]
    container = mock.Mock(query_items=mock.Mock(return_value=analytics_rows(reversed(records))))
    app = admin_app_factory()
    app.config[CONFIG_COSMOS_ANALYTICS_CONTAINER] = container

    response = await app.test_client().get("/api/admin/analytics")

    assert response.status_code == 200
    result = await response.get_json()
    assert result["total_queries"] == 1003
    assert result["avg_response_time_ms"] == 126
    assert result["total_sessions"] == 2
    assert result["example_question_rate"] == 25
    assert result["error_count"] == 11
    assert len(result["queries_by_day"]) == 31
    assert sum(day["count"] for day in result["queries_by_day"]) == 1003
    assert [day for day in result["queries_by_day"] if day["count"]] == [
        {"date": datetime.fromtimestamp(2000000000 - 86400, UTC).strftime("%Y-%m-%d"), "count": 501},
        {"date": datetime.fromtimestamp(2000000000, UTC).strftime("%Y-%m-%d"), "count": 502},
    ]
    assert len(result["recent_activity"]) == 20
    assert [activity["timestamp"] for activity in result["recent_activity"]] == [
        2000000000000 - index * 2 for index in range(20)
    ]
    assert result["recent_activity"][0] == {
        "timestamp": 2000000000000,
        "response_time_ms": 126,
        "is_example_question": True,
        "failed": True,
    }
    assert set(result) == {
        "total_queries",
        "total_sessions",
        "avg_response_time_ms",
        "queries_by_day",
        "recent_activity",
        "example_question_rate",
        "error_count",
    }
    assert "Legacy" not in str(result)
    assert "confidence" not in str(result)
    query = container.query_items.call_args.kwargs
    container.query_items.assert_called_once()
    assert query["parameters"] == [
        {"name": "@since", "value": (2000000000 - 30 * 86400) * 1000},
        {"name": "@until", "value": 2000000000000},
    ]
    assert "c.timestamp >= @since AND c.timestamp <= @until" in query["query"]
    assert all(
        token not in query["query"]
        for token in ("COUNT(", "SUM(", "AVG(", "GROUP BY", "TOP ", "SELECT *", "c.query", "c.confidence", "c.user_oid")
    )
    assert query["max_item_count"] == 100


@pytest.mark.asyncio
async def test_analytics_empty_store_is_a_real_zero_usage_result(admin_app_factory):
    container = mock.Mock(query_items=mock.Mock(return_value=analytics_rows([])))
    app = admin_app_factory()
    app.config[CONFIG_COSMOS_ANALYTICS_CONTAINER] = container
    response = await app.test_client().get("/api/admin/analytics")
    assert response.status_code == 200
    result = await response.get_json()
    assert result["total_queries"] == result["total_sessions"] == result["error_count"] == 0
    assert result["avg_response_time_ms"] == result["example_question_rate"] == 0
    assert len(result["queries_by_day"]) == 31
    assert all(day["count"] == 0 for day in result["queries_by_day"])
    assert result["recent_activity"] == []
    container.query_items.assert_called_once()


@pytest.mark.asyncio
async def test_analytics_missing_configuration_is_not_a_success_response(admin_app_factory):
    app = admin_app_factory()
    response = await app.test_client().get("/api/admin/analytics")
    assert response.status_code == 503
    assert (await response.get_json()) == {"error": "Analytics not configured"}


@pytest.mark.asyncio
async def test_analytics_query_failure_is_explicit(admin_app_factory):
    container = mock.Mock(
        query_items=mock.Mock(side_effect=CosmosHttpResponseError(status_code=503, message="Analytics unavailable"))
    )
    app = admin_app_factory()
    app.config[CONFIG_COSMOS_ANALYTICS_CONTAINER] = container
    response = await app.test_client().get("/api/admin/analytics")
    assert response.status_code == 500
    assert "error" in await response.get_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "record",
    [
        {},
        {"timestamp": True, "response_time_ms": 1},
        {"timestamp": "invalid", "response_time_ms": 1},
        {"timestamp": 2000000000001, "response_time_ms": 1},
        {"timestamp": 0, "response_time_ms": 1},
        {"timestamp": 2000000000000, "response_time_ms": None},
        {"timestamp": 2000000000000, "response_time_ms": True},
        {"timestamp": 2000000000000, "response_time_ms": -1},
        {"timestamp": 2000000000000, "response_time_ms": float("nan")},
        {"timestamp": 2000000000000, "response_time_ms": float("inf")},
    ],
)
async def test_analytics_invalid_usage_record_is_an_explicit_error(admin_app_factory, monkeypatch, record):
    monkeypatch.setattr(settings_cosmosdb.time, "time", lambda: 2000000000)
    container = mock.Mock(query_items=mock.Mock(return_value=analytics_rows([record])))
    app = admin_app_factory()
    app.config[CONFIG_COSMOS_ANALYTICS_CONTAINER] = container
    response = await app.test_client().get("/api/admin/analytics")
    assert response.status_code == 500
    assert "error" in await response.get_json()


@pytest.mark.asyncio
async def test_analytics_failure_after_a_page_does_not_return_partial_totals(admin_app_factory, monkeypatch):
    monkeypatch.setattr(settings_cosmosdb.time, "time", lambda: 2000000000)

    async def failing_pages():
        yield {"timestamp": 2000000000000, "response_time_ms": 5}
        raise CosmosHttpResponseError(status_code=503, message="Second page unavailable")

    container = mock.Mock(query_items=mock.Mock(return_value=failing_pages()))
    app = admin_app_factory()
    app.config[CONFIG_COSMOS_ANALYTICS_CONTAINER] = container
    response = await app.test_client().get("/api/admin/analytics")
    assert response.status_code == 500
    assert "error" in await response.get_json()


@pytest.mark.asyncio
async def test_analytics_counts_sessions_without_turn_or_identity_assumptions():
    records = [
        {"timestamp": 0, "response_time_ms": 10, "session_id": "one"},
        {"timestamp": 1, "response_time_ms": 20, "session_id": "one"},
        {"timestamp": 2, "response_time_ms": 30, "session_id": ""},
        {"timestamp": 3, "response_time_ms": 40, "session_id": None},
        {"timestamp": 4, "response_time_ms": 50, "user_oid": "anonymous"},
        {"timestamp": 5, "response_time_ms": 60, "session_id": "two"},
    ]
    container = mock.Mock(query_items=mock.Mock(return_value=analytics_rows(records)))
    result = await settings_cosmosdb.compute_analytics_summary(container, 0, 5)
    assert result["total_queries"] == 6
    assert result["total_sessions"] == 2
    assert result["avg_response_time_ms"] == 35
    assert result["queries_by_day"] == [{"date": "1970-01-01", "count": 6}]


@pytest.mark.asyncio
async def test_analytics_rejects_an_invalid_reporting_window():
    container = mock.Mock()
    with pytest.raises(ValueError, match="window"):
        await settings_cosmosdb.compute_analytics_summary(container, 2, 1)
    container.query_items.assert_not_called()


class UsageGatewayResponse(AsyncHttpResponse):
    def __init__(self, request, payload, status=200, continuation=None):
        super().__init__(request, None)
        self.status_code = status
        self.reason = "OK" if status == 200 else "Bad Request"
        self.headers = {"content-type": "application/json"}
        if continuation is not None:
            self.headers["x-ms-continuation"] = continuation
        self.content_type = "application/json"
        self.payload = json.dumps(payload).encode()

    def body(self):
        return self.payload


class UsageGatewayTransport(AsyncHttpTransport):
    def __init__(self):
        self.queries = []

    async def open(self):
        pass

    async def close(self):
        pass

    async def __aexit__(self, *args):
        await self.close()

    async def send(self, request, **kwargs):
        if request.method == "GET" and request.url.rstrip("/") == "https://usage.documents.azure.com":
            return UsageGatewayResponse(
                request,
                {
                    "id": "usage",
                    "readableLocations": [],
                    "writableLocations": [],
                    "userConsistencyPolicy": {"defaultConsistencyLevel": "Session"},
                },
            )
        if request.method == "GET" and request.url.rstrip("/").endswith("/colls/analytics"):
            return UsageGatewayResponse(
                request,
                {
                    "id": "analytics",
                    "_rid": "usage",
                    "partitionKey": {"paths": ["/user_oid"], "kind": "Hash", "version": 2},
                },
            )
        assert request.method == "POST" and request.url.rstrip("/").endswith("/docs"), request.url
        query = json.loads(request.body)
        self.queries.append(query["query"])
        if ("COUNT(" in query["query"] and "AVG(" in query["query"]) or "GROUP BY" in query["query"]:
            return UsageGatewayResponse(
                request,
                {
                    "code": "BadRequest",
                    "message": "Query contained NonValueAggregate and MultipleAggregates, which the calling client does not support.",
                },
                status=400,
            )
        continuation = request.headers.get("x-ms-continuation")
        records = (
            [{"timestamp": 2, "response_time_ms": 20, "session_id": "two"}]
            if continuation
            else [
                {"timestamp": 0, "response_time_ms": 10, "session_id": "one"},
                {"timestamp": 1, "response_time_ms": 15, "session_id": "one"},
            ]
        )
        return UsageGatewayResponse(
            request, {"Documents": records, "_count": len(records)}, continuation=None if continuation else "next-page"
        )


@pytest.mark.asyncio
async def test_analytics_supported_projection_paginates_through_the_real_cosmos_sdk():
    transport = UsageGatewayTransport()
    async with CosmosClient(
        "https://usage.documents.azure.com",
        credential=MockAzureCredential(),
        transport=transport,
        enable_endpoint_discovery=False,
    ) as client:
        container = client.get_database_client("usage").get_container_client("analytics")
        with pytest.raises(CosmosHttpResponseError, match="NonValueAggregate"):
            async for item in container.query_items(
                query="SELECT COUNT(1) AS total, AVG(c.response_time_ms) AS latency FROM c"
            ):
                pytest.fail(f"The unsupported aggregate unexpectedly succeeded: {item}")
        result = await settings_cosmosdb.compute_analytics_summary(container, 0, 2)
    assert result["total_queries"] == 3
    assert result["total_sessions"] == 2
    assert result["avg_response_time_ms"] == 15
    assert [item["timestamp"] for item in result["recent_activity"]] == [2, 1, 0]
    assert len(transport.queries) == 3
    assert transport.queries[1] == transport.queries[2]
