import json
import os
import time
from typing import Any

from azure.cosmos.aio import ContainerProxy, CosmosClient
from azure.cosmos.exceptions import CosmosBatchOperationError, CosmosHttpResponseError
from azure.identity.aio import AzureDeveloperCliCredential, ManagedIdentityCredential
from quart import Blueprint, current_app, jsonify, make_response, request
from werkzeug.exceptions import (
    BadRequest,
    Conflict,
    HTTPException,
    RequestEntityTooLarge,
)

from config import (
    CONFIG_CHAT_HISTORY_COSMOS_ENABLED,
    CONFIG_COSMOS_HISTORY_CLIENT,
    CONFIG_COSMOS_HISTORY_CONTAINER,
    CONFIG_COSMOS_HISTORY_VERSION,
    CONFIG_CREDENTIAL,
)
from decorators import authenticated
from error import error_response

chat_history_cosmosdb_bp = Blueprint("chat_history_cosmos", __name__, static_folder="static")

COSMOS_MAX_BATCH_OPERATIONS = 100
COSMOS_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
COSMOS_MAX_ID_BYTES = 1023


def validate_history_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BadRequest("id must be a non-empty string.")
    try:
        id_bytes = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise BadRequest("id must be valid UTF-8.") from error
    if (
        len(id_bytes) > COSMOS_MAX_ID_BYTES
        or any(char in value for char in "/\\?#")
        or any(ord(char) < 32 for char in value)
    ):
        raise BadRequest("id must be at most 1023 UTF-8 bytes and cannot contain /, \\, ?, #, or control characters.")
    return value


def history_json_size(value: Any) -> int:
    # Match the SDK's compact JSON encoding, including escaped Unicode and the batch envelope.
    return len(json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8"))


def validate_history_item(item: dict[str, Any]) -> None:
    try:
        json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")
        size = history_json_size(item)
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as error:
        raise BadRequest("answers must contain valid JSON values.") from error
    if size > COSMOS_MAX_PAYLOAD_BYTES:
        raise RequestEntityTooLarge("A conversation record exceeds the Cosmos DB 2 MB item limit.")


def history_operation_size(operation: tuple[str, tuple[Any, ...]]) -> int:
    field = "resourceBody" if operation[0] == "upsert" else "id"
    return history_json_size({"operationType": operation[0].title(), field: operation[1][0]})


def history_message_order(item: dict[str, Any], session_id: str) -> int:
    order = item.get("order")
    if type(order) is int and order >= 0:
        return order
    item_id = item.get("id", "")
    prefix = f"{session_id}-"
    if isinstance(item_id, str) and item_id.startswith(prefix):
        suffix = item_id[len(prefix) :]
        if suffix and suffix.isascii() and suffix.isdecimal():
            return int(suffix)
    raise ValueError("Stored conversation message has no valid numeric order.")


async def save_history_batches(
    container: ContainerProxy,
    partition_key: list[str],
    session_item: dict[str, Any],
    message_items: list[dict[str, Any]],
    saved_orders: set[int],
) -> None:
    batch: list[tuple[str, tuple[Any, ...]]] = []
    batch_bytes = 0
    for item in message_items:
        operation = ("upsert", (item,))
        operation_bytes = history_operation_size(operation)
        next_count = len(saved_orders) + (item["order"] not in saved_orders)
        session_operation = ("upsert", ({**session_item, "message_count": next_count},))
        session_bytes = history_operation_size(session_operation)
        if batch and (
            len(batch) + 2 > COSMOS_MAX_BATCH_OPERATIONS
            or 2 + batch_bytes + operation_bytes + 1 + session_bytes > COSMOS_MAX_PAYLOAD_BYTES
        ):
            await container.execute_item_batch(
                batch_operations=batch + [("upsert", ({**session_item, "message_count": len(saved_orders)},))],
                partition_key=partition_key,
            )
            batch = []
            batch_bytes = 0

        if 2 + operation_bytes + 1 + session_bytes > COSMOS_MAX_PAYLOAD_BYTES:
            # A valid near-limit item can fit a point write but not a batch with session metadata.
            await container.upsert_item(body=item)
            saved_orders.add(item["order"])
            await container.execute_item_batch(batch_operations=[session_operation], partition_key=partition_key)
        else:
            batch.append(operation)
            batch_bytes += operation_bytes + 1
            saved_orders.add(item["order"])

    if batch:
        await container.execute_item_batch(
            batch_operations=batch + [("upsert", ({**session_item, "message_count": len(saved_orders)},))],
            partition_key=partition_key,
        )


@chat_history_cosmosdb_bp.post("/chat_history")
@authenticated
async def post_chat_history(auth_claims: dict[str, Any]):
    if not current_app.config[CONFIG_CHAT_HISTORY_COSMOS_ENABLED]:
        return jsonify({"error": "Chat history not enabled"}), 400

    container: ContainerProxy = current_app.config[CONFIG_COSMOS_HISTORY_CONTAINER]
    if not container:
        return jsonify({"error": "Chat history not enabled"}), 400

    entra_oid = auth_claims.get("oid")
    if not entra_oid:
        return jsonify({"error": "User OID not found"}), 401

    email = auth_claims.get("email", "")
    display_name = auth_claims.get("name", "")

    try:
        request_json = await request.get_json()
        if not isinstance(request_json, dict):
            raise BadRequest("The request body must be a JSON object.")
        session_id = validate_history_id(request_json.get("id"))
        message_pairs = request_json.get("answers")
        if not isinstance(message_pairs, list) or not message_pairs:
            raise BadRequest("answers must be a non-empty list of [question, response] pairs.")
        timestamp = int(time.time() * 1000)
        metadata = {
            "version": current_app.config[CONFIG_COSMOS_HISTORY_VERSION],
            "session_id": session_id,
            "entra_oid": entra_oid,
            "email": email,
            "display_name": display_name,
        }
        message_items = []
        for ind, message_pair in enumerate(message_pairs):
            if (
                not isinstance(message_pair, list)
                or len(message_pair) != 2
                or not isinstance(message_pair[0], str)
                or not message_pair[0].strip()
                or not isinstance(message_pair[1], (str, dict))
            ):
                raise BadRequest(
                    f"answers[{ind}] must contain a non-empty question string and a string or object response."
                )
            item = {
                **metadata,
                "id": validate_history_id(f"{session_id}-{ind}"),
                "type": "message_pair",
                "question": message_pair[0],
                "response": message_pair[1],
                "order": ind,
                "timestamp": timestamp,
            }
            validate_history_item({"question": item["question"], "response": item["response"]})
            message_items.append(item)

        first_question = message_pairs[0][0]
        session_item = {
            **metadata,
            "id": session_id,
            "type": "session",
            "title": first_question[:50] + ("..." if len(first_question) > 50 else ""),
            "timestamp": timestamp,
            "message_count": len(message_items),
        }
        validate_history_item(session_item)
        partition_key = [entra_oid, session_id]
        res = container.query_items(
            query="SELECT * FROM c WHERE c.session_id = @session_id",
            parameters=[dict(name="@session_id", value=session_id)],
            partition_key=partition_key,
        )
        saved_messages: dict[int, dict[str, Any]] = {}
        saved_session: dict[str, Any] = {}
        async for page in res.by_page():
            async for item in page:
                if item.get("type") == "message_pair":
                    order = history_message_order(item, session_id)
                    if order in saved_messages:
                        raise ValueError("Stored conversation contains duplicate message orders.")
                    saved_messages[order] = item
                elif item.get("type") == "session" and item["id"] == session_id:
                    saved_session = item

        if any(order >= len(message_items) for order in saved_messages):
            raise Conflict("The saved conversation has more turns. Reload it before saving; use DELETE to remove it.")

        latest_message_timestamp = max(
            (item["timestamp"] for item in saved_messages.values() if type(item.get("timestamp")) is int),
            default=0,
        )
        saved_timestamp = saved_session.get("timestamp")
        session_item["timestamp"] = max(
            timestamp, latest_message_timestamp, saved_timestamp if type(saved_timestamp) is int else 0
        )
        pending_items = []
        for item in message_items:
            item["timestamp"] = session_item["timestamp"]
            saved_item = saved_messages.get(item["order"])
            if saved_item:
                item["id"] = saved_item["id"]
                if saved_item.get("question") == item["question"] and saved_item.get("response") == item["response"]:
                    continue
            validate_history_item(item)
            pending_items.append(item)

        if len({item["id"] for item in message_items}) != len(message_items):
            raise ValueError("Stored conversation message IDs conflict with numeric order.")
        metadata_changed = any(
            saved_session.get(key) != value for key, value in session_item.items() if key != "timestamp"
        ) or (type(saved_timestamp) is not int or saved_timestamp < latest_message_timestamp)
        if pending_items:
            await save_history_batches(container, partition_key, session_item, pending_items, set(saved_messages))
        elif metadata_changed:
            await container.execute_item_batch(
                batch_operations=[("upsert", (session_item,))], partition_key=partition_key
            )
        return jsonify({}), 201
    except HTTPException as error:
        return jsonify({"error": error.description}), error.code
    except (CosmosBatchOperationError, CosmosHttpResponseError) as error:
        if error.status_code == 413:
            return jsonify({"error": "A conversation record exceeds the Cosmos DB 2 MB request limit."}), 413
        return error_response(error, "/chat_history")
    except Exception as error:
        return error_response(error, "/chat_history")


@chat_history_cosmosdb_bp.get("/chat_history/sessions")
@authenticated
async def get_chat_history_sessions(auth_claims: dict[str, Any]):
    if not current_app.config[CONFIG_CHAT_HISTORY_COSMOS_ENABLED]:
        return jsonify({"error": "Chat history not enabled"}), 400

    container: ContainerProxy = current_app.config[CONFIG_COSMOS_HISTORY_CONTAINER]
    if not container:
        return jsonify({"error": "Chat history not enabled"}), 400

    entra_oid = auth_claims.get("oid")
    if not entra_oid:
        return jsonify({"error": "User OID not found"}), 401

    try:
        try:
            count = int(request.args.get("count", "10"))
        except ValueError as error:
            raise BadRequest("count must be a positive 32-bit integer.") from error
        if not 1 <= count <= 2147483647:
            raise BadRequest("count must be a positive 32-bit integer.")
        continuation_token = request.args.get("continuation_token")

        res = container.query_items(
            query="SELECT c.id, c.entra_oid, c.email, c.display_name, c.title, c.timestamp FROM c WHERE c.entra_oid = @entra_oid AND c.type = @type ORDER BY c.timestamp DESC",
            parameters=[dict(name="@entra_oid", value=entra_oid), dict(name="@type", value="session")],
            partition_key=[entra_oid],
            max_item_count=count,
        )

        pager = res.by_page(continuation_token)

        # Get the first page, and the continuation token
        sessions = []
        try:
            page = await pager.__anext__()
            continuation_token = pager.continuation_token  # type: ignore

            async for item in page:
                sessions.append(
                    {
                        "id": item.get("id"),
                        "entra_oid": item.get("entra_oid"),
                        "email": item.get("email", ""),
                        "display_name": item.get("display_name", ""),
                        "title": item.get("title", "untitled"),
                        "timestamp": item.get("timestamp"),
                    }
                )

        # If there are no more pages, StopAsyncIteration is raised
        except StopAsyncIteration:
            continuation_token = None

        return jsonify({"sessions": sessions, "continuation_token": continuation_token}), 200

    except HTTPException as error:
        return jsonify({"error": error.description}), error.code
    except Exception as error:
        return error_response(error, "/chat_history/sessions")


@chat_history_cosmosdb_bp.get("/chat_history/sessions/<session_id>")
@authenticated
async def get_chat_history_session(auth_claims: dict[str, Any], session_id: str):
    if not current_app.config[CONFIG_CHAT_HISTORY_COSMOS_ENABLED]:
        return jsonify({"error": "Chat history not enabled"}), 400

    container: ContainerProxy = current_app.config[CONFIG_COSMOS_HISTORY_CONTAINER]
    if not container:
        return jsonify({"error": "Chat history not enabled"}), 400

    entra_oid = auth_claims.get("oid")
    if not entra_oid:
        return jsonify({"error": "User OID not found"}), 401

    try:
        validate_history_id(session_id)
        res = container.query_items(
            query="SELECT * FROM c WHERE c.session_id = @session_id AND c.type = @type",
            parameters=[dict(name="@session_id", value=session_id), dict(name="@type", value="message_pair")],
            partition_key=[entra_oid, session_id],
        )

        items = []
        async for page in res.by_page():
            async for item in page:
                items.append(item)

        # ORDER BY c.order can omit legacy items without that field; sort all pages together instead.
        items.sort(key=lambda item: (history_message_order(item, session_id), item["id"]))
        message_pairs = [[item["question"], item["response"]] for item in items]

        return (
            jsonify(
                {
                    "id": session_id,
                    "entra_oid": entra_oid,
                    "email": auth_claims.get("email", ""),
                    "display_name": auth_claims.get("name", ""),
                    "answers": message_pairs,
                }
            ),
            200,
        )
    except HTTPException as error:
        return jsonify({"error": error.description}), error.code
    except Exception as error:
        return error_response(error, f"/chat_history/sessions/{session_id}")


@chat_history_cosmosdb_bp.delete("/chat_history/sessions/<session_id>")
@authenticated
async def delete_chat_history_session(auth_claims: dict[str, Any], session_id: str):
    if not current_app.config[CONFIG_CHAT_HISTORY_COSMOS_ENABLED]:
        return jsonify({"error": "Chat history not enabled"}), 400

    container: ContainerProxy = current_app.config[CONFIG_COSMOS_HISTORY_CONTAINER]
    if not container:
        return jsonify({"error": "Chat history not enabled"}), 400

    entra_oid = auth_claims.get("oid")
    if not entra_oid:
        return jsonify({"error": "User OID not found"}), 401

    try:
        validate_history_id(session_id)
        res = container.query_items(
            query="SELECT c.id FROM c WHERE c.session_id = @session_id",
            parameters=[dict(name="@session_id", value=session_id)],
            partition_key=[entra_oid, session_id],
        )

        ids_to_delete = []
        async for page in res.by_page():
            async for item in page:
                ids_to_delete.append(item["id"])

        # Retain the session until the final batch so a partial delete remains discoverable and retryable.
        ids_to_delete = sorted(set(ids_to_delete), key=lambda item_id: (item_id == session_id, item_id))
        batch_operations: list[tuple[str, tuple[Any, ...]]] = []
        batch_bytes = 2
        for item_id in ids_to_delete:
            operation = ("delete", (item_id,))
            operation_bytes = history_operation_size(operation)
            if batch_operations and (
                len(batch_operations) == COSMOS_MAX_BATCH_OPERATIONS
                or batch_bytes + 1 + operation_bytes > COSMOS_MAX_PAYLOAD_BYTES
            ):
                await container.execute_item_batch(
                    batch_operations=batch_operations, partition_key=[entra_oid, session_id]
                )
                batch_operations = []
                batch_bytes = 2
            batch_bytes += operation_bytes + bool(batch_operations)
            batch_operations.append(operation)
        if batch_operations:
            await container.execute_item_batch(batch_operations=batch_operations, partition_key=[entra_oid, session_id])
        return await make_response("", 204)
    except HTTPException as error:
        return jsonify({"error": error.description}), error.code
    except Exception as error:
        return error_response(error, f"/chat_history/sessions/{session_id}")


@chat_history_cosmosdb_bp.before_app_serving
async def setup_clients():
    USE_CHAT_HISTORY_COSMOS = os.getenv("USE_CHAT_HISTORY_COSMOS", "").lower() == "true"
    AZURE_COSMOSDB_ACCOUNT = os.getenv("AZURE_COSMOSDB_ACCOUNT")
    AZURE_CHAT_HISTORY_DATABASE = os.getenv("AZURE_CHAT_HISTORY_DATABASE")
    AZURE_CHAT_HISTORY_CONTAINER = os.getenv("AZURE_CHAT_HISTORY_CONTAINER")

    azure_credential: AzureDeveloperCliCredential | ManagedIdentityCredential = current_app.config[CONFIG_CREDENTIAL]

    if USE_CHAT_HISTORY_COSMOS:
        current_app.logger.info("USE_CHAT_HISTORY_COSMOS is true, setting up CosmosDB client")
        if not AZURE_COSMOSDB_ACCOUNT:
            raise ValueError("AZURE_COSMOSDB_ACCOUNT must be set when USE_CHAT_HISTORY_COSMOS is true")
        if not AZURE_CHAT_HISTORY_DATABASE:
            raise ValueError("AZURE_CHAT_HISTORY_DATABASE must be set when USE_CHAT_HISTORY_COSMOS is true")
        if not AZURE_CHAT_HISTORY_CONTAINER:
            raise ValueError("AZURE_CHAT_HISTORY_CONTAINER must be set when USE_CHAT_HISTORY_COSMOS is true")
        cosmos_client = CosmosClient(
            url=f"https://{AZURE_COSMOSDB_ACCOUNT}.documents.azure.com:443/", credential=azure_credential
        )
        cosmos_db = cosmos_client.get_database_client(AZURE_CHAT_HISTORY_DATABASE)
        cosmos_container = cosmos_db.get_container_client(AZURE_CHAT_HISTORY_CONTAINER)

        current_app.config[CONFIG_COSMOS_HISTORY_CLIENT] = cosmos_client
        current_app.config[CONFIG_COSMOS_HISTORY_CONTAINER] = cosmos_container
        current_app.config[CONFIG_COSMOS_HISTORY_VERSION] = os.environ["AZURE_CHAT_HISTORY_VERSION"]


@chat_history_cosmosdb_bp.after_app_serving
async def close_clients():
    if current_app.config.get(CONFIG_COSMOS_HISTORY_CLIENT):
        cosmos_client: CosmosClient = current_app.config[CONFIG_COSMOS_HISTORY_CLIENT]
        await cosmos_client.close()
