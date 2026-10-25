import logging
import math
import os
import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from azure.core import MatchConditions
from azure.cosmos import PartitionKey
from azure.cosmos.aio import ContainerProxy, CosmosClient
from azure.cosmos.exceptions import (
    CosmosHttpResponseError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)
from azure.identity.aio import AzureDeveloperCliCredential, ManagedIdentityCredential
from quart import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from config import (
    CONFIG_ADMIN_SETTINGS_CACHE,
    CONFIG_COSMOS_ANALYTICS_CONTAINER,
    CONFIG_COSMOS_SETTINGS_CONTAINER,
    CONFIG_CREDENTIAL,
    CONFIG_PROFILE,
)
from core.profile import (
    DEFAULT_PROFILE,
    PROFILE_FIELDS,
    PROFILE_TEXT_LIMITS,
    validate_profile_update,
)
from decorators import admin_required
from error import error_response

admin_settings_bp = Blueprint("admin_settings", __name__)

COSMOS_SETTINGS_CLIENT_KEY = "admin_cosmos_client"

DEFAULT_SETTINGS: dict[str, Any] = {
    "id": "admin_settings",
    **deepcopy(DEFAULT_PROFILE),
}

SETTINGS_FIELDS = PROFILE_FIELDS
SETTINGS_TEXT_LIMITS = PROFILE_TEXT_LIMITS
MAX_FUN_FACTS = 50
MAX_FUN_FACT_LENGTH = 1000
MAX_SETTINGS_REQUEST_BYTES = 64 * 1024


def validate_settings_update(settings: Any) -> dict[str, Any]:
    return validate_profile_update(settings)


def sanitize_settings(settings: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("Stored settings must be a JSON object")
    baseline = DEFAULT_PROFILE | (defaults or {})
    sanitized = {
        field: deepcopy(settings.get(field, default)) for field, default in baseline.items() if field in SETTINGS_FIELDS
    }
    return validate_settings_update(sanitized)


async def get_current_settings() -> dict[str, Any]:
    container: ContainerProxy | None = current_app.config.get(CONFIG_COSMOS_SETTINGS_CONTAINER)
    if container is not None:
        settings: dict[str, Any] = await container.read_item(item="admin_settings", partition_key="admin_settings")
    else:
        settings = (
            current_app.config.get(CONFIG_ADMIN_SETTINGS_CACHE)
            or current_app.config.get(CONFIG_PROFILE)
            or DEFAULT_SETTINGS
        )
    return sanitize_settings(settings, current_app.config.get(CONFIG_PROFILE))


@admin_settings_bp.get("/api/admin/settings")
@admin_required
async def get_admin_settings(auth_claims: dict[str, Any]):
    try:
        return jsonify(await get_current_settings()), 200
    except Exception as error:
        return error_response(error, "/api/admin/settings")


@admin_settings_bp.put("/api/admin/settings")
@admin_required
async def update_admin_settings(auth_claims: dict[str, Any]):
    try:
        if not request.is_json:
            return jsonify({"error": "request must be json"}), 415
        if (request.content_length or 0) > MAX_SETTINGS_REQUEST_BYTES:
            return jsonify({"error": "Settings request is too large"}), 413
        if len(await request.get_data()) > MAX_SETTINGS_REQUEST_BYTES:
            return jsonify({"error": "Settings request is too large"}), 413
        try:
            updates = validate_settings_update(await request.get_json())
        except BadRequest:
            return jsonify({"error": "Invalid JSON body"}), 400
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        container: ContainerProxy | None = current_app.config.get(CONFIG_COSMOS_SETTINGS_CONTAINER)
        if container is None:
            return jsonify({"error": "Settings storage not configured"}), 500

        stored_settings = await container.read_item(item="admin_settings", partition_key="admin_settings")
        current_settings = sanitize_settings(stored_settings, current_app.config.get(CONFIG_PROFILE))
        current_settings.update(updates)
        etag = stored_settings.get("_etag")
        if not isinstance(etag, str) or not etag or etag == "*":
            raise ValueError("Stored settings must have a valid ETag")
        await container.replace_item(
            item="admin_settings",
            body={"id": "admin_settings", **current_settings},
            etag=etag,
            match_condition=MatchConditions.IfNotModified,
        )
        current_app.config[CONFIG_ADMIN_SETTINGS_CACHE] = current_settings
        return jsonify(current_settings), 200
    except RequestEntityTooLarge:
        return jsonify({"error": "Settings request is too large"}), 413
    except CosmosHttpResponseError as error:
        if error.status_code in (409, 412):
            return jsonify({"error": "Settings changed while saving. Reload settings and try again."}), 409
        return error_response(error, "/api/admin/settings")
    except Exception as error:
        return error_response(error, "/api/admin/settings")


@admin_settings_bp.get("/api/status")
async def get_public_status():
    try:
        settings = await get_current_settings()
        return (
            jsonify({field: value for field, value in settings.items() if field not in {"manager_email", "fun_facts"}}),
            200,
        )
    except Exception as error:
        return error_response(error, "/api/status")


# --- Admin Analytics ---


@admin_settings_bp.get("/api/admin/analytics")
@admin_required
async def get_admin_analytics(auth_claims: dict[str, Any]):
    try:
        container: ContainerProxy | None = current_app.config.get(CONFIG_COSMOS_ANALYTICS_CONTAINER)
        if not container:
            return jsonify({"error": "Analytics not configured"}), 503

        now_ms = int(time.time() * 1000)
        thirty_days_ago_ms = now_ms - 30 * 86400 * 1000
        return jsonify(await compute_analytics_summary(container, thirty_days_ago_ms, now_ms)), 200
    except Exception as error:
        return error_response(error, "/api/admin/analytics")


async def compute_analytics_summary(container: ContainerProxy, since: int, until: int) -> dict[str, Any]:
    if since > until:
        raise ValueError("Usage reporting window is invalid")
    parameters = [{"name": "@since", "value": since}, {"name": "@until", "value": until}]
    day_ms = 86400 * 1000
    counts_by_day = dict.fromkeys(range(since // day_ms, until // day_ms + 1), 0)
    sessions: set[str] = set()
    total = 0
    response_time_total = 0.0
    example_count = 0
    error_count = 0
    recent_activity: list[dict[str, Any]] = []

    # Use a plain projection; the deployed async client rejects the previous aggregate query.
    # Page only operational fields; keep daily/session summaries and the latest 20 records.
    async for item in container.query_items(
        query=(
            "SELECT c.timestamp, c.response_time_ms, c.session_id, c.error, c.is_example_question "
            "FROM c WHERE c.timestamp >= @since AND c.timestamp <= @until"
        ),
        parameters=parameters,
        max_item_count=100,
    ):
        timestamp = item.get("timestamp")
        response_time = item.get("response_time_ms")
        if (
            not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or not since <= timestamp <= until
            or not isinstance(response_time, (int, float))
            or isinstance(response_time, bool)
            or not math.isfinite(response_time)
            or response_time < 0
        ):
            raise ValueError("Stored usage record has an invalid timestamp or response duration")
        total += 1
        response_time_total += response_time
        counts_by_day[timestamp // day_ms] += 1
        session_id = item.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            sessions.add(session_id)
        is_example = item.get("is_example_question") is True
        failed = isinstance(item.get("error"), str) and bool(item["error"].strip())
        example_count += is_example
        error_count += failed
        recent_activity.append(
            {
                "timestamp": timestamp,
                "response_time_ms": round(response_time),
                "is_example_question": is_example,
                "failed": failed,
            }
        )
        recent_activity.sort(key=lambda activity: activity["timestamp"], reverse=True)
        del recent_activity[20:]

    return {
        "total_queries": total,
        "total_sessions": len(sessions),
        "avg_response_time_ms": round(response_time_total / total) if total else 0,
        "queries_by_day": [
            {"date": datetime.fromtimestamp(day * 86400, UTC).strftime("%Y-%m-%d"), "count": count}
            for day, count in counts_by_day.items()
        ],
        "recent_activity": recent_activity,
        "example_question_rate": round(example_count / total * 100, 1) if total else 0,
        "error_count": error_count,
    }


@admin_settings_bp.before_app_serving
async def setup_clients():
    AZURE_COSMOSDB_ACCOUNT = os.getenv("AZURE_COSMOSDB_ACCOUNT")
    AZURE_ADMIN_SETTINGS_DATABASE = os.getenv(
        "AZURE_ADMIN_SETTINGS_DATABASE", os.getenv("AZURE_CHAT_HISTORY_DATABASE", "")
    )
    AZURE_ADMIN_SETTINGS_CONTAINER = os.getenv("AZURE_ADMIN_SETTINGS_CONTAINER", "admin_settings")
    AZURE_ANALYTICS_CONTAINER = os.getenv("AZURE_ANALYTICS_CONTAINER", "analytics")

    if not AZURE_COSMOSDB_ACCOUNT or not AZURE_ADMIN_SETTINGS_DATABASE:
        if os.getenv("RUNNING_IN_PRODUCTION", "").lower() == "true" or os.getenv("WEBSITE_HOSTNAME"):
            raise ValueError("Hosted public access requires Cosmos settings/shared usage storage to be configured.")
        current_app.logger.info("Admin settings Cosmos DB not configured, using defaults")
        current_app.config[CONFIG_ADMIN_SETTINGS_CACHE] = {
            "id": "admin_settings",
            **deepcopy(current_app.config.get(CONFIG_PROFILE, DEFAULT_PROFILE)),
        }
        return

    azure_credential: AzureDeveloperCliCredential | ManagedIdentityCredential = current_app.config[CONFIG_CREDENTIAL]

    cosmos_client: CosmosClient | None = None
    try:
        cosmos_client = CosmosClient(
            url=f"https://{AZURE_COSMOSDB_ACCOUNT}.documents.azure.com:443/",
            credential=azure_credential,
        )
        cosmos_db = cosmos_client.get_database_client(AZURE_ADMIN_SETTINGS_DATABASE)

        settings_container = cosmos_db.get_container_client(AZURE_ADMIN_SETTINGS_CONTAINER)
        current_app.config[CONFIG_COSMOS_SETTINGS_CONTAINER] = settings_container
        current_app.config[COSMOS_SETTINGS_CLIENT_KEY] = cosmos_client

        try:
            settings = await get_current_settings()
            current_app.logger.info("Loaded admin settings from Cosmos DB")
        except CosmosResourceNotFoundError:
            try:
                await settings_container.create_item(
                    {"id": "admin_settings", **deepcopy(current_app.config.get(CONFIG_PROFILE, DEFAULT_PROFILE))}
                )
            except CosmosResourceExistsError:
                pass
            settings = await get_current_settings()
            current_app.logger.info("Initialized admin settings in Cosmos DB")
        current_app.config[CONFIG_ADMIN_SETTINGS_CACHE] = settings
    except Exception:
        logging.exception("Failed to set up admin settings Cosmos DB")
        if cosmos_client is not None:
            current_app.config.pop(COSMOS_SETTINGS_CLIENT_KEY, None)
            await cosmos_client.close()
        raise

    try:
        analytics_container = cosmos_db.get_container_client(AZURE_ANALYTICS_CONTAINER)
        try:
            await analytics_container.read()
            current_app.logger.info("Analytics container '%s' ready", AZURE_ANALYTICS_CONTAINER)
        except CosmosResourceNotFoundError:
            current_app.logger.warning("Analytics container '%s' not found, creating it", AZURE_ANALYTICS_CONTAINER)
            analytics_container = await cosmos_db.create_container_if_not_exists(
                id=AZURE_ANALYTICS_CONTAINER,
                partition_key=PartitionKey(path="/user_oid"),
            )
            current_app.logger.info("Analytics container '%s' created", AZURE_ANALYTICS_CONTAINER)
        current_app.config[CONFIG_COSMOS_ANALYTICS_CONTAINER] = analytics_container
        current_app.logger.info("Admin settings and analytics Cosmos DB clients ready")
    except Exception:
        logging.exception("Failed to set up analytics Cosmos DB")
        current_app.config[CONFIG_COSMOS_ANALYTICS_CONTAINER] = None


@admin_settings_bp.after_app_serving
async def close_clients():
    cosmos_client = current_app.config.get(COSMOS_SETTINGS_CLIENT_KEY)
    if cosmos_client:
        await cosmos_client.close()
