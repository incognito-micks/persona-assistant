import json
from types import SimpleNamespace
from unittest import mock

import pytest
from azure.core.exceptions import ClientAuthenticationError
from kiota_abstractions.api_error import APIError
from msgraph import GraphServiceClient
from msgraph.generated.models.application import Application
from msgraph.generated.models.spa_application import SpaApplication
from msgraph.generated.models.user import User

from .mocks import MockAzureCredential
from scripts import auth_common, auth_init, auth_update

MOCK_OBJECT_ID = "owner-app-object"
MOCK_APP_ID = "owner-app-client"
MOCK_OWNER_ID = "human-owner-object"


def graph_error(status):
    error = APIError()
    error.response_status_code = status
    return error


@pytest.fixture
def graph_client(monkeypatch):
    client = GraphServiceClient(credentials=MockAzureCredential(), scopes=["https://graph.microsoft.com/.default"])
    state = SimpleNamespace(
        client=client,
        calls=[],
        created=Application(id=MOCK_OBJECT_ID, app_id=MOCK_APP_ID),
        existing=Application(
            id=MOCK_OBJECT_ID,
            app_id=MOCK_APP_ID,
            spa=SpaApplication(redirect_uris=["https://existing.example/redirect"]),
        ),
        user=User(id=MOCK_OWNER_ID),
        errors={},
    )

    def respond(request_info):
        method = request_info.http_method.value
        # The Graph adapter rewrites this generated SDK placeholder to the delegated /me endpoint.
        url = (request_info.url or "").replace("/users/me-token-to-replace", "/me")
        body = json.loads(request_info.content) if request_info.content else None
        state.calls.append(SimpleNamespace(method=method, url=url, body=body))
        if method == "POST" and url.endswith("/applications"):
            operation, response = "create", state.created
        elif method == "GET" and "/applications(appId=" in url:
            operation, response = "lookup", state.existing
        elif method == "GET" and url.endswith("/me"):
            operation, response = "me", state.user
        elif method == "PATCH" and "/applications/" in url:
            operation, response = "patch", None
        else:
            raise AssertionError(f"Unexpected Graph call: {method} {url}")
        if operation in state.errors:
            raise state.errors[operation]
        return response

    async def fake_send_async(request_info, return_type, error_mapping=None):
        return respond(request_info)

    async def fake_send_no_response_content_async(request_info, error_mapping=None):
        respond(request_info)

    monkeypatch.setattr(client.request_adapter, "send_async", fake_send_async)
    monkeypatch.setattr(client.request_adapter, "send_no_response_content_async", fake_send_no_response_content_async)
    return state


@pytest.fixture
def script_context(monkeypatch, graph_client):
    for key in [
        "AZURE_TENANT_ID",
        "AZURE_AUTH_TENANT_ID",
        "AZURE_CLIENT_APP_ID",
        "ADMIN_USER_OID",
        "BACKEND_URI",
        auth_common.MANAGED_CLIENT_APP_ID,
    ]:
        monkeypatch.delenv(key, raising=False)
    credential = mock.MagicMock()
    credential.__aenter__ = mock.AsyncMock(return_value=credential)
    credential.__aexit__ = mock.AsyncMock(return_value=None)
    credential_factory = mock.Mock(return_value=credential)
    graph_factory = mock.Mock(return_value=graph_client.client)
    for module in [auth_init, auth_update]:
        monkeypatch.setattr(module, "load_azd_env", lambda: None)
        monkeypatch.setattr(module, "AzureDeveloperCliCredential", credential_factory)
        monkeypatch.setattr(module, "GraphServiceClient", graph_factory)
    updates = {}
    monkeypatch.setattr(auth_init, "update_azd_env", lambda name, value: updates.update({name: value}))
    return SimpleNamespace(
        graph=graph_client,
        credential_factory=credential_factory,
        graph_factory=graph_factory,
        updates=updates,
    )


def test_owner_app_builder_requests_only_one_single_tenant_pkce_spa(graph_client):
    app = auth_init.owner_app_initial("unit")
    request = graph_client.client.applications.to_post_request_information(app)
    body = json.loads(request.content)
    assert body["displayName"] == "Template Owner Admin unit"
    assert body["signInAudience"] == "AzureADMyOrg"
    assert body["spa"]["redirectUris"] == auth_common.LOCAL_REDIRECT_URIS
    assert body.get("requiredResourceAccess", []) == []
    assert not {"api", "identifierUris", "passwordCredentials", "web", "publicClient"} & body.keys()
    assert "implicitGrantSettings" not in body["spa"]


@pytest.mark.asyncio
async def test_create_application_success_uses_no_secret_or_consent_requests(graph_client):
    result = await auth_init.create_application(graph_client.client, auth_init.owner_app_initial("unit"))
    assert result == (MOCK_OBJECT_ID, MOCK_APP_ID)
    assert len(graph_client.calls) == 1
    assert graph_client.calls[0].url.endswith("/applications")
    assert graph_client.calls[0].body.get("requiredResourceAccess", []) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "created", [None, Application(), Application(id=MOCK_OBJECT_ID), Application(app_id=MOCK_APP_ID)]
)
async def test_create_application_requires_returned_ids(graph_client, created):
    graph_client.created = created
    with pytest.raises(ValueError, match="no object ID or client ID"):
        await auth_init.create_application(graph_client.client, auth_init.owner_app_initial("unit"))


@pytest.mark.asyncio
async def test_get_application_does_not_treat_directory_denial_as_missing(graph_client):
    graph_client.errors["lookup"] = graph_error(403)
    with pytest.raises(APIError):
        await auth_common.get_application(graph_client.client, MOCK_APP_ID)
    assert [call.method for call in graph_client.calls] == ["GET"]
    graph_client.errors["lookup"] = graph_error(404)
    assert await auth_common.get_application(graph_client.client, MOCK_APP_ID) is None


@pytest.mark.asyncio
async def test_owner_oid_is_resolved_only_via_graph_delegated_me(graph_client):
    assert await auth_init.get_delegated_owner_oid(graph_client.client) == MOCK_OWNER_ID
    assert len(graph_client.calls) == 1
    assert graph_client.calls[0].url.endswith("/me")


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [None, User(), User(id=" ")])
async def test_owner_oid_requires_an_identified_human(graph_client, user):
    graph_client.user = user
    with pytest.raises(ValueError, match="signed-in human owner"):
        await auth_init.get_delegated_owner_oid(graph_client.client)


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_init_preserves_preconfigured_app_without_directory_calls(script_context, monkeypatch, capsys, managed):
    monkeypatch.setenv("AZURE_CLIENT_APP_ID", "external-app")
    if managed:
        monkeypatch.setenv(auth_common.MANAGED_CLIENT_APP_ID, "external-app")
    await auth_init.main()
    script_context.credential_factory.assert_not_called()
    assert script_context.updates == {}
    assert not script_context.graph.calls
    output = capsys.readouterr().out
    assert "ADMIN_USER_OID" in output
    assert "preserved" in output
    assert "disabled" in output


@pytest.mark.asyncio
async def test_init_without_tenant_keeps_public_access_and_admin_disabled(script_context, capsys):
    await auth_init.main()
    script_context.credential_factory.assert_not_called()
    assert script_context.updates == {}
    output = capsys.readouterr().out
    assert "Public chat stays anonymous" in output
    assert "owner administration remains disabled" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_owner", [False, True])
async def test_init_creates_one_owner_spa_and_no_server_app(script_context, monkeypatch, explicit_owner):
    monkeypatch.setenv("AZURE_TENANT_ID", "owner-tenant")
    if explicit_owner:
        monkeypatch.setenv("ADMIN_USER_OID", MOCK_OWNER_ID)
    await auth_init.main()
    assert script_context.updates == {
        "AZURE_CLIENT_APP_ID": MOCK_APP_ID,
        auth_common.MANAGED_CLIENT_APP_ID: MOCK_APP_ID,
        "ADMIN_USER_OID": MOCK_OWNER_ID,
    }
    script_context.credential_factory.assert_called_once_with(tenant_id="owner-tenant")
    calls = script_context.graph.calls
    assert sum(call.method == "POST" for call in calls) == 1
    assert sum(call.url.endswith("/me") for call in calls) == (0 if explicit_owner else 1)
    assert all("/applications" in call.url or call.url.endswith("/me") for call in calls)
    body = next(call.body for call in calls if call.method == "POST")
    assert not body.get("requiredResourceAccess")
    assert "passwordCredentials" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["me", "create"])
@pytest.mark.parametrize("status", [400, 401, 403])
async def test_init_directory_failures_print_manual_instructions_without_opening_admin(
    script_context, monkeypatch, capsys, operation, status
):
    monkeypatch.setenv("AZURE_TENANT_ID", "owner-tenant")
    script_context.graph.errors[operation] = graph_error(status)
    await auth_init.main()
    assert script_context.updates == {}
    output = capsys.readouterr().out
    assert "manual setup" in output
    assert "ADMIN_USER_OID" in output
    assert "owner administration remains disabled" in output


@pytest.mark.asyncio
async def test_init_never_uses_ci_principal_as_the_owner(script_context, monkeypatch, capsys):
    monkeypatch.setenv("AZURE_TENANT_ID", "owner-tenant")
    monkeypatch.setenv("AZURE_PRINCIPAL_ID", "ci-service-principal")
    script_context.graph.errors["me"] = graph_error(400)
    await auth_init.main()
    assert script_context.updates == {}
    assert [call.method for call in script_context.graph.calls] == ["GET"]
    assert "never a deployment service principal" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_init_missing_human_does_not_create_an_app(script_context, monkeypatch):
    monkeypatch.setenv("AZURE_AUTH_TENANT_ID", "legacy-owner-tenant")
    script_context.graph.user = None
    await auth_init.main()
    assert script_context.updates == {}
    assert [call.method for call in script_context.graph.calls] == ["GET"]


@pytest.mark.asyncio
async def test_init_does_not_hide_unexpected_directory_failures(script_context, monkeypatch):
    monkeypatch.setenv("AZURE_TENANT_ID", "owner-tenant")
    script_context.graph.errors["me"] = graph_error(500)
    with pytest.raises(APIError):
        await auth_init.main()
    assert script_context.updates == {}


@pytest.mark.asyncio
async def test_init_missing_cli_auth_prints_manual_steps_without_configuring_admin(script_context, monkeypatch, capsys):
    monkeypatch.setenv("AZURE_TENANT_ID", "owner-tenant")
    script_context.graph.errors["me"] = ClientAuthenticationError(message="No logged-in user")
    await auth_init.main()
    assert script_context.updates == {}
    assert [call.method for call in script_context.graph.calls] == ["GET"]
    output = capsys.readouterr().out
    assert "authentication is unavailable" in output
    assert "azd env set" in output
    assert "owner administration remains disabled" in output


def test_update_azd_env_uses_checked_argument_list_not_shell():
    with mock.patch.object(auth_init.subprocess, "run") as run:
        auth_init.update_azd_env("AZURE_CLIENT_APP_ID", "value with spaces")
    run.assert_called_once_with(["azd", "env", "set", "AZURE_CLIENT_APP_ID", "value with spaces"], check=True)


def test_redirect_update_builder_merges_only_spa_redirects(graph_client):
    update = auth_update.owner_redirect_update(graph_client.existing, "https://deployed.example/")
    request = graph_client.client.applications.by_application_id(MOCK_OBJECT_ID).to_patch_request_information(update)
    body = json.loads(request.content)
    assert body["spa"]["redirectUris"] == [
        "https://existing.example/redirect",
        *auth_common.LOCAL_REDIRECT_URIS,
        "https://deployed.example/redirect",
    ]
    assert not {"web", "publicClient", "api", "requiredResourceAccess", "passwordCredentials"} & body.keys()


def test_redirect_update_builder_handles_missing_spa_and_deduplicates():
    initial = Application()
    update = auth_update.owner_redirect_update(initial, "http://localhost:50505")
    assert update.spa.redirect_uris == auth_common.LOCAL_REDIRECT_URIS
    repeated = auth_update.owner_redirect_update(update, "http://localhost:50505")
    assert repeated.spa.redirect_uris == auth_common.LOCAL_REDIRECT_URIS


@pytest.mark.parametrize(
    "uri",
    [
        "http://public.example",
        "ftp://public.example",
        "https://user:password@public.example",
        "https://public.example/path",
        "https://public.example?query=1",
        "https://public.example#fragment",
        "https://",
    ],
)
def test_redirect_builder_rejects_unsafe_or_non_origin_backend_urls(uri):
    with pytest.raises(ValueError, match="HTTPS origin"):
        auth_common.owner_redirect_uris(uri)


@pytest.mark.asyncio
async def test_update_without_an_app_makes_no_directory_calls(script_context):
    await auth_update.main()
    script_context.credential_factory.assert_not_called()
    assert not script_context.graph.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [None, "different-managed-app"])
async def test_update_preserves_external_spa_and_explains_manual_redirect(script_context, monkeypatch, capsys, marker):
    monkeypatch.setenv("AZURE_CLIENT_APP_ID", "external-app")
    monkeypatch.setenv("BACKEND_URI", "https://deployed.example")
    if marker:
        monkeypatch.setenv(auth_common.MANAGED_CLIENT_APP_ID, marker)
    await auth_update.main()
    script_context.credential_factory.assert_not_called()
    assert not script_context.graph.calls
    assert "https://deployed.example/redirect" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_update_managed_spa_only_after_origin_and_tenant_are_known(script_context, monkeypatch):
    monkeypatch.setenv("AZURE_CLIENT_APP_ID", MOCK_APP_ID)
    monkeypatch.setenv(auth_common.MANAGED_CLIENT_APP_ID, MOCK_APP_ID)
    await auth_update.main()
    script_context.credential_factory.assert_not_called()
    monkeypatch.setenv("BACKEND_URI", "https://deployed.example")
    await auth_update.main()
    script_context.credential_factory.assert_not_called()
    monkeypatch.setenv("AZURE_TENANT_ID", "owner-tenant")
    await auth_update.main()
    assert [call.method for call in script_context.graph.calls] == ["GET", "PATCH"]
    update = script_context.graph.calls[-1].body
    assert update["spa"]["redirectUris"][-1] == "https://deployed.example/redirect"
    assert update["spa"]["redirectUris"][0] == "https://existing.example/redirect"
    assert not {"web", "publicClient", "api", "requiredResourceAccess", "passwordCredentials"} & update.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404])
async def test_update_failure_never_creates_a_replacement_app(script_context, monkeypatch, capsys, status):
    monkeypatch.setenv("AZURE_CLIENT_APP_ID", MOCK_APP_ID)
    monkeypatch.setenv(auth_common.MANAGED_CLIENT_APP_ID, MOCK_APP_ID)
    monkeypatch.setenv("BACKEND_URI", "https://deployed.example")
    monkeypatch.setenv("AZURE_TENANT_ID", "owner-tenant")
    script_context.graph.errors["lookup"] = graph_error(status)
    await auth_update.main()
    assert [call.method for call in script_context.graph.calls] == ["GET"]
    assert "https://deployed.example/redirect" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [graph_error(403), ClientAuthenticationError(message="No logged-in user")])
async def test_update_patch_or_credential_failure_has_manual_fallback(script_context, monkeypatch, capsys, error):
    monkeypatch.setenv("AZURE_CLIENT_APP_ID", MOCK_APP_ID)
    monkeypatch.setenv(auth_common.MANAGED_CLIENT_APP_ID, MOCK_APP_ID)
    monkeypatch.setenv("BACKEND_URI", "https://deployed.example")
    monkeypatch.setenv("AZURE_TENANT_ID", "owner-tenant")
    script_context.graph.errors["patch"] = error
    await auth_update.main()
    assert [call.method for call in script_context.graph.calls] == ["GET", "PATCH"]
    assert script_context.updates == {}
    assert "https://deployed.example/redirect" in capsys.readouterr().out


@pytest.mark.parametrize("tenant", ["common", "organizations", "consumers", "https://tenant", "tenant/path"])
def test_script_tenant_configuration_rejects_multitenant_or_url_authorities(tenant):
    with pytest.raises(ValueError, match="single Entra tenant"):
        auth_common.owner_tenant_id({"AZURE_TENANT_ID": tenant})


def test_script_tenant_configuration_matches_runtime_precedence():
    assert auth_common.owner_tenant_id({"AZURE_AUTH_TENANT_ID": "legacy"}) == "legacy"
    assert auth_common.owner_tenant_id({"AZURE_TENANT_ID": "primary", "AZURE_AUTH_TENANT_ID": "legacy"}) == "primary"
