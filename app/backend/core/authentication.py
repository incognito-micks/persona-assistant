import asyncio
import base64
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Optional

import aiohttp
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.types import Options

JWKS_CACHE_TTL_SECONDS = 300
JWKS_REFRESH_COOLDOWN_SECONDS = 30
JWKS_REQUEST_TIMEOUT_SECONDS = 2
JWKS_CONNECT_TIMEOUT_SECONDS = 1
JWKS_FETCH_ATTEMPTS = 2
JWKS_RETRY_DELAY_SECONDS = 0.1


@dataclass
class JwksCacheEntry:
    jwks: Optional[dict[str, Any]] = None
    expires_at: float = 0
    next_forced_refresh_at: float = 0
    retry_after: float = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AuthError(Exception):
    def __init__(self, error, status_code):
        self.error = error
        self.status_code = status_code

    def __str__(self) -> str:
        return self.error or ""


class AuthenticationHelper:
    # Legacy caller compatibility only; claims resolution does not exchange downstream tokens.
    scope: str = "https://search.azure.com/.default"

    def __init__(
        self,
        use_authentication: bool,
        server_app_id: Optional[str],
        server_app_secret: Optional[str],
        client_app_id: Optional[str],
        tenant_id: Optional[str],
        enforce_access_control: bool = False,
        enable_unauthenticated_access: bool = False,
        id_token_mode: bool = False,
        b2c_tenant_name: Optional[str] = None,
        b2c_tenant_id: Optional[str] = None,
        b2c_client_app_id: Optional[str] = None,
        b2c_policy: Optional[str] = None,
        owner_user_oid: Optional[str] = None,
    ):
        self.use_authentication = use_authentication
        self.server_app_id = server_app_id
        self.server_app_secret = server_app_secret
        self.client_app_id = client_app_id
        self.tenant_id = tenant_id
        self.id_token_mode = id_token_mode
        self.owner_user_oid = owner_user_oid
        self.owner_authentication_enabled = bool(
            use_authentication and id_token_mode and tenant_id and client_app_id and owner_user_oid
        )
        self.authority = f"https://login.microsoftonline.com/{tenant_id}"
        # Depending on if requestedAccessTokenVersion is 1 or 2, the issuer and audience of the token may be different
        # See https://learn.microsoft.com/graph/api/resources/apiapplication
        self.valid_issuers = [
            f"https://sts.windows.net/{tenant_id}/",
            f"https://login.microsoftonline.com/{tenant_id}/v2.0",
        ]
        if self.id_token_mode:
            # In ID token mode, the audience is the client app ID (SPA app registration)
            self.valid_audiences = [str(client_app_id)]
        else:
            # Include client_app_id for admin ID token auth alongside server app audiences
            self.valid_audiences = [f"api://{server_app_id}", str(server_app_id), str(client_app_id)]
        # See https://learn.microsoft.com/entra/identity-platform/access-tokens#validate-the-issuer for more information on token validation
        self.key_url = f"{self.authority}/discovery/v2.0/keys"
        self.jwks_cache = {self.key_url: JwksCacheEntry()}

        # Azure AD B2C configuration for external user authentication
        self.b2c_enabled = bool(b2c_tenant_name and b2c_client_app_id and b2c_policy)
        if self.b2c_enabled:
            self.b2c_tenant_name = b2c_tenant_name
            self.b2c_authority = (
                f"https://{b2c_tenant_name}.b2clogin.com/{b2c_tenant_name}.onmicrosoft.com/{b2c_policy}"
            )
            self.b2c_key_url = f"{self.b2c_authority}/discovery/v2.0/keys"
            self.jwks_cache[self.b2c_key_url] = JwksCacheEntry()
            self.b2c_valid_issuers = [
                f"https://{b2c_tenant_name}.b2clogin.com/{b2c_tenant_id}/v2.0/",
            ]
            self.b2c_valid_audiences = [str(b2c_client_app_id)]
            self.b2c_client_app_id = b2c_client_app_id

        if self.use_authentication:
            self.enforce_access_control = enforce_access_control
            self.enable_unauthenticated_access = enable_unauthenticated_access
        else:
            self.enforce_access_control = False
            self.enable_unauthenticated_access = True

    def get_auth_setup_for_client(self) -> dict[str, Any]:
        return {
            "useLogin": False,
            "requireAccessControl": False,
            "enableUnauthenticatedAccess": True,
        }

    def get_auth_setup_for_admin(self) -> dict[str, Any]:
        """Expose owner login only when a complete, supported owner configuration exists."""
        if not self.owner_authentication_enabled:
            return {"enabled": False}
        return {
            "enabled": True,
            "useLogin": True,
            "useIdToken": True,
            "msalConfig": {
                "auth": {
                    "clientId": self.client_app_id,
                    "authority": self.authority,
                    "redirectUri": "/redirect",
                    "postLogoutRedirectUri": "/",
                    "navigateToLoginRequestUrl": False,
                },
                "cache": {
                    "cacheLocation": "sessionStorage",
                    "storeAuthStateInCookie": False,
                },
            },
            "loginRequest": {
                "scopes": ["openid", "profile", "email"],
            },
            "tokenRequest": {
                "scopes": ["openid", "profile", "email"],
            },
        }

    @staticmethod
    def get_token_auth_header(headers: Mapping[str, str]) -> str:
        authorization = [value for name, value in headers.items() if name.lower() == "authorization"]
        if authorization:
            if len(authorization) != 1 or not isinstance(authorization[0], str):
                raise AuthError("Exactly one Authorization header is required.", 401)
            auth = authorization[0]
            parts = auth.split()

            if not parts or parts[0].lower() != "bearer":
                raise AuthError(error="Authorization header must start with Bearer", status_code=401)
            elif len(parts) == 1:
                raise AuthError(error="Token not found", status_code=401)
            elif len(parts) > 2:
                raise AuthError(error="Authorization header must be Bearer token", status_code=401)

            token = parts[1]
            return token

        # App services built-in authentication passes the access token directly as a header
        # To learn more, please visit https://learn.microsoft.com/azure/app-service/configure-authentication-oauth-tokens
        tokens = [value for name, value in headers.items() if name.lower() == "x-ms-token-aad-access-token"]
        if len(tokens) == 1 and isinstance(tokens[0], str) and tokens[0].strip():
            return tokens[0]

        raise AuthError(error="Authorization header is expected", status_code=401)

    async def get_auth_claims_if_enabled(self, headers: Mapping[str, str]) -> dict[str, Any]:
        supplied_credentials = any(
            name.lower() in {"authorization", "x-ms-token-aad-access-token"} for name in headers.keys()
        )
        if not supplied_credentials:
            return {}
        try:
            auth_token = AuthenticationHelper.get_token_auth_header(headers)
            if not self.use_authentication:
                raise AuthError("Owner authentication is not configured.", 401)
            is_b2c = await self.validate_access_token(auth_token)
            # The same immutable token has already passed signature and claims validation.
            claims = jwt.decode(auth_token, options={"verify_signature": False})
            user_oid = claims.get("oid") or claims.get("sub", "")
            if not is_b2c:
                if claims.get("idtyp") == "app":
                    raise AuthError("An application-only token cannot authenticate a user.", 401)
                if self.owner_authentication_enabled:
                    if claims.get("tid") != self.tenant_id:
                        raise AuthError("Token is not from the configured owner tenant.", 401)
                    if not isinstance(claims.get("oid"), str) or not claims["oid"].strip():
                        raise AuthError("Token is missing an owner object ID.", 401)
                    if (
                        claims.get("idtyp") not in {None, "user"}
                        or "scp" in claims
                        or not isinstance(claims.get("sub"), str)
                        or not claims["sub"].strip()
                        or not isinstance(claims.get("nonce"), str)
                        or not claims["nonce"].strip()
                    ):
                        raise AuthError("An interactive owner ID token is required.", 401)
                    user_oid = claims["oid"]
                if not self.id_token_mode and claims.get("aud") != self.client_app_id:
                    scopes = claims.get("scp")
                    if not isinstance(scopes, str) or "access_as_user" not in scopes.split():
                        raise AuthError("A delegated access_as_user user token is required.", 401)
                    if not isinstance(user_oid, str) or not user_oid.strip():
                        raise AuthError("Token is missing a user identity.", 401)
            email = ""
            if is_b2c:
                emails = claims.get("emails", [])
                email = emails[0] if emails else ""
            if not email:
                email = claims.get("email") or claims.get("preferred_username", "")
            return {
                "oid": user_oid,
                "email": email,
                "name": claims.get("name", ""),
                "auth_provider": "b2c" if is_b2c else "aad",
            }
        except AuthError:
            raise
        except Exception as exc:
            logging.exception("Exception getting authorization information")
            raise AuthError("Unable to validate authorization information.", 401) from exc

    async def fetch_jwks(self, key_url: str) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=JWKS_REQUEST_TIMEOUT_SECONDS, connect=JWKS_CONNECT_TIMEOUT_SECONDS)
        # Sessions are only needed on cache refresh, not on each authenticated request.
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(JWKS_FETCH_ATTEMPTS):
                try:
                    async with session.get(url=key_url, allow_redirects=False) as response:
                        if response.status == 200:
                            jwks = await response.json()
                            if (
                                not isinstance(jwks, dict)
                                or not isinstance(jwks.get("keys"), list)
                                or not jwks["keys"]
                                or not all(isinstance(key, dict) for key in jwks["keys"])
                            ):
                                raise AuthError("Unable to get keys to validate auth token.", 401)
                            return jwks
                        if response.status not in {429, 500, 502, 503, 504}:
                            raise AuthError("Unable to get keys to validate auth token.", 401)
                except (TimeoutError, aiohttp.ClientError, ValueError):
                    pass
                if attempt + 1 < JWKS_FETCH_ATTEMPTS:
                    await asyncio.sleep(JWKS_RETRY_DELAY_SECONDS)
        raise AuthError("Unable to get keys to validate auth token.", 401)

    async def get_jwks(self, key_url: str, previous_jwks: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        cache = self.jwks_cache[key_url]
        async with cache.lock:
            now = monotonic()
            if cache.jwks is not None and now < cache.expires_at:
                if previous_jwks is None or cache.jwks is not previous_jwks:
                    return cache.jwks
                if now < cache.next_forced_refresh_at:
                    return cache.jwks
            if now < cache.retry_after:
                raise AuthError("Unable to get keys to validate auth token.", 401)
            if previous_jwks is not None:
                # Coalesce rotation refreshes and bound refreshes caused by invalid tokens.
                cache.next_forced_refresh_at = now + JWKS_REFRESH_COOLDOWN_SECONDS
            try:
                jwks = await self.fetch_jwks(key_url)
            except AuthError:
                cache.retry_after = monotonic() + JWKS_REFRESH_COOLDOWN_SECONDS
                raise
            cache.jwks = jwks
            cache.expires_at = monotonic() + JWKS_CACHE_TTL_SECONDS
            cache.retry_after = 0
            return jwks

    async def create_pem_format(self, jwks: dict[str, Any], token: str) -> Optional[bytes]:
        unverified_header = jwt.get_unverified_header(token)
        for key in jwks["keys"]:
            if (
                key.get("kid") == unverified_header.get("kid")
                and key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig"
                and key.get("alg", "RS256") == "RS256"
            ):
                try:
                    public_numbers = rsa.RSAPublicNumbers(
                        e=int.from_bytes(base64.urlsafe_b64decode(key["e"] + "=="), byteorder="big"),
                        n=int.from_bytes(base64.urlsafe_b64decode(key["n"] + "=="), byteorder="big"),
                    )
                    public_key = public_numbers.public_key()
                    return public_key.public_bytes(
                        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise AuthError("Unable to parse signing key.", 401) from exc
        return None

    async def validate_access_token(self, token: str) -> bool:
        """
        Validate an access token is issued by Entra ID or Azure AD B2C.
        Returns True if the token is from B2C, False if from AAD.
        """
        # Peek at unverified claims to determine the token source
        try:
            header = jwt.get_unverified_header(token)
            unverified_claims = jwt.decode(token, options={"verify_signature": False})
            issuer = unverified_claims.get("iss")
            audience = unverified_claims.get("aud")
        except jwt.PyJWTError as exc:
            raise AuthError("Unable to parse authorization token.", 401) from exc
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not header["kid"]:
            raise AuthError("Unable to parse authorization token.", 401)

        # Determine which provider this token belongs to
        is_b2c = False
        if self.b2c_enabled and issuer in self.b2c_valid_issuers:
            is_b2c = True
            jwks_url = self.b2c_key_url
            valid_audiences = self.b2c_valid_audiences
        elif issuer in self.valid_issuers:
            jwks_url = self.key_url
            valid_audiences = self.valid_audiences
        else:
            all_issuers = list(self.valid_issuers)
            if self.b2c_enabled:
                all_issuers.extend(self.b2c_valid_issuers)
            raise AuthError(f"Issuer {issuer} not in {','.join(all_issuers)}", 401)

        if audience not in valid_audiences:
            raise AuthError(
                f"Audience {audience} not in {','.join(valid_audiences)}",
                401,
            )

        cached_jwks = self.jwks_cache[jwks_url].jwks
        jwks = await self.get_jwks(jwks_url)
        # Only retry cached keys; a miss or expiry has already fetched the current set.
        can_refresh = jwks is cached_jwks
        for attempt in range(2 if can_refresh else 1):
            rsa_key = await self.create_pem_format(jwks, token)
            if rsa_key:
                try:
                    options: Options = (
                        {"require": ["exp", "iat", "nbf", "sub", "tid", "oid", "nonce"]}
                        if self.owner_authentication_enabled
                        else {}
                    )
                    jwt.decode(token, rsa_key, algorithms=["RS256"], audience=audience, issuer=issuer, options=options)
                    return is_b2c
                except jwt.InvalidSignatureError as exc:
                    if attempt or not can_refresh:
                        raise AuthError("Unable to validate token signature.", 401) from exc
                except jwt.ExpiredSignatureError as exc:
                    raise AuthError("Token is expired", 401) from exc
                except (jwt.InvalidAudienceError, jwt.InvalidIssuerError) as exc:
                    raise AuthError("Incorrect claims: please check the audience and issuer", 401) from exc
                except jwt.PyJWTError as exc:
                    raise AuthError("Unable to parse authorization token.", 401) from exc
            if can_refresh and not attempt:
                jwks = await self.get_jwks(jwks_url, previous_jwks=jwks)
        raise AuthError("Unable to find appropriate key", 401)


def create_owner_authentication_helper(environ: Mapping[str, str]) -> AuthenticationHelper:
    """Configure the one supported owner login without changing anonymous public access."""
    tenant_id = environ.get("AZURE_TENANT_ID", "").strip() or environ.get("AZURE_AUTH_TENANT_ID", "").strip()
    client_app_id = environ.get("AZURE_CLIENT_APP_ID", "").strip()
    owner_user_oid = environ.get("ADMIN_USER_OID", "").strip()
    supported_tenant = bool(
        tenant_id
        and tenant_id.lower() not in {"common", "organizations", "consumers"}
        and not any(character.isspace() or character in "/\\:?#" for character in tenant_id)
    )
    owner_enabled = bool(supported_tenant and client_app_id and owner_user_oid)
    return AuthenticationHelper(
        use_authentication=owner_enabled,
        server_app_id=None,
        server_app_secret=None,
        client_app_id=client_app_id or None,
        tenant_id=tenant_id or None,
        enable_unauthenticated_access=True,
        id_token_mode=True,
        owner_user_oid=owner_user_oid if owner_enabled else None,
    )
