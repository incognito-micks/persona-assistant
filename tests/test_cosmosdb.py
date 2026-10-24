import copy
import json
from urllib.parse import quote

import pytest
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError

from .mocks import MockAsyncPageIterator

AUTH_HEADERS = {"Authorization": "Bearer MockToken"}

for_sessions_query = [
    [
        {
            "id": "123",
            "session_id": "123",
            "entra_oid": "OID_X",
            "email": "test@example.com",
            "display_name": "Test User",
            "title": "This is a test message",
            "timestamp": 123456789,
            "type": "session",
        }
    ]
]

for_deletion_query = [
    [
        {
            "id": "123",
            "session_id": "123",
            "entra_oid": "OID_X",
            "email": "test@example.com",
            "display_name": "Test User",
            "title": "This is a test message",
            "timestamp": 123456789,
            "type": "session",
        },
        {
            "id": "123-0",
            "version": "cosmosdb-v2",
            "session_id": "123",
            "entra_oid": "OID_X",
            "email": "test@example.com",
            "display_name": "Test User",
            "type": "message_pair",
            "question": "What does a Product Manager do?",
            "response": {
                "delta": {"role": "assistant"},
                "session_state": "143c0240-b2ee-4090-8e90-2a1c58124894",
                "message": {
                    "content": "A Product Manager is responsible for leading the product management team and providing guidance on product strategy, design, development, and launch. They collaborate with internal teams and external partners to ensure successful product execution. They also develop and implement product life-cycle management processes, monitor industry trends, develop product marketing plans, research customer needs, collaborate with internal teams, develop pricing strategies, oversee product portfolio, analyze product performance, and identify areas for improvement [role_library.pdf#page=29][role_library.pdf#page=12][role_library.pdf#page=23].",
                    "role": "assistant",
                },
            },
            "order": 0,
            "timestamp": None,
        },
    ]
]

for_message_pairs_query = [
    [
        {
            "id": "123-0",
            "version": "cosmosdb-v2",
            "session_id": "123",
            "entra_oid": "OID_X",
            "email": "test@example.com",
            "display_name": "Test User",
            "type": "message_pair",
            "question": "What does a Product Manager do?",
            "response": {
                "delta": {"role": "assistant"},
                "session_state": "143c0240-b2ee-4090-8e90-2a1c58124894",
                "message": {
                    "content": "A Product Manager is responsible for leading the product management team and providing guidance on product strategy, design, development, and launch. They collaborate with internal teams and external partners to ensure successful product execution. They also develop and implement product life-cycle management processes, monitor industry trends, develop product marketing plans, research customer needs, collaborate with internal teams, develop pricing strategies, oversee product portfolio, analyze product performance, and identify areas for improvement [role_library.pdf#page=29][role_library.pdf#page=12][role_library.pdf#page=23].",
                    "role": "assistant",
                },
            },
            "order": 0,
            "timestamp": None,
        },
    ]
]


class MockCosmosDBResultsIterator:
    def __init__(self, data=None):
        self.data = copy.deepcopy(data or [])

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.data:
            raise StopAsyncIteration
        return MockAsyncPageIterator(self.data.pop(0))

    async def get_count(self):
        return len(self.data)

    def by_page(self, continuation_token=None):
        if continuation_token:
            self.continuation_token = continuation_token + "next"
        else:
            self.continuation_token = "next"
        return self


class MockHistoryStore:
    def __init__(self):
        self.items = {}
        self.batch_calls = []
        self.point_calls = []
        self.query_calls = []
        self.fail_on_batch = None
        self.fail_after_commit = None

    def seed(self, items):
        for item in items:
            self.items[(item["entra_oid"], item["session_id"], item["id"])] = copy.deepcopy(item)

    def query_items(self, **kwargs):
        self.query_calls.append(copy.deepcopy(kwargs))
        partition = tuple(kwargs["partition_key"])
        parameters = {entry["name"]: entry["value"] for entry in kwargs.get("parameters", [])}
        items = [
            copy.deepcopy(item)
            for key, item in self.items.items()
            if key[: len(partition)] == partition
            and ("@type" not in parameters or item.get("type") == parameters["@type"])
        ]
        if "ORDER BY c.timestamp DESC" in kwargs["query"]:
            items.sort(key=lambda item: item["timestamp"], reverse=True)
        else:
            items.reverse()
        return MockCosmosDBResultsIterator([items[index : index + 7] for index in range(0, len(items), 7)])

    async def execute_item_batch(self, **kwargs):
        operations = copy.deepcopy(kwargs["batch_operations"])
        self.batch_calls.append(operations)
        assert 0 < len(operations) <= 100
        wire_operations = [
            {
                "operationType": operation[0].title(),
                "resourceBody" if operation[0] == "upsert" else "id": operation[1][0],
            }
            for operation in operations
        ]
        assert len(json.dumps(wire_operations, separators=(",", ":")).encode("utf-8")) <= 2 * 1024 * 1024
        if len(self.batch_calls) == self.fail_on_batch:
            raise CosmosBatchOperationError(status_code=503, headers={}, message="Mock batch unavailable")
        updated = copy.deepcopy(self.items)
        partition = tuple(kwargs["partition_key"])
        for operation in operations:
            if operation[0] == "upsert":
                item = operation[1][0]
                assert (item["entra_oid"], item["session_id"]) == partition
                updated[(*partition, item["id"])] = item
            else:
                del updated[(*partition, operation[1][0])]
        self.items = updated
        if len(self.batch_calls) == self.fail_after_commit:
            raise CosmosHttpResponseError(status_code=503, message="Mock response lost after commit")
        return [{"statusCode": 200} for operation in operations]

    async def upsert_item(self, body, **kwargs):
        self.point_calls.append(copy.deepcopy(body))
        assert len(json.dumps(body, separators=(",", ":")).encode("utf-8")) <= 2 * 1024 * 1024
        self.seed([body])
        return copy.deepcopy(body)


@pytest.fixture(autouse=True)
def cosmos_store(monkeypatch):
    store = MockHistoryStore()

    def query_items(container_proxy, **kwargs):
        return store.query_items(**kwargs)

    async def execute_item_batch(container_proxy, **kwargs):
        return await store.execute_item_batch(**kwargs)

    async def upsert_item(container_proxy, body, **kwargs):
        return await store.upsert_item(body, **kwargs)

    async def unexpected_read(container_proxy, *args, **kwargs):
        raise AssertionError("Unexpected Cosmos read; all service calls must be mocked")

    monkeypatch.setattr(ContainerProxy, "query_items", query_items)
    monkeypatch.setattr(ContainerProxy, "execute_item_batch", execute_item_batch)
    monkeypatch.setattr(ContainerProxy, "upsert_item", upsert_item)
    monkeypatch.setattr(ContainerProxy, "read_item", unexpected_read)
    monkeypatch.setattr(ContainerProxy, "delete_item", unexpected_read)
    return store


@pytest.mark.asyncio
async def test_chathistory_newitem(auth_public_documents_client, monkeypatch):

    async def mock_execute_item_batch(container_proxy, **kwargs):
        partition_key = kwargs["partition_key"]
        assert partition_key == ["OID_X", "123"]
        operations = kwargs["batch_operations"]
        assert len(operations) == 2
        assert operations[0][0] == "upsert"
        assert operations[1][0] == "upsert"
        session = operations[-1][1][0]
        assert session["id"] == "123"
        assert session["session_id"] == "123"
        assert session["entra_oid"] == "OID_X"
        assert session["email"] == "test@example.com"
        assert session["display_name"] == "Test User"
        assert session["title"] == "This is a test message"
        assert session["message_count"] == 1
        message = operations[0][1][0]
        assert message["id"] == "123-0"
        assert message["session_id"] == "123"
        assert message["entra_oid"] == "OID_X"
        assert message["email"] == "test@example.com"
        assert message["display_name"] == "Test User"
        assert message["question"] == "This is a test message"
        assert message["response"] == "This is a test answer"
        assert message["order"] == 0

    monkeypatch.setattr(ContainerProxy, "execute_item_batch", mock_execute_item_batch)

    response = await auth_public_documents_client.post(
        "/chat_history",
        headers={"Authorization": "Bearer MockToken"},
        json={
            "id": "123",
            "answers": [["This is a test message", "This is a test answer"]],
        },
    )
    assert response.status_code == 201


@pytest.mark.asyncio
async def test_chathistory_newitem_error_disabled(client, monkeypatch):

    response = await client.post(
        "/chat_history",
        json={
            "id": "123",
            "answers": [["This is a test message"]],
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chathistory_newitem_error_container(auth_public_documents_client, monkeypatch):
    auth_public_documents_client.app.config["cosmos_history_container"] = None
    response = await auth_public_documents_client.post(
        "/chat_history",
        headers={"Authorization": "Bearer MockToken"},
        json={
            "id": "123",
            "answers": [["This is a test message"]],
        },
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chathistory_newitem_error_entra(auth_public_documents_client, monkeypatch):
    response = await auth_public_documents_client.post(
        "/chat_history",
        json={
            "id": "123",
            "answers": [["This is a test message"]],
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chathistory_newitem_error_runtime(auth_public_documents_client, monkeypatch):

    async def mock_execute_item_batch(container_proxy, **kwargs):
        raise Exception("Test Exception")

    monkeypatch.setattr(ContainerProxy, "execute_item_batch", mock_execute_item_batch)

    response = await auth_public_documents_client.post(
        "/chat_history",
        headers={"Authorization": "Bearer MockToken"},
        json={
            "id": "123",
            "answers": [["This is a test message", "This is a test answer"]],
        },
    )
    assert response.status_code == 500
    assert (await response.get_json()) == {
        "error": "The app encountered an error processing your request.\nIf you are an administrator of the app, check the application logs for a full traceback.\nError type: <class 'Exception'>\n"
    }


@pytest.mark.asyncio
async def test_chathistory_query(auth_public_documents_client, monkeypatch, snapshot):

    def mock_query_items(container_proxy, query, **kwargs):
        return MockCosmosDBResultsIterator(for_sessions_query)

    monkeypatch.setattr(ContainerProxy, "query_items", mock_query_items)

    response = await auth_public_documents_client.get(
        "/chat_history/sessions?count=20", headers={"Authorization": "Bearer MockToken"}
    )
    assert response.status_code == 200
    result = await response.get_json()
    snapshot.assert_match(json.dumps(result, indent=4), "result.json")


@pytest.mark.asyncio
async def test_chathistory_query_continuation(auth_public_documents_client, monkeypatch, snapshot):

    def mock_query_items(container_proxy, query, **kwargs):
        return MockCosmosDBResultsIterator()

    monkeypatch.setattr(ContainerProxy, "query_items", mock_query_items)

    response = await auth_public_documents_client.get(
        "/chat_history/sessions?count=20&continuation_token=123", headers={"Authorization": "Bearer MockToken"}
    )
    assert response.status_code == 200
    result = await response.get_json()
    snapshot.assert_match(json.dumps(result, indent=4), "result.json")


@pytest.mark.asyncio
async def test_chathistory_query_error_disabled(client, monkeypatch):
    response = await client.get("/chat_history/sessions?count=20")
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chathistory_query_error_container(auth_public_documents_client, monkeypatch):
    auth_public_documents_client.app.config["cosmos_history_container"] = None
    response = await auth_public_documents_client.get(
        "/chat_history/sessions", headers={"Authorization": "Bearer MockToken"}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chathistory_query_error_entra(auth_public_documents_client, monkeypatch):
    response = await auth_public_documents_client.get("/chat_history/sessions")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chathistory_query_error_runtime(auth_public_documents_client, monkeypatch):

    def mock_query_items(container_proxy, query, **kwargs):
        raise Exception("Test Exception")

    monkeypatch.setattr(ContainerProxy, "query_items", mock_query_items)

    response = await auth_public_documents_client.get(
        "/chat_history/sessions?count=20", headers={"Authorization": "Bearer MockToken"}
    )
    assert response.status_code == 500
    assert (await response.get_json()) == {
        "error": "The app encountered an error processing your request.\nIf you are an administrator of the app, check the application logs for a full traceback.\nError type: <class 'Exception'>\n"
    }


# Tests for getting an individual chat history item
@pytest.mark.asyncio
async def test_chathistory_getitem(auth_public_documents_client, monkeypatch, snapshot):

    def mock_query_items(container_proxy, query, **kwargs):
        return MockCosmosDBResultsIterator(for_message_pairs_query)

    monkeypatch.setattr(ContainerProxy, "query_items", mock_query_items)

    response = await auth_public_documents_client.get(
        "/chat_history/sessions/123",
        headers={"Authorization": "Bearer MockToken"},
    )
    assert response.status_code == 200
    result = await response.get_json()
    snapshot.assert_match(json.dumps(result, indent=4), "result.json")


# Error handling tests for getting an individual chat history item
@pytest.mark.asyncio
async def test_chathistory_getitem_error_disabled(client, monkeypatch):

    response = await client.get(
        "/chat_history/sessions/123",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chathistory_getitem_error_container(auth_public_documents_client, monkeypatch):
    auth_public_documents_client.app.config["cosmos_history_container"] = None
    response = await auth_public_documents_client.get(
        "/chat_history/sessions/123",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chathistory_getitem_error_entra(auth_public_documents_client, monkeypatch):
    response = await auth_public_documents_client.get(
        "/chat_history/sessions/123",
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chathistory_getitem_error_runtime(auth_public_documents_client, monkeypatch):

    def mock_query_items(container_proxy, **kwargs):
        raise Exception("Test Exception")

    monkeypatch.setattr(ContainerProxy, "query_items", mock_query_items)

    response = await auth_public_documents_client.get(
        "/chat_history/sessions/123",
        headers={"Authorization": "Bearer MockToken"},
    )
    assert response.status_code == 500


# Tests for deleting an individual chat history item
@pytest.mark.asyncio
async def test_chathistory_deleteitem(auth_public_documents_client, monkeypatch):

    def mock_query_items(container_proxy, query, **kwargs):
        return MockCosmosDBResultsIterator(for_deletion_query)

    monkeypatch.setattr(ContainerProxy, "query_items", mock_query_items)

    # mock the batch delete operation
    async def mock_execute_item_batch(container_proxy, **kwargs):
        partition_key = kwargs["partition_key"]
        assert partition_key == ["OID_X", "123"]
        operations = kwargs["batch_operations"]
        assert len(operations) == 2
        assert operations[0][0] == "delete"
        assert operations[1][0] == "delete"
        assert operations[0][1][0] == "123-0"
        assert operations[1][1][0] == "123"

    monkeypatch.setattr(ContainerProxy, "execute_item_batch", mock_execute_item_batch)

    response = await auth_public_documents_client.delete(
        "/chat_history/sessions/123",
        headers={"Authorization": "Bearer MockToken"},
    )
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_chathistory_deleteitem_error_disabled(client, monkeypatch):

    response = await client.delete(
        "/chat_history/sessions/123",
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chathistory_deleteitem_error_container(auth_public_documents_client, monkeypatch):
    auth_public_documents_client.app.config["cosmos_history_container"] = None
    response = await auth_public_documents_client.delete(
        "/chat_history/sessions/123",
        headers={"Authorization": "Bearer MockToken"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_chathistory_deleteitem_error_entra(auth_public_documents_client, monkeypatch):
    response = await auth_public_documents_client.delete(
        "/chat_history/sessions/123",
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chathistory_deleteitem_error_runtime(auth_public_documents_client, monkeypatch):

    def mock_query_items(container_proxy, **kwargs):
        raise Exception("Test Exception")

    monkeypatch.setattr(ContainerProxy, "query_items", mock_query_items)

    response = await auth_public_documents_client.delete(
        "/chat_history/sessions/123",
        headers={"Authorization": "Bearer MockToken"},
    )
    assert response.status_code == 500


def conversation_answers(count):
    return [
        [f"Question {index}", {"message": {"role": "assistant", "content": f"Answer {index}"}}]
        for index in range(count)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [99, 100, 101, 250])
async def test_chathistory_bounded_incremental_save(auth_public_documents_client, cosmos_store, count):
    answers = conversation_answers(count)
    response = await auth_public_documents_client.post(
        "/chat_history", headers={"Authorization": "Bearer MockToken"}, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 201
    assert len(cosmos_store.items) == count + 1
    full_batches, remainder = divmod(count, 99)
    assert [len(batch) for batch in cosmos_store.batch_calls] == [100] * full_batches + (
        [remainder + 1] if remainder else []
    )
    assert all(
        item["order"] == int(item["id"].rsplit("-", 1)[1])
        for item in cosmos_store.items.values()
        if item["type"] == "message_pair"
    )
    session = cosmos_store.items[("OID_X", "123", "123")]
    assert session["message_count"] == count
    batches_before_retry = len(cosmos_store.batch_calls)
    response = await auth_public_documents_client.post(
        "/chat_history", headers={"Authorization": "Bearer MockToken"}, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 201
    assert len(cosmos_store.batch_calls) == batches_before_retry
    assert cosmos_store.items[("OID_X", "123", "123")] == session
    response = await auth_public_documents_client.post(
        "/chat_history",
        headers={"Authorization": "Bearer MockToken"},
        json={"id": "123", "answers": answers + conversation_answers(1)},
    )
    assert response.status_code == 201
    assert len(cosmos_store.batch_calls[-1]) == 2
    assert cosmos_store.items[("OID_X", "123", "123")]["message_count"] == count + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_order", [True, False, "mixed"])
async def test_chathistory_getitem_numeric_order_across_pages(auth_public_documents_client, monkeypatch, legacy_order):
    items = []
    answers = conversation_answers(15)
    for index, (question, answer) in enumerate(answers):
        item = copy.deepcopy(for_message_pairs_query[0][0])
        item.update(id=f"123-{index}", question=question, response=answer)
        item.pop("order")
        if legacy_order is True or (legacy_order == "mixed" and index % 2):
            item["order"] = index
        items.append(item)

    def mock_query_items(container_proxy, **kwargs):
        assert kwargs["partition_key"] == ["OID_X", "123"]
        return MockCosmosDBResultsIterator(
            [[items[12], items[2]], items[3:12], [items[14], items[0], items[13], items[1]]]
        )

    monkeypatch.setattr(ContainerProxy, "query_items", mock_query_items)
    response = await auth_public_documents_client.get(
        "/chat_history/sessions/123", headers={"Authorization": "Bearer MockToken"}
    )
    assert response.status_code == 200
    assert (await response.get_json())["answers"] == answers


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
async def test_chathistory_retry_partial_save(auth_public_documents_client, cosmos_store, lost_response):
    answers = conversation_answers(250)
    if lost_response:
        cosmos_store.fail_after_commit = 2
    else:
        cosmos_store.fail_on_batch = 2
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 500
    saved_ids = {item["id"] for item in cosmos_store.items.values() if item["type"] == "message_pair"}
    assert len(saved_ids) == (198 if lost_response else 99)
    assert cosmos_store.items[("OID_X", "123", "123")]["message_count"] == len(saved_ids)
    calls_before_retry = len(cosmos_store.batch_calls)
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 201
    retried_ids = {
        operation[1][0]["id"]
        for batch in cosmos_store.batch_calls[calls_before_retry:]
        for operation in batch
        if operation[1][0]["type"] == "message_pair"
    }
    assert not (saved_ids & retried_ids)
    assert len(cosmos_store.items) == 251
    assert cosmos_store.items[("OID_X", "123", "123")]["message_count"] == 250
    response = await auth_public_documents_client.get("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert (await response.get_json())["answers"] == answers


@pytest.mark.asyncio
async def test_chathistory_updates_only_changed_turns_and_metadata(
    auth_public_documents_client, cosmos_store, monkeypatch
):
    answers = conversation_answers(15)
    answers[0][0] = "A" * 80
    monkeypatch.setattr("chat_history.cosmosdb.time.time", lambda: 1000)
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 201
    session = cosmos_store.items[("OID_X", "123", "123")]
    assert session["title"] == "A" * 50 + "..."
    assert session["timestamp"] == 1000000
    answers[0][0] = "Updated title"
    answers[12][1] = {"message": {"content": "Corrected answer", "role": "assistant"}}
    monkeypatch.setattr("chat_history.cosmosdb.time.time", lambda: 2000)
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 201
    assert [operation[1][0]["id"] for operation in cosmos_store.batch_calls[-1]] == ["123-0", "123-12", "123"]
    session = cosmos_store.items[("OID_X", "123", "123")]
    assert session["message_count"] == 15
    assert session["title"] == "Updated title"
    assert session["timestamp"] == 2000000
    assert cosmos_store.items[("OID_X", "123", "123-1")]["timestamp"] == 1000000
    response = await auth_public_documents_client.get("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert (await response.get_json())["answers"] == answers


@pytest.mark.asyncio
async def test_chathistory_legacy_retry_does_not_rewrite_messages(auth_public_documents_client, cosmos_store):
    answers = conversation_answers(15)
    await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    for item in cosmos_store.items.values():
        if item["type"] == "message_pair":
            item.pop("order")
            item["timestamp"] = None
    cosmos_store.batch_calls.clear()
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 201
    assert not cosmos_store.batch_calls
    assert not cosmos_store.point_calls
    response = await auth_public_documents_client.get("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert (await response.get_json())["answers"] == answers


@pytest.mark.asyncio
async def test_chathistory_does_not_silently_truncate(auth_public_documents_client, cosmos_store):
    answers = conversation_answers(15)
    await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    before = copy.deepcopy(cosmos_store.items)
    calls_before = len(cosmos_store.batch_calls)
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers[:10]}
    )
    assert response.status_code == 409
    assert "more turns" in (await response.get_json())["error"]
    assert cosmos_store.items == before
    assert len(cosmos_store.batch_calls) == calls_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"id": "123"},
        {"id": None, "answers": [["Q", "A"]]},
        {"id": 123, "answers": [["Q", "A"]]},
        {"id": "", "answers": [["Q", "A"]]},
        {"id": "  ", "answers": [["Q", "A"]]},
        {"id": "bad/id", "answers": [["Q", "A"]]},
        {"id": "bad\\id", "answers": [["Q", "A"]]},
        {"id": "bad?id", "answers": [["Q", "A"]]},
        {"id": "bad#id", "answers": [["Q", "A"]]},
        {"id": "bad\x00id", "answers": [["Q", "A"]]},
        {"id": "\ud800", "answers": [["Q", "A"]]},
        {"id": "a" * 1022, "answers": [["Q", "A"]]},
        {"id": "é" * 512, "answers": [["Q", "A"]]},
        {"id": "123", "answers": None},
        {"id": "123", "answers": []},
        {"id": "123", "answers": {}},
        {"id": "123", "answers": ["Q"]},
        {"id": "123", "answers": [["Q"]]},
        {"id": "123", "answers": [["Q", "A", "extra"]]},
        {"id": "123", "answers": [[None, "A"]]},
        {"id": "123", "answers": [["", "A"]]},
        {"id": "123", "answers": [["  ", "A"]]},
        {"id": "123", "answers": [["Q", None]]},
        {"id": "123", "answers": [["Q", 7]]},
        {"id": "123", "answers": [["Q", True]]},
        {"id": "123", "answers": [["Q", []]]},
        {"id": "123", "answers": [["Q", {"value": float("nan")}]]},
        {"id": "123", "answers": [["Q", "\ud800"]]},
    ],
)
async def test_chathistory_invalid_save_input(auth_public_documents_client, cosmos_store, payload):
    response = await auth_public_documents_client.post(
        "/chat_history", headers={**AUTH_HEADERS, "Content-Type": "application/json"}, data=json.dumps(payload)
    )
    assert response.status_code == 400
    assert (await response.get_json())["error"]
    assert not cosmos_store.query_calls
    assert not cosmos_store.batch_calls
    assert not cosmos_store.point_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["application/json", "text/plain"])
async def test_chathistory_invalid_json(auth_public_documents_client, cosmos_store, content_type):
    response = await auth_public_documents_client.post(
        "/chat_history", headers={**AUTH_HEADERS, "Content-Type": content_type}, data="not JSON"
    )
    assert 400 <= response.status_code < 500
    assert (await response.get_json())["error"]
    assert not cosmos_store.query_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("count", ["", "no", "0", "-1", "1.5", "2147483648", "9" * 1000])
async def test_chathistory_invalid_count(auth_public_documents_client, cosmos_store, count):
    response = await auth_public_documents_client.get(f"/chat_history/sessions?count={count}", headers=AUTH_HEADERS)
    assert response.status_code == 400
    assert "count" in (await response.get_json())["error"]
    assert not cosmos_store.query_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "delete"])
@pytest.mark.parametrize("session_id", ["bad?id", "bad#id", "bad\\id", "bad\x00id", "a" * 1024])
async def test_chathistory_invalid_route_id(auth_public_documents_client, cosmos_store, method, session_id):
    response = await getattr(auth_public_documents_client, method)(
        f"/chat_history/sessions/{quote(session_id, safe='')}", headers=AUTH_HEADERS
    )
    assert response.status_code == 400
    assert "id" in (await response.get_json())["error"]
    assert not cosmos_store.query_calls
    assert not cosmos_store.batch_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["x" * 710000, "😀" * 70000], ids=["ascii", "escaped-unicode"])
async def test_chathistory_batches_respect_encoded_payload_bytes(auth_public_documents_client, cosmos_store, text):
    answers = [[f"Question {index}", text] for index in range(3)]
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 201
    assert [len(batch) for batch in cosmos_store.batch_calls] == [3, 2]
    assert not cosmos_store.point_calls
    assert cosmos_store.items[("OID_X", "123", "123")]["message_count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized_index", [0, 99])
async def test_chathistory_rejects_oversized_item_before_any_writes(
    auth_public_documents_client, cosmos_store, oversized_index
):
    answers = conversation_answers(100)
    answers[oversized_index][1] = "x" * (2 * 1024 * 1024)
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 413
    assert "2 MB" in (await response.get_json())["error"]
    assert not cosmos_store.query_calls
    assert not cosmos_store.batch_calls
    assert not cosmos_store.point_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_failure", [False, True])
async def test_chathistory_maximum_size_item_and_metadata_retry(
    auth_public_documents_client, cosmos_store, monkeypatch, metadata_failure
):
    monkeypatch.setattr("chat_history.cosmosdb.time.time", lambda: 1000)
    await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": [["Q", ""]]}
    )
    item = copy.deepcopy(cosmos_store.items[("OID_X", "123", "123-0")])
    max_bytes = 2 * 1024 * 1024
    padding = max_bytes - len(json.dumps(item, separators=(",", ":")).encode("utf-8"))
    answers = [["Q", "x" * padding]]
    monkeypatch.setattr("chat_history.cosmosdb.time.time", lambda: 2000)
    if metadata_failure:
        cosmos_store.fail_on_batch = 2
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == (500 if metadata_failure else 201)
    assert len(cosmos_store.point_calls) == 1
    assert len(json.dumps(cosmos_store.point_calls[0], separators=(",", ":")).encode("utf-8")) == max_bytes
    assert cosmos_store.point_calls[0]["entra_oid"] == "OID_X"
    assert cosmos_store.point_calls[0]["session_id"] == "123"
    monkeypatch.setattr("chat_history.cosmosdb.time.time", lambda: 3000)
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 201
    assert len(cosmos_store.point_calls) == 1
    session = cosmos_store.items[("OID_X", "123", "123")]
    assert session["message_count"] == 1
    assert session["timestamp"] == (3000000 if metadata_failure else 2000000)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 99, 100, 101, 250])
async def test_chathistory_bounded_idempotent_delete(auth_public_documents_client, cosmos_store, count):
    if count:
        await auth_public_documents_client.post(
            "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": conversation_answers(count)}
        )
    cosmos_store.batch_calls.clear()
    response = await auth_public_documents_client.delete("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert response.status_code == 204
    assert not cosmos_store.items
    total = count + 1 if count else 0
    full_batches, remainder = divmod(total, 100)
    assert [len(batch) for batch in cosmos_store.batch_calls] == [100] * full_batches + (
        [remainder] if remainder else []
    )
    if count:
        assert cosmos_store.batch_calls[-1][-1] == ("delete", ("123",))
    calls_before_retry = len(cosmos_store.batch_calls)
    response = await auth_public_documents_client.delete("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert response.status_code == 204
    assert len(cosmos_store.batch_calls) == calls_before_retry


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_response", [False, True])
async def test_chathistory_retry_partial_delete(auth_public_documents_client, cosmos_store, lost_response):
    await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": conversation_answers(250)}
    )
    cosmos_store.batch_calls.clear()
    if lost_response:
        cosmos_store.fail_after_commit = 2
    else:
        cosmos_store.fail_on_batch = 2
    response = await auth_public_documents_client.delete("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert response.status_code == 500
    assert len(cosmos_store.items) == (51 if lost_response else 151)
    assert ("OID_X", "123", "123") in cosmos_store.items
    response = await auth_public_documents_client.delete("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert response.status_code == 204
    assert not cosmos_store.items


@pytest.mark.asyncio
async def test_chathistory_partition_isolation(auth_public_documents_client, cosmos_store):
    answers = conversation_answers(2)
    await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    foreign_items = []
    for item in list(cosmos_store.items.values()):
        foreign = copy.deepcopy(item)
        foreign["entra_oid"] = "OTHER_OID"
        foreign_items.append(foreign)
        other_session = copy.deepcopy(item)
        other_session["session_id"] = "456"
        other_session["id"] = item["id"].replace("123", "456")
        foreign_items.append(other_session)
    cosmos_store.seed(foreign_items)
    protected_items = copy.deepcopy(
        {key: item for key, item in cosmos_store.items.items() if key[:2] != ("OID_X", "123")}
    )
    answers.append(["Another question", "Another answer"])
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": answers}
    )
    assert response.status_code == 201
    response = await auth_public_documents_client.get("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert (await response.get_json())["answers"] == answers
    response = await auth_public_documents_client.delete("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert response.status_code == 204
    assert cosmos_store.items == protected_items
    assert all(query["partition_key"] == ["OID_X", "123"] for query in cosmos_store.query_calls)


@pytest.mark.asyncio
async def test_chathistory_numeric_order_is_authoritative(auth_public_documents_client, cosmos_store):
    items = []
    answers = conversation_answers(15)
    for index, (question, answer) in enumerate(answers):
        item = copy.deepcopy(for_message_pairs_query[0][0])
        item.update(id=f"123-{14 - index}", order=index, question=question, response=answer)
        items.append(item)
    cosmos_store.seed(items)
    response = await auth_public_documents_client.get("/chat_history/sessions/123", headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert (await response.get_json())["answers"] == answers


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["missing_order", "duplicate_order", "id_collision"])
async def test_chathistory_corrupt_saved_order_fails_without_writes(
    auth_public_documents_client, cosmos_store, corruption
):
    item = copy.deepcopy(for_message_pairs_query[0][0])
    if corruption == "missing_order":
        item.pop("order")
        item["id"] = "123-unknown"
        items = [item]
    elif corruption == "duplicate_order":
        duplicate = {**item, "id": "123-1"}
        items = [item, duplicate]
    else:
        item["order"] = 1
        items = [item]
    cosmos_store.seed(items)
    before = copy.deepcopy(cosmos_store.items)
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": conversation_answers(2)}
    )
    assert response.status_code == 500
    assert cosmos_store.items == before
    assert not cosmos_store.batch_calls
    assert not cosmos_store.point_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [CosmosHttpResponseError, CosmosBatchOperationError])
async def test_chathistory_service_size_error_is_413(auth_public_documents_client, monkeypatch, error_type):
    async def mock_execute_item_batch(container_proxy, **kwargs):
        raise error_type(status_code=413, headers={}, message="Mock service payload rejection")

    monkeypatch.setattr(ContainerProxy, "execute_item_batch", mock_execute_item_batch)
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": conversation_answers(1)}
    )
    assert response.status_code == 413
    assert "2 MB" in (await response.get_json())["error"]


@pytest.mark.asyncio
async def test_chathistory_maximum_legacy_record_retry(auth_public_documents_client, cosmos_store):
    item = copy.deepcopy(for_message_pairs_query[0][0])
    item.pop("order")
    item.pop("timestamp")
    item.update(question="Q", response="")
    padding = 2 * 1024 * 1024 - len(json.dumps(item, separators=(",", ":")).encode("utf-8"))
    item["response"] = "x" * padding
    session = {**for_sessions_query[0][0], "title": "Q"}
    cosmos_store.seed([session, item])
    response = await auth_public_documents_client.post(
        "/chat_history", headers=AUTH_HEADERS, json={"id": "123", "answers": [["Q", item["response"]]]}
    )
    assert response.status_code == 201
    assert not cosmos_store.point_calls
    assert [operation[1][0]["type"] for batch in cosmos_store.batch_calls for operation in batch] == ["session"]
    assert cosmos_store.items[("OID_X", "123", "123-0")] == item
