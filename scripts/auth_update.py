import asyncio
import os

from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureDeveloperCliCredential
from kiota_abstractions.api_error import APIError
from msgraph import GraphServiceClient
from msgraph.generated.models.application import Application
from msgraph.generated.models.spa_application import SpaApplication

from auth_common import (
    MANAGED_CLIENT_APP_ID,
    get_application,
    owner_redirect_uris,
    owner_tenant_id,
    print_manual_setup,
)
from load_azd_env import load_azd_env


def owner_redirect_update(existing_app: Application, backend_uri: str) -> Application:
    existing_redirects = existing_app.spa.redirect_uris if existing_app.spa else None
    redirects = list(dict.fromkeys([*(existing_redirects or []), *owner_redirect_uris(backend_uri)]))
    return Application(spa=SpaApplication(redirect_uris=redirects))


async def main() -> None:
    load_azd_env()
    client_app_id = os.getenv("AZURE_CLIENT_APP_ID", "").strip()
    if not client_app_id:
        print("No owner SPA is configured; skipping redirect update. Public chat remains anonymous.")
        return

    backend_uri = os.getenv("BACKEND_URI", "").strip()
    if backend_uri:
        owner_redirect_uris(backend_uri)
    if os.getenv(MANAGED_CLIENT_APP_ID, "").strip() != client_app_id or not backend_uri:
        print("Not changing an externally supplied SPA or guessing a deployed redirect origin.")
        print_manual_setup(client_app_id, backend_uri or None)
        return

    tenant_id = owner_tenant_id(os.environ)
    if not tenant_id:
        print("No owner tenant is configured; skipping redirect update.")
        print_manual_setup(client_app_id, backend_uri)
        return

    async with AzureDeveloperCliCredential(tenant_id=tenant_id) as credential:
        graph_client = GraphServiceClient(credentials=credential, scopes=["https://graph.microsoft.com/.default"])
        try:
            app = await get_application(graph_client, client_app_id)
            if app is None or not app.id:
                print("Managed owner SPA was not found; no replacement application was created.")
                print_manual_setup(client_app_id, backend_uri)
                return
            await graph_client.applications.by_application_id(app.id).patch(owner_redirect_update(app, backend_uri))
        except APIError as error:
            if error.response_status_code not in {401, 403}:
                raise
            print("Directory permissions do not allow an automatic redirect update.")
            print_manual_setup(client_app_id, backend_uri)
            return
        except ClientAuthenticationError:
            print("Azure Developer CLI authentication is unavailable; add the SPA redirect URI manually.")
            print_manual_setup(client_app_id, backend_uri)
            return
    print(f"Updated only the managed owner SPA redirect URIs for {client_app_id}.")


if __name__ == "__main__":
    asyncio.run(main())
