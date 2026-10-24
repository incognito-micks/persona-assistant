import asyncio
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import aiohttp
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from quart import Quart
from werkzeug.datastructures import Headers

import core.authentication as authentication
from config import CONFIG_ADMIN_USER_OID, CONFIG_AUTH_CLIENT
from core.authentication import (
    AuthenticationHelper,
    AuthError,
    create_owner_authentication_helper,
)
from decorators import admin_required, authenticated

from .mocks import MockResponse

B2C_OPTIONS = {
    "b2c_tenant_name": "external",
    "b2c_tenant_id": "B2C_TENANT",
    "b2c_client_app_id": "B2C_CLIENT",
    "b2c_policy": "B2C_1_signin",
}
B2C_ISSUER = "https://external.b2clogin.com/B2C_TENANT/v2.0/"
OWNER_ENVIRONMENT = {
    "AZURE_TENANT_ID": "TENANT_ID",
    "AZURE_CLIENT_APP_ID": "CLIENT_APP",
    "ADMIN_USER_OID": "OID_X",
}
PUBLIC_AUTH_SETUP = {
    "useLogin": False,
    "requireAccessControl": False,
    "enableUnauthenticatedAccess": True,
}


def create_authentication_helper(
    enforce_access_control: bool = False,
    enable_unauthenticated_access: bool = False,
    **options,
):
    configuration = {
        "use_authentication": True,
        "server_app_id": "SERVER_APP",
        "server_app_secret": "SERVER_SECRET",
        "client_app_id": "CLIENT_APP",
        "tenant_id": "TENANT_ID",
        "enforce_access_control": enforce_access_control,
        "enable_unauthenticated_access": enable_unauthenticated_access,
    }
    configuration.update(options)
    return AuthenticationHelper(**configuration)


def create_mock_jwt(kid="mock_kid", oid="OID_X", **claims):
    # Create a payload with necessary claims
    payload = {
        "iss": "https://login.microsoftonline.com/TENANT_ID/v2.0",
        "sub": "AaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaA",
        "aud": "SERVER_APP",
        "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
        "iat": int(datetime.now(timezone.utc).timestamp()),
        "nbf": int(datetime.now(timezone.utc).timestamp()),
        "name": "John Doe",
        "oid": oid,
        "preferred_username": "john.doe@example.com",
        "scp": "access_as_user",
        "rh": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA.",
        "tid": "22222222-2222-2222-2222-222222222222",
        "uti": "AbCdEfGhIjKlMnOp-ABCDEFG",
        "ver": "2.0",
    }
    payload.update(claims)
    if payload.get("scp") is None:
        payload.pop("scp", None)

    # Create a header
    header = {"kid": kid, "alg": "RS256", "typ": "JWT"}

    # Create a mock private key (for signing)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Create the JWT
    token = jwt.encode(payload, private_key, algorithm="RS256", headers=header)

    return token, private_key.public_key(), payload


def create_mock_jwks(public_key, kid="mock_kid"):
    key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    key.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return {"keys": [key]}


@pytest.fixture
def jwks_endpoint(monkeypatch):
    endpoint = SimpleNamespace(requests=[], responses=[])

    class DelayedResponse(MockResponse):
        async def __aenter__(self):
            await asyncio.sleep(0)
            return self

    def respond(data, status=200):
        endpoint.responses.append(DelayedResponse(status=status, text=json.dumps(data)))

    def mock_get(self, *, url, allow_redirects):
        assert allow_redirects is False
        endpoint.requests.append(url)
        response = endpoint.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    endpoint.respond = respond
    monkeypatch.setattr(aiohttp.ClientSession, "get", mock_get)
    return endpoint


@pytest.fixture
def cache_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(authentication, "monotonic", lambda: now[0])
    return now


@pytest.mark.asyncio
async def test_get_auth_claims_success(jwks_endpoint):
    helper = create_authentication_helper(enforce_access_control=True)
    mock_token, public_key, claims = create_mock_jwt(oid="OID_X")
    jwks_endpoint.respond(create_mock_jwks(public_key))
    auth_claims = await helper.get_auth_claims_if_enabled(headers={"Authorization": f"Bearer {mock_token}"})
    assert "access_token" not in auth_claims
    assert auth_claims.get("oid") == "OID_X"
    assert auth_claims.get("email") == claims["preferred_username"]
    assert auth_claims.get("name") == claims["name"]
    assert auth_claims.get("auth_provider") == "aad"


@pytest.mark.asyncio
async def test_get_auth_claims_success_no_required(jwks_endpoint):
    helper = create_authentication_helper(enforce_access_control=False)
    mock_token, public_key, claims = create_mock_jwt(oid="OID_X")
    jwks_endpoint.respond(create_mock_jwks(public_key))
    auth_claims = await helper.get_auth_claims_if_enabled(headers={"Authorization": f"Bearer {mock_token}"})
    assert "access_token" not in auth_claims
    assert auth_claims.get("oid") == claims["oid"]


@pytest.mark.asyncio
async def test_get_auth_claims_unauthorized():
    helper = create_authentication_helper()
    with pytest.raises(AuthError) as exc_info:
        await helper.get_auth_claims_if_enabled(headers={"Authorization": "Bearer invalid-token"})
    assert exc_info.value.status_code == 401


def test_auth_setup():
    helper = create_authentication_helper()
    result = helper.get_auth_setup_for_client()
    assert result == PUBLIC_AUTH_SETUP


def test_auth_setup_ignores_legacy_access_control():
    helper = create_authentication_helper(enforce_access_control=True)
    result = helper.get_auth_setup_for_client()
    assert result == PUBLIC_AUTH_SETUP


def test_auth_setup_ignores_legacy_access_control_and_anonymous_flags():
    helper = create_authentication_helper(enforce_access_control=True, enable_unauthenticated_access=True)
    result = helper.get_auth_setup_for_client()
    assert result == PUBLIC_AUTH_SETUP


def test_get_auth_token():
    with pytest.raises(AuthError) as exc_info:
        AuthenticationHelper.get_token_auth_header({})
    assert exc_info.value.status_code == 401
    with pytest.raises(AuthError) as exc_info:
        AuthenticationHelper.get_token_auth_header({"Authorization": ". ."})
    assert exc_info.value.status_code == 401
    with pytest.raises(AuthError) as exc_info:
        AuthenticationHelper.get_token_auth_header({"Authorization": "invalid"})
    assert exc_info.value.status_code == 401
    with pytest.raises(AuthError) as exc_info:
        AuthenticationHelper.get_token_auth_header({"Authorization": "invalid MockToken"})
    assert exc_info.value.status_code == 401
    assert AuthenticationHelper.get_token_auth_header({"Authorization": "Bearer MockToken"}) == "MockToken"
    assert AuthenticationHelper.get_token_auth_header({"x-ms-token-aad-access-token": "MockToken"}) == "MockToken"


@pytest.mark.parametrize("authorization", ["Bearer", "Bearer one two"])
def test_malformed_bearer_headers_are_rejected(authorization):
    with pytest.raises(AuthError) as exc_info:
        AuthenticationHelper.get_token_auth_header({"Authorization": authorization})
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_create_pem_format():
    helper = create_authentication_helper()
    mock_token, public_key, payload = create_mock_jwt(oid="OID_X")
    _, other_public_key, _ = create_mock_jwt(oid="OID_Y")
    mock_jwks = {
        "keys": [
            # Include a key with a different KID to ensure the correct key is selected
            {
                "kty": "RSA",
                "kid": "other_mock_kid",
                "use": "sig",
                "n": base64.urlsafe_b64encode(
                    other_public_key.public_numbers().n.to_bytes(
                        (other_public_key.public_numbers().n.bit_length() + 7) // 8, byteorder="big"
                    )
                )
                .decode("utf-8")
                .rstrip("="),
                "e": base64.urlsafe_b64encode(
                    other_public_key.public_numbers().e.to_bytes(
                        (other_public_key.public_numbers().e.bit_length() + 7) // 8, byteorder="big"
                    )
                )
                .decode("utf-8")
                .rstrip("="),
            },
            {
                "kty": "RSA",
                "kid": "mock_kid",
                "use": "sig",
                "n": base64.urlsafe_b64encode(
                    public_key.public_numbers().n.to_bytes(
                        (public_key.public_numbers().n.bit_length() + 7) // 8, byteorder="big"
                    )
                )
                .decode("utf-8")
                .rstrip("="),
                "e": base64.urlsafe_b64encode(
                    public_key.public_numbers().e.to_bytes(
                        (public_key.public_numbers().e.bit_length() + 7) // 8, byteorder="big"
                    )
                )
                .decode("utf-8")
                .rstrip("="),
            },
        ]
    }

    pem_key = await helper.create_pem_format(mock_jwks, mock_token)

    # Assert that the result is bytes
    assert isinstance(pem_key, bytes), "create_pem_format should return bytes"

    # Convert bytes to string for regex matching
    pem_str = pem_key.decode("utf-8")

    # Assert that the key starts and ends with the correct markers
    assert pem_str.startswith("-----BEGIN PUBLIC KEY-----"), "PEM key should start with the correct marker"
    assert pem_str.endswith("-----END PUBLIC KEY-----\n"), "PEM key should end with the correct marker"

    # Assert that the format matches the structure of a PEM key
    pem_regex = r"^-----BEGIN PUBLIC KEY-----\n([A-Za-z0-9+/\n]+={0,2})\n-----END PUBLIC KEY-----\n$"
    assert re.match(pem_regex, pem_str), "PEM key format is incorrect"

    # Verify that the key can be used to decode the token
    try:
        decoded = jwt.decode(
            mock_token, key=pem_key, algorithms=["RS256"], audience=payload["aud"], issuer=payload["iss"]
        )
        assert decoded["oid"] == payload["oid"], "Decoded token should contain correct OID"
    except Exception as e:
        pytest.fail(f"jwt.decode raised an unexpected exception: {str(e)}")

    # Try to load the key using cryptography library to ensure it's a valid PEM format
    try:
        loaded_public_key = serialization.load_pem_public_key(pem_key)
        assert isinstance(loaded_public_key, rsa.RSAPublicKey), "Loaded key should be an RSA public key"
    except Exception as e:
        pytest.fail(f"Failed to load PEM key: {str(e)}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audience", "issuer", "id_token_mode"),
    [
        ("SERVER_APP", "https://login.microsoftonline.com/TENANT_ID/v2.0", False),
        ("api://SERVER_APP", "https://sts.windows.net/TENANT_ID/", False),
        ("CLIENT_APP", "https://login.microsoftonline.com/TENANT_ID/v2.0", False),
        ("CLIENT_APP", "https://login.microsoftonline.com/TENANT_ID/v2.0", True),
    ],
)
async def test_validate_access_token(jwks_endpoint, audience, issuer, id_token_mode):
    token, public_key, claims = create_mock_jwt(aud=audience, iss=issuer)
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper(id_token_mode=id_token_mode)
    assert await helper.validate_access_token(token) is False


@pytest.mark.asyncio
async def test_jwks_cache_reuses_provider_keys(jwks_endpoint):
    token, public_key, claims = create_mock_jwt()
    jwks_endpoint.respond(create_mock_jwks(public_key))
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    assert await helper.validate_access_token(token) is False
    assert await helper.validate_access_token(token) is False
    assert jwks_endpoint.requests == [helper.key_url]


@pytest.mark.asyncio
@pytest.mark.parametrize("enforce_access_control", [False, True])
@pytest.mark.parametrize("id_token_mode", [False, True])
async def test_claims_do_not_construct_or_exchange_with_msal(
    monkeypatch, jwks_endpoint, enforce_access_control, id_token_mode
):
    def reject_msal(*args, **kwargs):
        pytest.fail("Claims resolution must not construct a confidential client or exchange a Search token")

    monkeypatch.setattr("msal.ConfidentialClientApplication.__init__", reject_msal)
    monkeypatch.setattr("msal.ConfidentialClientApplication.acquire_token_on_behalf_of", reject_msal)
    token, public_key, claims = create_mock_jwt(
        aud="CLIENT_APP" if id_token_mode else "SERVER_APP", scp=None if id_token_mode else "access_as_user"
    )
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper(
        enforce_access_control=enforce_access_control, id_token_mode=id_token_mode, server_app_secret=None
    )

    assert await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {token}"}) == {
        "oid": claims["oid"],
        "email": claims["preferred_username"],
        "name": claims["name"],
        "auth_provider": "aad",
    }


@pytest.mark.asyncio
async def test_concurrent_jwks_cache_misses_share_fetch(jwks_endpoint):
    token, public_key, claims = create_mock_jwt()
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    assert await asyncio.gather(*(helper.validate_access_token(token) for request in range(12))) == [False] * 12
    assert jwks_endpoint.requests == [helper.key_url]


@pytest.mark.asyncio
async def test_jwks_cache_expires_and_coalesces_refresh(jwks_endpoint, cache_clock):
    token, public_key, claims = create_mock_jwt()
    jwks_endpoint.respond(create_mock_jwks(public_key))
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    assert await helper.validate_access_token(token) is False
    cache_clock[0] += authentication.JWKS_CACHE_TTL_SECONDS - 1
    assert await helper.validate_access_token(token) is False
    assert len(jwks_endpoint.requests) == 1
    cache_clock[0] += 1
    assert await asyncio.gather(*(helper.validate_access_token(token) for request in range(12))) == [False] * 12
    assert jwks_endpoint.requests == [helper.key_url, helper.key_url]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_kid", [False, True])
async def test_jwks_key_rotation_refreshes_once_for_concurrent_requests(jwks_endpoint, changed_kid):
    original_token, original_key, original_claims = create_mock_jwt()
    rotated_kid = "rotated_kid" if changed_kid else "mock_kid"
    rotated_token, rotated_key, rotated_claims = create_mock_jwt(kid=rotated_kid)
    jwks_endpoint.respond(create_mock_jwks(original_key))
    jwks_endpoint.respond(create_mock_jwks(rotated_key, kid=rotated_kid))
    helper = create_authentication_helper()

    assert await helper.validate_access_token(original_token) is False
    assert await asyncio.gather(*(helper.validate_access_token(rotated_token) for request in range(12))) == [False] * 12
    assert await helper.validate_access_token(rotated_token) is False
    assert jwks_endpoint.requests == [helper.key_url, helper.key_url]


@pytest.mark.asyncio
async def test_unknown_kids_have_bounded_refresh_and_cache_size(jwks_endpoint, cache_clock):
    token, public_key, claims = create_mock_jwt()
    unknown_token, unknown_key, unknown_claims = create_mock_jwt(kid="unknown")
    rotated_token, rotated_key, rotated_claims = create_mock_jwt(kid="rotated")
    jwks_endpoint.respond(create_mock_jwks(public_key))
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    assert await helper.validate_access_token(token) is False
    for invalid_token in [unknown_token, rotated_token, unknown_token]:
        with pytest.raises(AuthError) as exc_info:
            await helper.validate_access_token(invalid_token)
        assert exc_info.value.status_code == 401
    assert jwks_endpoint.requests == [helper.key_url, helper.key_url]
    assert len(helper.jwks_cache) == 1

    cache_clock[0] += authentication.JWKS_REFRESH_COOLDOWN_SECONDS
    jwks_endpoint.respond(create_mock_jwks(rotated_key, kid="rotated"))
    assert await helper.validate_access_token(rotated_token) is False
    assert len(jwks_endpoint.requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_kid", [False, True])
async def test_failed_rotation_refresh_rejects_token_but_keeps_unexpired_keys(jwks_endpoint, cache_clock, changed_kid):
    token, public_key, claims = create_mock_jwt()
    rotated_kid = "rotated" if changed_kid else "mock_kid"
    rotated_token, rotated_key, rotated_claims = create_mock_jwt(kid=rotated_kid)
    jwks_endpoint.respond(create_mock_jwks(public_key))
    for attempt in range(authentication.JWKS_FETCH_ATTEMPTS):
        jwks_endpoint.respond({}, status=503)
    helper = create_authentication_helper()

    assert await helper.validate_access_token(token) is False
    with pytest.raises(AuthError) as exc_info:
        await helper.validate_access_token(rotated_token)
    assert exc_info.value.status_code == 401
    assert await helper.validate_access_token(token) is False
    with pytest.raises(AuthError):
        await helper.validate_access_token(rotated_token)
    assert len(jwks_endpoint.requests) == 1 + authentication.JWKS_FETCH_ATTEMPTS

    cache_clock[0] += authentication.JWKS_REFRESH_COOLDOWN_SECONDS
    jwks_endpoint.respond(create_mock_jwks(rotated_key, kid=rotated_kid))
    assert await helper.validate_access_token(rotated_token) is False
    assert len(jwks_endpoint.requests) == 2 + authentication.JWKS_FETCH_ATTEMPTS


@pytest.mark.asyncio
async def test_invalid_signatures_do_not_trigger_unbounded_refreshes(jwks_endpoint):
    token, public_key, claims = create_mock_jwt()
    invalid_token, invalid_key, invalid_claims = create_mock_jwt()
    jwks_endpoint.respond(create_mock_jwks(public_key))
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    assert await helper.validate_access_token(token) is False
    for request in range(3):
        with pytest.raises(AuthError) as exc_info:
            await helper.validate_access_token(invalid_token)
        assert exc_info.value.status_code == 401
    assert await helper.validate_access_token(token) is False
    assert jwks_endpoint.requests == [helper.key_url, helper.key_url]


@pytest.mark.asyncio
async def test_expired_jwks_are_not_accepted_after_failed_refresh(jwks_endpoint, cache_clock):
    token, public_key, claims = create_mock_jwt()
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()
    assert await helper.validate_access_token(token) is False

    cache_clock[0] += authentication.JWKS_CACHE_TTL_SECONDS
    jwks_endpoint.responses.extend(
        aiohttp.ClientConnectionError("offline") for attempt in range(authentication.JWKS_FETCH_ATTEMPTS)
    )
    for request in range(2):
        with pytest.raises(AuthError) as exc_info:
            await helper.validate_access_token(token)
        assert exc_info.value.status_code == 401
    assert len(jwks_endpoint.requests) == 1 + authentication.JWKS_FETCH_ATTEMPTS


@pytest.mark.asyncio
async def test_concurrent_failed_jwks_fetches_are_coalesced(jwks_endpoint):
    token, public_key, claims = create_mock_jwt()
    jwks_endpoint.responses.extend(
        aiohttp.ClientConnectionError("offline") for attempt in range(authentication.JWKS_FETCH_ATTEMPTS)
    )
    helper = create_authentication_helper()

    results = await asyncio.gather(
        *(helper.validate_access_token(token) for request in range(12)), return_exceptions=True
    )
    assert all(isinstance(result, AuthError) and result.status_code == 401 for result in results)
    assert len(jwks_endpoint.requests) == authentication.JWKS_FETCH_ATTEMPTS
    assert helper.jwks_cache[helper.key_url].jwks is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["kid", "signature"])
async def test_concurrent_cold_cache_rejections_do_not_refetch_new_keys(jwks_endpoint, failure):
    token, public_key, claims = create_mock_jwt()
    invalid_token, invalid_key, invalid_claims = create_mock_jwt(kid="unknown" if failure == "kid" else "mock_kid")
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    results = await asyncio.gather(
        *(helper.validate_access_token(invalid_token) for request in range(12)), return_exceptions=True
    )
    assert all(isinstance(result, AuthError) and result.status_code == 401 for result in results)
    assert jwks_endpoint.requests == [helper.key_url]


@pytest.mark.asyncio
async def test_jwks_timeouts_and_retries_share_one_short_lived_session(monkeypatch, jwks_endpoint):
    token, public_key, claims = create_mock_jwt()
    jwks_endpoint.responses.append(asyncio.TimeoutError())
    jwks_endpoint.respond(create_mock_jwks(public_key))
    original_session = aiohttp.ClientSession
    sessions = []

    def create_session(**kwargs):
        session = original_session(**kwargs)
        sessions.append((session, kwargs))
        return session

    monkeypatch.setattr(aiohttp, "ClientSession", create_session)
    helper = create_authentication_helper()
    assert await helper.validate_access_token(token) is False
    assert await helper.validate_access_token(token) is False

    assert len(jwks_endpoint.requests) == authentication.JWKS_FETCH_ATTEMPTS
    assert len(sessions) == 1
    session, options = sessions[0]
    assert session.closed
    assert options["timeout"].total == authentication.JWKS_REQUEST_TIMEOUT_SECONDS
    assert options["timeout"].connect == authentication.JWKS_CONNECT_TIMEOUT_SECONDS


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 429, 500, 502, 503, 504])
async def test_jwks_http_errors_fail_closed(jwks_endpoint, status):
    token, public_key, claims = create_mock_jwt()
    attempts = authentication.JWKS_FETCH_ATTEMPTS if status in {429, 500, 502, 503, 504} else 1
    for attempt in range(attempts):
        jwks_endpoint.respond(create_mock_jwks(public_key), status=status)
    helper = create_authentication_helper()

    with pytest.raises(AuthError) as exc_info:
        await helper.validate_access_token(token)
    assert exc_info.value.status_code == 401
    assert len(jwks_endpoint.requests) == attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("document", [None, {}, [], {"keys": []}, {"keys": None}, {"keys": "keys"}, {"keys": [1]}])
async def test_malformed_jwks_fail_closed(jwks_endpoint, document):
    token, public_key, claims = create_mock_jwt()
    jwks_endpoint.respond(document)
    helper = create_authentication_helper()

    with pytest.raises(AuthError) as exc_info:
        await helper.validate_access_token(token)
    assert exc_info.value.status_code == 401
    assert helper.jwks_cache[helper.key_url].jwks is None
    assert len(jwks_endpoint.requests) == 1


@pytest.mark.asyncio
async def test_non_json_jwks_fail_after_bounded_retries(jwks_endpoint):
    token, public_key, claims = create_mock_jwt()
    for attempt in range(authentication.JWKS_FETCH_ATTEMPTS):
        jwks_endpoint.responses.append(MockResponse(status=200, text="<html>unavailable</html>"))
    helper = create_authentication_helper()

    with pytest.raises(AuthError) as exc_info:
        await helper.validate_access_token(token)
    assert exc_info.value.status_code == 401
    assert len(jwks_endpoint.requests) == authentication.JWKS_FETCH_ATTEMPTS
    assert helper.jwks_cache[helper.key_url].jwks is None


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"n": None}, {"n": "!"}, {"e": "!"}])
async def test_malformed_signing_keys_fail_closed(jwks_endpoint, overrides):
    token, public_key, claims = create_mock_jwt()
    document = create_mock_jwks(public_key)
    document["keys"][0].update(overrides)
    jwks_endpoint.respond(document)
    helper = create_authentication_helper()

    with pytest.raises(AuthError) as exc_info:
        await helper.validate_access_token(token)
    assert exc_info.value.status_code == 401
    assert len(jwks_endpoint.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["iss", "aud", "exp", "nbf", "iat", "signature"])
async def test_invalid_claims_and_signature_are_not_accepted(jwks_endpoint, claim):
    now = int(datetime.now(timezone.utc).timestamp())
    overrides = {
        "iss": {"iss": "https://untrusted.example/v2.0"},
        "aud": {"aud": "OTHER_APP"},
        "exp": {"exp": now - 60},
        "nbf": {"nbf": now + 3600},
        "iat": {"iat": now + 3600},
        "signature": {},
    }
    token, public_key, claims = create_mock_jwt(**overrides[claim])
    if claim == "signature":
        other_token, public_key, other_claims = create_mock_jwt()
    expected_requests = 0 if claim in {"iss", "aud"} else 1
    for request in range(expected_requests):
        jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    with pytest.raises(AuthError) as exc_info:
        await helper.validate_access_token(token)
    assert exc_info.value.status_code == 401
    assert len(jwks_endpoint.requests) == expected_requests


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [jwt.InvalidAudienceError, jwt.InvalidIssuerError])
async def test_verified_claim_failures_remain_auth_errors(monkeypatch, jwks_endpoint, error_type):
    token, public_key, claims = create_mock_jwt()
    jwks_endpoint.respond(create_mock_jwks(public_key))
    original_decode = jwt.decode

    def decode(token, *args, **kwargs):
        if kwargs.get("options", {}).get("verify_signature") is False:
            return original_decode(token, *args, **kwargs)
        raise error_type()

    monkeypatch.setattr(jwt, "decode", decode)
    helper = create_authentication_helper()
    with pytest.raises(AuthError, match="Incorrect claims") as exc_info:
        await helper.validate_access_token(token)
    assert exc_info.value.status_code == 401
    assert len(jwks_endpoint.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", ["HS256", "none"])
async def test_other_algorithms_are_rejected_before_fetching_keys(jwks_endpoint, algorithm):
    token, public_key, claims = create_mock_jwt()
    token = jwt.encode(
        claims, "test-key" * 4 if algorithm == "HS256" else None, algorithm=algorithm, headers={"kid": "mock_kid"}
    )
    helper = create_authentication_helper()

    with pytest.raises(AuthError) as exc_info:
        await helper.validate_access_token(token)
    assert exc_info.value.status_code == 401
    assert not jwks_endpoint.requests


@pytest.mark.asyncio
async def test_missing_key_id_is_rejected_before_fetching_keys(jwks_endpoint):
    token, public_key, claims = create_mock_jwt(kid="")
    helper = create_authentication_helper()
    with pytest.raises(AuthError) as exc_info:
        await helper.validate_access_token(token)
    assert exc_info.value.status_code == 401
    assert not jwks_endpoint.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "claims"),
    [
        ({"id_token_mode": True}, {"aud": "SERVER_APP"}),
        (B2C_OPTIONS, {"iss": B2C_ISSUER, "aud": "SERVER_APP"}),
        (B2C_OPTIONS, {"iss": "https://external.b2clogin.com/OTHER_TENANT/v2.0/", "aud": "B2C_CLIENT"}),
    ],
)
async def test_provider_audiences_and_issuers_are_not_interchangeable(jwks_endpoint, options, claims):
    token, public_key, payload = create_mock_jwt(**claims)
    helper = create_authentication_helper(**options)

    with pytest.raises(AuthError) as exc_info:
        await helper.validate_access_token(token)
    assert exc_info.value.status_code == 401
    assert not jwks_endpoint.requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audience", "id_token_mode"), [("SERVER_APP", False), ("CLIENT_APP", False), ("CLIENT_APP", True)]
)
async def test_aad_access_and_admin_id_tokens_use_incoming_claims(jwks_endpoint, audience, id_token_mode):
    token, public_key, claims = create_mock_jwt(
        aud=audience,
        oid=None,
        sub="verified-subject",
        email="verified@example.com",
        scp="access_as_user" if audience == "SERVER_APP" else None,
    )
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper(id_token_mode=id_token_mode)

    assert await helper.get_auth_claims_if_enabled({"x-ms-token-aad-access-token": token}) == {
        "oid": claims["sub"],
        "email": claims["email"],
        "name": claims["name"],
        "auth_provider": "aad",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("audience", ["SERVER_APP", "api://SERVER_APP"])
@pytest.mark.parametrize("idtyp", [None, "app"])
@pytest.mark.parametrize("allow_anonymous", [False, True])
async def test_aad_app_only_tokens_do_not_resolve_as_users(jwks_endpoint, audience, idtyp, allow_anonymous):
    identity_type_claim = {} if idtyp is None else {"idtyp": idtyp}
    token, public_key, claims = create_mock_jwt(
        aud=audience,
        oid="APPLICATION_PRINCIPAL",
        sub="APPLICATION_PRINCIPAL",
        scp=None,
        roles=["access_as_user"],
        azp="SERVICE_APP",
        appid="SERVICE_APP",
        **identity_type_claim,
    )
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper(enable_unauthenticated_access=allow_anonymous)

    with pytest.raises(AuthError) as exc_info:
        await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {token}"})
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("audience", "id_token_mode"),
    [("SERVER_APP", False), ("api://SERVER_APP", False), ("CLIENT_APP", False), ("CLIENT_APP", True)],
)
async def test_explicit_app_principals_are_rejected_even_with_a_user_scope(jwks_endpoint, audience, id_token_mode):
    token, public_key, claims = create_mock_jwt(aud=audience, scp="access_as_user", idtyp="app")
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper(id_token_mode=id_token_mode)

    with pytest.raises(AuthError) as exc_info:
        await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {token}"})
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scopes",
    [
        None,
        "",
        " ",
        [],
        ["access_as_user"],
        {"access_as_user": True},
        123,
        "openid profile",
        "access_as_user_extra",
        "prefix_access_as_user",
    ],
)
async def test_aad_access_tokens_require_the_delegated_user_scope(jwks_endpoint, scopes):
    token, public_key, claims = create_mock_jwt(scp=scopes)
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    with pytest.raises(AuthError, match="delegated access_as_user user token") as exc_info:
        await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {token}"})
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [
        {"oid": "", "sub": ""},
        {"oid": None, "sub": ""},
        {"oid": " ", "sub": "valid-subject"},
        {"oid": 123, "sub": "valid-subject"},
        {"oid": {"id": "user"}, "sub": "valid-subject"},
        {"oid": [], "sub": ""},
    ],
)
async def test_aad_access_tokens_require_a_usable_user_identity(jwks_endpoint, identity):
    token, public_key, claims = create_mock_jwt(**identity)
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    with pytest.raises(AuthError, match="missing a user identity") as exc_info:
        await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {token}"})
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"scp": "access_as_user", "idtyp": "user"},
        {"scp": "profile access_as_user", "roles": ["user-role"]},
        {"iss": "https://sts.windows.net/TENANT_ID/", "aud": "api://SERVER_APP", "scp": "access_as_user"},
        {
            "scp": "access_as_user profile",
            "azp": "DELEGATING_APP",
            "appid": "DELEGATING_APP",
            "azpacr": "1",
            "appidacr": "1",
        },
    ],
)
async def test_delegated_aad_user_tokens_remain_supported(jwks_endpoint, overrides):
    token, public_key, claims = create_mock_jwt(**overrides)
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper()

    assert await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {token}"}) == {
        "oid": claims["oid"],
        "email": claims["preferred_username"],
        "name": claims["name"],
        "auth_provider": "aad",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("id_token_mode", [False, True])
async def test_b2c_and_aad_admin_tokens_use_separate_provider_caches(jwks_endpoint, id_token_mode):
    aad_token, aad_key, aad_claims = create_mock_jwt(aud="CLIENT_APP", scp=None)
    b2c_token, b2c_key, b2c_claims = create_mock_jwt(
        iss=B2C_ISSUER, aud="B2C_CLIENT", oid=None, sub="external-subject", emails=["external@example.com"], scp=None
    )
    jwks_endpoint.respond(create_mock_jwks(aad_key))
    jwks_endpoint.respond(create_mock_jwks(b2c_key))
    helper = create_authentication_helper(id_token_mode=id_token_mode, **B2C_OPTIONS)

    for request in range(2):
        aad_result = await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {aad_token}"})
        b2c_result = await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {b2c_token}"})
        assert aad_result["oid"] == aad_claims["oid"]
        assert aad_result["auth_provider"] == "aad"
        assert b2c_result == {
            "oid": b2c_claims["sub"],
            "email": "external@example.com",
            "name": b2c_claims["name"],
            "auth_provider": "b2c",
        }
    assert jwks_endpoint.requests == [helper.key_url, helper.b2c_key_url]
    assert len(helper.jwks_cache) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "email"),
    [
        ({"emails": ["external@example.com"], "email": "other@example.com"}, "external@example.com"),
        ({"emails": [], "email": "fallback@example.com"}, "fallback@example.com"),
        ({"emails": []}, "john.doe@example.com"),
    ],
)
async def test_b2c_email_fallbacks(jwks_endpoint, overrides, email):
    token, public_key, claims = create_mock_jwt(iss=B2C_ISSUER, aud="B2C_CLIENT", scp=None, **overrides)
    jwks_endpoint.respond(create_mock_jwks(public_key))
    helper = create_authentication_helper(**B2C_OPTIONS)

    result = await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {token}"})
    assert result["email"] == email
    assert result["auth_provider"] == "b2c"
    assert await helper.validate_access_token(token) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_anonymous", [False, True])
@pytest.mark.parametrize("credentials", ["missing", "malformed", "network_failure"])
async def test_missing_auth_is_public_but_supplied_invalid_auth_always_fails(
    jwks_endpoint, allow_anonymous, credentials
):
    helper = create_authentication_helper(enforce_access_control=True, enable_unauthenticated_access=allow_anonymous)
    headers = {}
    if credentials == "malformed":
        headers = {"Authorization": "Bearer invalid"}
    elif credentials == "network_failure":
        token, public_key, claims = create_mock_jwt()
        headers = {"Authorization": f"Bearer {token}"}
        for attempt in range(authentication.JWKS_FETCH_ATTEMPTS):
            jwks_endpoint.respond({}, status=503)

    if credentials == "missing":
        assert await helper.get_auth_claims_if_enabled(headers) == {}
    else:
        with pytest.raises(AuthError) as exc_info:
            await helper.get_auth_claims_if_enabled(headers)
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_anonymous", [False, True])
async def test_unexpected_validation_failure_never_becomes_anonymous(monkeypatch, allow_anonymous):
    helper = create_authentication_helper(enable_unauthenticated_access=allow_anonymous)

    async def fail_validation(token):
        raise RuntimeError("Validation unavailable")

    def reject_claims(*args, **kwargs):
        pytest.fail("Claims must not be extracted after validation fails")

    monkeypatch.setattr(helper, "validate_access_token", fail_validation)
    monkeypatch.setattr(jwt, "decode", reject_claims)
    with pytest.raises(AuthError, match="Unable to validate authorization information") as error:
        await helper.get_auth_claims_if_enabled({"Authorization": "Bearer token"})
    assert isinstance(error.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_disabled_authentication_rejects_supplied_tokens_without_validation(monkeypatch, jwks_endpoint):
    helper = create_authentication_helper(use_authentication=False, enforce_access_control=True)

    async def reject_validation(token):
        pytest.fail("Disabled authentication must not validate a token")

    monkeypatch.setattr(helper, "validate_access_token", reject_validation)
    assert await helper.get_auth_claims_if_enabled({}) == {}
    with pytest.raises(AuthError):
        await helper.get_auth_claims_if_enabled({"Authorization": "Bearer invalid"})
    assert not helper.enforce_access_control
    assert helper.enable_unauthenticated_access
    assert not jwks_endpoint.requests


@pytest.mark.parametrize("id_token_mode", [False, True])
def test_legacy_b2c_configuration_does_not_enable_product_login(id_token_mode):
    helper = create_authentication_helper(id_token_mode=id_token_mode, **B2C_OPTIONS)
    client = helper.get_auth_setup_for_client()
    admin = helper.get_auth_setup_for_admin()

    assert client == PUBLIC_AUTH_SETUP
    assert admin == {"enabled": False}


@pytest.mark.parametrize("missing", [None, *OWNER_ENVIRONMENT])
@pytest.mark.parametrize("missing_value", ["", " "])
def test_owner_factory_requires_complete_configuration(missing, missing_value):
    environment = dict(OWNER_ENVIRONMENT)
    if missing:
        environment[missing] = missing_value
    helper = create_owner_authentication_helper(environment)

    assert helper.get_auth_setup_for_client() == PUBLIC_AUTH_SETUP
    assert helper.enable_unauthenticated_access
    assert not helper.enforce_access_control
    assert not helper.b2c_enabled
    assert helper.server_app_id is None
    assert helper.server_app_secret is None
    assert helper.use_authentication is (missing is None)
    if missing:
        assert helper.get_auth_setup_for_admin() == {"enabled": False}
    else:
        admin = helper.get_auth_setup_for_admin()
        assert admin["enabled"] is True
        assert admin["useIdToken"] is True
        assert admin["msalConfig"]["auth"]["clientId"] == "CLIENT_APP"
        assert admin["msalConfig"]["auth"]["authority"] == "https://login.microsoftonline.com/TENANT_ID"
        assert admin["msalConfig"]["cache"]["cacheLocation"] == "sessionStorage"
        assert admin["loginRequest"]["scopes"] == ["openid", "profile", "email"]
        assert admin["tokenRequest"]["scopes"] == ["openid", "profile", "email"]
        assert helper.valid_audiences == ["CLIENT_APP"]


def test_owner_factory_uses_legacy_tenant_fallback_but_ignores_removed_modes():
    environment = {
        "AZURE_AUTH_TENANT_ID": "TENANT_ID",
        "AZURE_CLIENT_APP_ID": "CLIENT_APP",
        "ADMIN_USER_OID": "OID_X",
        "AZURE_USE_AUTHENTICATION": "false",
        "AZURE_AUTH_ID_TOKEN_MODE": "false",
        "AZURE_ENFORCE_ACCESS_CONTROL": "true",
        "AZURE_ENABLE_UNAUTHENTICATED_ACCESS": "false",
        "AZURE_SERVER_APP_ID": "SERVER_APP",
        "AZURE_SERVER_APP_SECRET": "unused-secret",
        "AZURE_B2C_TENANT_NAME": "external",
        "AZURE_B2C_CLIENT_APP_ID": "B2C_CLIENT",
        "AZURE_B2C_POLICY": "B2C_1_signin",
    }
    helper = create_owner_authentication_helper(environment)
    assert helper.owner_authentication_enabled
    assert helper.id_token_mode
    assert helper.get_auth_setup_for_client() == PUBLIC_AUTH_SETUP
    assert not helper.b2c_enabled
    assert helper.server_app_id is None
    assert helper.server_app_secret is None

    environment["AZURE_TENANT_ID"] = "PRIMARY_TENANT"
    helper = create_owner_authentication_helper(environment)
    assert helper.tenant_id == "PRIMARY_TENANT"


@pytest.mark.parametrize(
    "tenant",
    ["common", "organizations", "consumers", "COMMON", "https://example.com", "tenant/path", "tenant#fragment", " "],
)
def test_owner_factory_disables_unsupported_tenant_authorities(tenant):
    helper = create_owner_authentication_helper({**OWNER_ENVIRONMENT, "AZURE_TENANT_ID": tenant})
    assert helper.get_auth_setup_for_admin() == {"enabled": False}
    assert not helper.use_authentication
    assert helper.get_auth_setup_for_client() == PUBLIC_AUTH_SETUP


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": ""},
        {"Authorization": " "},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic credentials"},
        {"Authorization": "Bearer one two"},
        {"Authorization": "", "x-ms-token-aad-access-token": "fallback-must-not-be-used"},
        {"x-ms-token-aad-access-token": ""},
        Headers([("Authorization", "Bearer one"), ("Authorization", "Bearer two")]),
    ],
)
def test_explicit_malformed_headers_cannot_fall_back_to_anonymous_or_other_headers(headers):
    with pytest.raises(AuthError) as error:
        AuthenticationHelper.get_token_auth_header(headers)
    assert error.value.status_code == 401


def test_authorization_header_names_are_case_insensitive():
    assert AuthenticationHelper.get_token_auth_header({"authorization": "Bearer synthetic-token"}) == "synthetic-token"


def create_access_test_app(environment, admin_oid=None):
    app = Quart(__name__)
    app.config[CONFIG_AUTH_CLIENT] = create_owner_authentication_helper(environment)
    app.config[CONFIG_ADMIN_USER_OID] = environment.get("ADMIN_USER_OID") if admin_oid is None else admin_oid

    @app.get("/public")
    @authenticated
    async def public_route(auth_claims):
        return auth_claims

    @app.get("/owner")
    @admin_required
    async def owner_route(auth_claims):
        return auth_claims

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_configured", [False, True])
async def test_public_decorator_allows_absent_auth_but_owner_decorator_does_not(jwks_endpoint, owner_configured):
    app = create_access_test_app(OWNER_ENVIRONMENT if owner_configured else {})
    public_response = await app.test_client().get("/public")
    assert public_response.status_code == 200
    assert await public_response.get_json() == {}
    owner_response = await app.test_client().get("/owner")
    assert owner_response.status_code == 403
    assert not jwks_endpoint.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", list(OWNER_ENVIRONMENT))
async def test_partial_owner_configuration_cannot_authorize_even_a_signed_owner_token(jwks_endpoint, missing):
    environment = dict(OWNER_ENVIRONMENT)
    environment.pop(missing)
    token, public_key, claims = create_mock_jwt(aud="CLIENT_APP", tid="TENANT_ID", nonce="interactive-nonce", scp=None)
    app = create_access_test_app(environment)
    for route in ["/public", "/owner"]:
        response = await app.test_client().get(route, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 403
    response = await app.test_client().get("/public")
    assert response.status_code == 200
    assert not jwks_endpoint.requests


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_configured", [False, True])
@pytest.mark.parametrize("authorization", ["", " ", "Bearer", "Basic credentials", "Bearer invalid", "Bearer one two"])
async def test_public_and_owner_decorators_reject_explicit_invalid_auth(jwks_endpoint, owner_configured, authorization):
    app = create_access_test_app(OWNER_ENVIRONMENT if owner_configured else {})
    for route in ["/public", "/owner"]:
        response = await app.test_client().get(route, headers={"Authorization": authorization})
        assert response.status_code == 403
    assert not jwks_endpoint.requests


@pytest.mark.asyncio
async def test_signed_owner_id_token_authenticates_without_server_secret_or_downstream_exchange(jwks_endpoint):
    token, public_key, claims = create_mock_jwt(aud="CLIENT_APP", tid="TENANT_ID", nonce="interactive-nonce", scp=None)
    jwks_endpoint.respond(create_mock_jwks(public_key))
    app = create_access_test_app(OWNER_ENVIRONMENT)

    for route in ["/public", "/owner"]:
        response = await app.test_client().get(route, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        result = await response.get_json()
        assert result["oid"] == claims["oid"]
        assert result["auth_provider"] == "aad"
    helper = app.config[CONFIG_AUTH_CLIENT]
    assert jwks_endpoint.requests == [helper.key_url]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "admin_oid",
    ["OTHER_OWNER", "", " ", 123, ["OID_X"], {"oid": "OID_X"}],
)
async def test_owner_decorator_requires_an_exact_nonempty_configured_oid(jwks_endpoint, admin_oid):
    token, public_key, claims = create_mock_jwt(aud="CLIENT_APP", tid="TENANT_ID", nonce="interactive-nonce", scp=None)
    jwks_endpoint.respond(create_mock_jwks(public_key))
    app = create_access_test_app(OWNER_ENVIRONMENT, admin_oid=admin_oid)
    response = await app.test_client().get("/owner", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"oid": "OTHER_USER"},
        {"oid": None, "sub": "OID_X"},
        {"oid": "", "sub": "OID_X"},
        {"tid": "OTHER_TENANT"},
        {"iss": "https://login.microsoftonline.com/OTHER_TENANT/v2.0", "tid": "OTHER_TENANT"},
        {"aud": "OTHER_CLIENT"},
        {"aud": "SERVER_APP", "scp": "access_as_user"},
        {"iss": B2C_ISSUER, "aud": "B2C_CLIENT"},
        {"idtyp": "app"},
        {"idtyp": "service"},
        {"nonce": None, "roles": ["admin"], "azp": "DEPLOYMENT_PRINCIPAL", "oid": "OID_X"},
        {"nonce": ""},
        {"sub": ""},
        {"scp": "access_as_user"},
        {"exp": int((datetime.now(timezone.utc) - timedelta(minutes=1)).timestamp())},
    ],
)
async def test_owner_routes_reject_other_users_tenants_apps_and_noninteractive_tokens(jwks_endpoint, overrides):
    token, public_key, claims = create_mock_jwt(
        **{"aud": "CLIENT_APP", "tid": "TENANT_ID", "nonce": "interactive-nonce", "scp": None, **overrides}
    )
    jwks_endpoint.respond(create_mock_jwks(public_key))
    app = create_access_test_app(OWNER_ENVIRONMENT)
    response = await app.test_client().get("/owner", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["exp", "iat", "nbf", "sub", "tid", "oid", "nonce"])
async def test_owner_id_tokens_require_standard_identity_and_lifetime_claims(jwks_endpoint, claim):
    token, public_key, claims = create_mock_jwt(aud="CLIENT_APP", tid="TENANT_ID", nonce="interactive-nonce", scp=None)
    claims.pop(claim)
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "mock_kid"})
    jwks_endpoint.respond(create_mock_jwks(private_key.public_key()))
    helper = create_owner_authentication_helper(OWNER_ENVIRONMENT)
    with pytest.raises(AuthError):
        await helper.get_auth_claims_if_enabled({"Authorization": f"Bearer {token}"})
