import asyncio
import hashlib
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any

from azure.core import MatchConditions
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from quart import current_app
from werkzeug.exceptions import ServiceUnavailable

from config import (
    CONFIG_COSMOS_SETTINGS_CONTAINER,
    CONFIG_PUBLIC_ACCESS_ENABLED,
    CONFIG_PUBLIC_USAGE_LIMITER,
    CONFIG_RATE_LIMITER,
)

PUBLIC_DAILY_REQUEST_LIMIT = 200


@dataclass
class UserRateInfo:
    timestamps: list[float] = field(default_factory=list)


class RateLimiter:
    def __init__(self, max_requests: int = 20, window_seconds: int = 60, max_users: int = 10000):
        if min(max_requests, window_seconds, max_users) < 1:
            raise ValueError("Rate limits must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_users = max_users
        self.users: dict[str, UserRateInfo] = {}
        self.last_cleanup = time.monotonic()

    def is_rate_limited(self, user_oid: str) -> bool:
        now = time.monotonic()
        if now - self.last_cleanup >= self.window_seconds or len(self.users) >= self.max_users:
            self.users = {
                identity: info
                for identity, info in self.users.items()
                if info.timestamps and now - info.timestamps[-1] < self.window_seconds
            }
            self.last_cleanup = now
        if user_oid not in self.users:
            if len(self.users) >= self.max_users:
                return True
            self.users[user_oid] = UserRateInfo()
        info = self.users[user_oid]
        info.timestamps = [timestamp for timestamp in info.timestamps if now - timestamp < self.window_seconds]
        if len(info.timestamps) >= self.max_requests:
            return True
        info.timestamps.append(now)
        return False


async def check_rate_limit(auth_claims: dict[str, Any], remote_addr: str | None) -> bool:
    if current_app.config.get(CONFIG_PUBLIC_ACCESS_ENABLED, True) is not True:
        raise ServiceUnavailable("Public access is temporarily disabled by the owner.")
    user_oid = auth_claims.get("oid")
    if isinstance(user_oid, str) and user_oid and user_oid != "anonymous":
        identity = f"user:{user_oid}"
    else:
        identity = f"remote:{remote_addr or 'unknown'}"
    identity_key = "rate_limit:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    limiter: RateLimiter | None = current_app.config.get(CONFIG_RATE_LIMITER)
    if limiter is None:
        limiter = RateLimiter()
        current_app.config[CONFIG_RATE_LIMITER] = limiter
    container: ContainerProxy | None = current_app.config.get(CONFIG_COSMOS_SETTINGS_CONTAINER)
    if await consume_rate_limit(limiter, identity_key, container):
        return True
    daily_limiter: RateLimiter | None = current_app.config.get(CONFIG_PUBLIC_USAGE_LIMITER)
    if daily_limiter is None:
        daily_limiter = RateLimiter(max_requests=PUBLIC_DAILY_REQUEST_LIMIT, window_seconds=86400, max_users=1)
        current_app.config[CONFIG_PUBLIC_USAGE_LIMITER] = daily_limiter
    daily_key = "rate_limit:" + hashlib.sha256(b"deployment:daily-public-requests").hexdigest()
    return await consume_rate_limit(daily_limiter, daily_key, container)


async def consume_rate_limit(limiter: RateLimiter, identity_key: str, container: ContainerProxy | None) -> bool:
    if container is None:
        return limiter.is_rate_limited(identity_key)

    for attempt in range(8):
        try:
            try:
                stored = await container.read_item(item=identity_key, partition_key=identity_key)
            except CosmosResourceNotFoundError:
                stored = None

            now = time.time()
            timestamps = []
            etag = None
            if stored is not None:
                etag = stored.get("_etag")
                expires_at = stored.get("expires_at")
                stored_timestamps = stored.get("timestamps")
                if (
                    stored.get("id") != identity_key
                    or stored.get("type") != "rate_limit"
                    or not isinstance(etag, str)
                    or not etag
                    or etag == "*"
                    or not isinstance(expires_at, (int, float))
                    or isinstance(expires_at, bool)
                    or not math.isfinite(expires_at)
                    or not isinstance(stored_timestamps, list)
                    or any(type(stamp) not in (int, float) or not math.isfinite(stamp) for stamp in stored_timestamps)
                ):
                    raise ValueError("Invalid rate limit record")
                if expires_at > now:
                    timestamps = [stamp for stamp in stored_timestamps if now - stamp < limiter.window_seconds]

            if len(timestamps) >= limiter.max_requests:
                return True
            timestamps.append(now)
            body = {
                "id": identity_key,
                "type": "rate_limit",
                "timestamps": timestamps,
                "expires_at": now + limiter.window_seconds,
                "ttl": limiter.window_seconds,
            }
            if stored is None:
                await container.create_item(body=body)
            else:
                await container.replace_item(
                    item=identity_key,
                    body=body,
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                )
            return False
        except CosmosHttpResponseError as error:
            if error.status_code not in (404, 409, 412) or attempt == 7:
                raise ServiceUnavailable("Rate limit storage is unavailable. Please try again later.") from error
        except Exception as error:
            raise ServiceUnavailable("Rate limit storage is unavailable. Please try again later.") from error
        await asyncio.sleep(random.uniform(0, min(0.01 * 2**attempt, 0.25)))

    raise ServiceUnavailable("Rate limit storage is unavailable. Please try again later.")
