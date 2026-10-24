from collections.abc import Callable
from functools import wraps
from typing import Any, TypeVar, cast

from quart import abort, current_app, request

from config import CONFIG_ADMIN_USER_OID, CONFIG_AUTH_CLIENT
from core.authentication import AuthError

_C = TypeVar("_C", bound=Callable[..., Any])


def authenticated(route_fn: _C) -> _C:
    """
    Allow anonymous public access, validating any explicitly supplied credentials.
    """

    @wraps(route_fn)
    async def auth_handler(*args, **kwargs):
        auth_helper = current_app.config[CONFIG_AUTH_CLIENT]
        try:
            auth_claims = await auth_helper.get_auth_claims_if_enabled(request.headers)
        except AuthError:
            abort(403)

        return await route_fn(auth_claims, *args, **kwargs)

    return cast(_C, auth_handler)


def admin_required(route_fn: _C) -> _C:
    """
    Require a verified Entra user matching the explicitly configured owner object ID.
    """

    @wraps(route_fn)
    async def auth_handler(*args, **kwargs):
        auth_helper = current_app.config[CONFIG_AUTH_CLIENT]
        try:
            auth_claims = await auth_helper.get_auth_claims_if_enabled(request.headers)
        except AuthError:
            abort(403)

        if auth_claims.get("auth_provider") != "aad":
            abort(403)

        admin_oid = current_app.config.get(CONFIG_ADMIN_USER_OID)
        user_oid = auth_claims.get("oid")
        if (
            not isinstance(admin_oid, str)
            or not admin_oid.strip()
            or not isinstance(user_oid, str)
            or not user_oid.strip()
            or user_oid != admin_oid
        ):
            abort(403)

        return await route_fn(auth_claims, *args, **kwargs)

    return cast(_C, auth_handler)
