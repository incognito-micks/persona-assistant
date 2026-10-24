import asyncio
import logging
import math
import random
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from azure.core import MatchConditions
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from quart import current_app
from werkzeug.exceptions import ServiceUnavailable

from config import CONFIG_COSMOS_SETTINGS_CONTAINER

logger = logging.getLogger(__name__)

MAX_SESSION_SECONDS = 300
MAX_CONCURRENT_VOICE_SESSIONS = 3
MAX_DAILY_VOICE_SESSIONS = 20
VOICE_LEASE_SECONDS = 400
STORE_OPERATION_TIMEOUT_SECONDS = 10
MAX_STORE_ATTEMPTS = 8
VOICE_USAGE_DOCUMENT_ID = "voice_usage"
VOICE_USAGE_LOCAL_KEY = "voice_usage_local"
SECONDS_PER_DAY = 24 * 60 * 60


class VoiceUsageLimitExceeded(Exception):
    pass


@dataclass
class LocalVoiceUsage:
    record: dict[str, Any] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class VoiceSessionLease:
    def __init__(self, container: ContainerProxy | None, local: LocalVoiceUsage):
        self.container = container
        self.local = local
        self.identifier = uuid4().hex
        self.may_exist = False
        self.admitted = False

    def updated_record(self, stored: dict[str, Any] | None, *, releasing: bool) -> dict[str, Any] | None:
        now = time.time()
        today = int(now // SECONDS_PER_DAY)
        if stored is None:
            if releasing:
                return None
            record: dict[str, Any] = {
                "id": VOICE_USAGE_DOCUMENT_ID,
                "type": "voice_usage",
                "day": today,
                "admitted": 0,
                "leases": {},
                "ttl": -1,
            }
        else:
            if (
                stored.get("id") != VOICE_USAGE_DOCUMENT_ID
                or stored.get("type") != "voice_usage"
                or type(stored.get("day")) is not int
                or stored["day"] < 0
                or type(stored.get("admitted")) is not int
                or not 0 <= stored["admitted"] <= MAX_DAILY_VOICE_SESSIONS
                or not isinstance(stored.get("leases"), dict)
                or len(stored["leases"]) > MAX_CONCURRENT_VOICE_SESSIONS
                or type(stored.get("ttl")) is not int
                or stored["ttl"] != -1
            ):
                raise ValueError("Invalid voice usage record")
            for identifier, lease in stored["leases"].items():
                if (
                    not isinstance(identifier, str)
                    or len(identifier) != 32
                    or any(character not in "0123456789abcdef" for character in identifier)
                    or not isinstance(lease, dict)
                    or type(lease.get("day")) is not int
                    or not 0 <= lease["day"] <= stored["day"]
                    or type(lease.get("expires_at")) not in (int, float)
                    or not math.isfinite(lease["expires_at"])
                ):
                    raise ValueError("Invalid voice usage lease")
            if stored["admitted"] < sum(lease["day"] == stored["day"] for lease in stored["leases"].values()):
                raise ValueError("Invalid voice usage admission count")
            record = {
                "id": VOICE_USAGE_DOCUMENT_ID,
                "type": "voice_usage",
                "day": max(today, stored["day"]),
                "admitted": 0 if today > stored["day"] else stored["admitted"],
                "leases": {
                    identifier: dict(lease)
                    for identifier, lease in stored["leases"].items()
                    if lease["expires_at"] > now
                },
                "ttl": -1,
            }

        if releasing:
            lease = record["leases"].pop(self.identifier, None)
            if lease is None:
                return None
            if not self.admitted and lease["day"] == record["day"]:
                record["admitted"] = max(0, record["admitted"] - 1)
        else:
            if len(record["leases"]) >= MAX_CONCURRENT_VOICE_SESSIONS:
                raise VoiceUsageLimitExceeded("Voice session capacity reached")
            if record["admitted"] >= MAX_DAILY_VOICE_SESSIONS:
                raise VoiceUsageLimitExceeded("Daily voice session limit reached")
            record["admitted"] += 1
            record["leases"][self.identifier] = {
                "day": record["day"],
                "expires_at": now + VOICE_LEASE_SECONDS,
            }
        return record

    async def update(self, *, releasing: bool) -> None:
        if self.container is None:
            async with self.local.lock:
                body = self.updated_record(self.local.record, releasing=releasing)
                if body is not None:
                    if not releasing:
                        self.may_exist = True
                    self.local.record = body
            return

        for attempt in range(MAX_STORE_ATTEMPTS):
            try:
                try:
                    stored = await self.container.read_item(
                        item=VOICE_USAGE_DOCUMENT_ID, partition_key=VOICE_USAGE_DOCUMENT_ID
                    )
                except CosmosResourceNotFoundError:
                    stored = None
                etag = None
                if stored is not None:
                    etag = stored.get("_etag")
                    if not isinstance(etag, str) or not etag or etag == "*":
                        raise ValueError("Invalid voice usage ETag")
                body = self.updated_record(stored, releasing=releasing)
                if body is None:
                    return
                if not releasing:
                    # A timed-out/cancelled write may still have committed in Cosmos.
                    self.may_exist = True
                if stored is None:
                    await self.container.create_item(body=body)
                else:
                    await self.container.replace_item(
                        item=VOICE_USAGE_DOCUMENT_ID,
                        body=body,
                        etag=etag,
                        match_condition=MatchConditions.IfNotModified,
                    )
                return
            except VoiceUsageLimitExceeded:
                raise
            except CosmosHttpResponseError as error:
                if error.status_code not in (404, 409, 412) or attempt == MAX_STORE_ATTEMPTS - 1:
                    raise ServiceUnavailable("Voice capacity storage is unavailable") from error
            except Exception as error:
                raise ServiceUnavailable("Voice capacity storage is unavailable") from error
            await asyncio.sleep(random.uniform(0, min(0.01 * 2**attempt, 0.1)))
        raise ServiceUnavailable("Voice capacity storage is unavailable")

    async def acquire(self) -> None:
        await self.update(releasing=False)

    async def release(self) -> None:
        await self.update(releasing=True)
        self.may_exist = False


@asynccontextmanager
async def voice_session_usage() -> AsyncGenerator[VoiceSessionLease, None]:
    """Refund known setup failures; abandoned reservations expire without a daily refund."""
    container: ContainerProxy | None = current_app.config.get(CONFIG_COSMOS_SETTINGS_CONTAINER)
    local: LocalVoiceUsage | None = current_app.config.get(VOICE_USAGE_LOCAL_KEY)
    if local is None:
        local = LocalVoiceUsage()
        current_app.config[VOICE_USAGE_LOCAL_KEY] = local
    lease = VoiceSessionLease(container, local)
    failed = False
    try:
        await asyncio.wait_for(lease.acquire(), timeout=STORE_OPERATION_TIMEOUT_SECONDS)
        yield lease
    except BaseException:
        failed = True
        raise
    finally:
        if lease.may_exist:
            cleanup = asyncio.create_task(
                asyncio.wait_for(lease.release(), timeout=STORE_OPERATION_TIMEOUT_SECONDS),
                name="realtime-voice-lease-release",
            )
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                try:
                    await cleanup
                except Exception:
                    logger.warning("Voice capacity release failed; reservation will expire")
                raise
            except Exception:
                logger.warning("Voice capacity release failed; reservation will expire")
                if not failed:
                    raise
