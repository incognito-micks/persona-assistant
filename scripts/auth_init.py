import asyncio
import os
import subprocess
import uuid

from azure.core.exceptions import ClientAuthenticationError
from azure.identity.aio import AzureDeveloperCliCredential
from kiota_abstractions.api_error import APIError
from msgraph import GraphServiceClient
from msgraph.generated.models.application import Application
from msgraph.generated.models.spa_application import SpaApplication

from auth_common import (
    MANAGED_CLIENT_APP_ID,
    owner_redirect_uris,
    owner_tenant_id,
    print_manual_setup,
)
from load_azd_env import load_azd_env


def owner_app_initial(identifier: str) -> Application:
    return Application(
        display_name=f"Template Owner Admin {identifier}",
        sign_in_audience="AzureADMyOrg",
        spa=SpaApplication(redirect_uris=owner_redirect_uris()),
        required_resource_access=[],
    )


async def create_application(graph_client: GraphServiceClient, request_app: Application) -> tuple[str, str]:
    app = await graph_client.applications.post(request_app)
    if app is None or not app.id or not app.app_id:
        raise ValueError("Created owner application has no object ID or client ID.")
    return app.id, app.app_id


async def get_delegated_owner_oid(graph_client: GraphServiceClient) -> str:
    # Graph /me only accepts a delegated human context, never an application-only deployment token.
    user = await graph_client.me.get()
    if user is None or not isinstance(user.id, str) or not user.id.strip():
        raise ValueError("Unable to identify a signed-in human owner. Set ADMIN_USER_OID explicitly.")
    return user.id


def update_azd_env(name: str, value: str) -> None:
    subprocess.run(["azd", "env", "set", name, value], check=True)


async def main() -> None:
    load_azd_env()
    client_app_id = os.getenv("AZURE_CLIENT_APP_ID", "").strip()
    owner_oid = os.getenv("ADMIN_USER_OID", "").strip()
    if client_app_id:
        print("Owner SPA registration already configured; not changing the existing application.")
        print_manual_setup(client_app_id, os.getenv("BACKEND_URI"))
        return

    tenant_id = owner_tenant_id(os.environ)
    if not tenant_id:
        print("No owner tenant is configured; skipping automatic owner registration.")
        print_manual_setup()
        return

    async with AzureDeveloperCliCredential(tenant_id=tenant_id) as credential:
        graph_client = GraphServiceClient(credentials=credential, scopes=["https://graph.microsoft.com/.default"])
        try:
            if not owner_oid:
                owner_oid = await get_delegated_owner_oid(graph_client)
            _, client_app_id = await create_application(graph_client, owner_app_initial(uuid.uuid4().hex[:8]))
        except APIError as error:
            if error.response_status_code not in {400, 401, 403}:
                raise
            print("Automatic owner registration is unavailable with this identity or directory permissions.")
            print("Use a delegated human login or ask your tenant administrator to complete the manual setup.")
            print_manual_setup()
            return
        except ClientAuthenticationError:
            print("Azure Developer CLI authentication is unavailable. Sign in as the human owner or use manual setup.")
            print_manual_setup()
            return
        except ValueError:
            print("A human owner could not be identified; no owner application was configured.")
            print_manual_setup()
            return

    update_azd_env("AZURE_CLIENT_APP_ID", client_app_id)
    update_azd_env(MANAGED_CLIENT_APP_ID, client_app_id)
    update_azd_env("ADMIN_USER_OID", owner_oid)
    print("Single-tenant owner SPA configured without a server application, client secret, or API permissions.")
    print("Public chat and realtime voice remain anonymous.")


if __name__ == "__main__":
    asyncio.run(main())
