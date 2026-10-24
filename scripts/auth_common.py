from collections.abc import Mapping
from typing import Optional
from urllib.parse import urlsplit

from kiota_abstractions.api_error import APIError
from msgraph import GraphServiceClient
from msgraph.generated.models.application import Application

LOCAL_REDIRECT_URIS = ["http://localhost:50505/redirect", "http://localhost:5173/redirect"]
MANAGED_CLIENT_APP_ID = "AZURE_AUTH_MANAGED_CLIENT_APP_ID"


def owner_tenant_id(environ: Mapping[str, str]) -> str:
    tenant_id = environ.get("AZURE_TENANT_ID", "").strip() or environ.get("AZURE_AUTH_TENANT_ID", "").strip()
    if tenant_id.lower() in {"common", "organizations", "consumers"} or any(
        character.isspace() or character in "/\\:?#" for character in tenant_id
    ):
        raise ValueError("Owner authentication requires a single Entra tenant ID.")
    return tenant_id


def owner_redirect_uris(backend_uri: Optional[str] = None) -> list[str]:
    redirects = list(LOCAL_REDIRECT_URIS)
    if backend_uri:
        origin = backend_uri.rstrip("/")
        parsed = urlsplit(origin)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path
            or (
                parsed.scheme != "https"
                and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        ):
            raise ValueError("BACKEND_URI must be an HTTPS origin (HTTP is allowed only for localhost).")
        redirect = f"{origin}/redirect"
        if redirect not in redirects:
            redirects.append(redirect)
    return redirects


async def get_application(graph_client: GraphServiceClient, client_id: str) -> Optional[Application]:
    try:
        return await graph_client.applications_with_app_id(client_id).get()
    except APIError as error:
        if error.response_status_code == 404:
            return None
        raise


def print_manual_setup(client_app_id: Optional[str] = None, backend_uri: Optional[str] = None) -> None:
    print("Public chat stays anonymous. Owner administration requires all three azd/runtime values:")
    print("  AZURE_TENANT_ID: the owner's single Entra tenant ID (AZURE_AUTH_TENANT_ID is a legacy fallback).")
    print("  AZURE_CLIENT_APP_ID: one single-tenant SPA app registration using authorization code + PKCE.")
    print("  ADMIN_USER_OID: the human owner's object ID in that tenant, never a deployment service principal.")
    print("Set each value with `azd env set <NAME> <VALUE>` and pass the same values to the backend runtime.")
    print("Without these values, owner administration remains disabled. No guest accounts are needed.")
    if client_app_id:
        print(f"Existing application {client_app_id} is preserved; configure it manually in Entra App registrations.")
    print("Add these Single-page application redirect URIs (not Web redirect URIs):")
    for redirect in owner_redirect_uris(backend_uri):
        print(f"  {redirect}")
    if not backend_uri:
        print("  https://<your-deployed-origin>/redirect")
    print("Do not add a client secret, Search permissions, exposed API scopes, or broad Graph/admin consent.")
