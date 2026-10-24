import copy
import json
import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import deploy_preflight as preflight

ROOT = Path(__file__).resolve().parents[1]
MODELS = preflight.load_models(ROOT)
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
CLIENT = "33333333-3333-3333-3333-333333333333"
OWNER = "44444444-4444-4444-4444-444444444444"


@pytest.fixture
def deployment_env():
    return {
        "AZURE_ENV_NAME": "persona-dev",
        "AZURE_LOCATION": "eastus2",
        "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION,
        "AZURE_TENANT_ID": TENANT,
        "AZURE_CLIENT_APP_ID": CLIENT,
        "ADMIN_USER_OID": OWNER,
    }


class AzureReadMock:
    def __init__(self):
        self.commands = []
        self.account = {
            "id": SUBSCRIPTION,
            "tenantId": TENANT,
            "state": "Enabled",
            "environmentName": "AzureCloud",
            "user": {"type": "user"},
        }
        self.openai_account = {
            "location": "eastus2",
            "tags": {"application": "persona-assistant", "azd-env-name": "persona-dev"},
        }
        self.permissions = [{"actions": ["*"], "notActions": []}]
        self.providers = {
            namespace: {
                "registrationState": "Registered",
                "resourceTypes": [
                    {"resourceType": resource_type, "locations": ["East US 2"]} for resource_type in resource_types
                ],
            }
            for namespace, resource_types in preflight.PROVIDER_TYPES.items()
        }
        self.models = [
            {
                "kind": "OpenAI",
                "skuName": "S0",
                "model": {
                    "name": spec.name,
                    "version": spec.version,
                    "lifecycleStatus": "GenerallyAvailable",
                    "skus": [
                        {
                            "name": spec.sku,
                            "usageName": f"OpenAI.{spec.sku}.{spec.name}",
                            "capacity": {"minimum": 1, "maximum": 100, "step": 1},
                        }
                    ],
                },
            }
            for spec in MODELS.values()
        ]
        self.usages = [
            {
                "name": {"value": model["model"]["skus"][0]["usageName"]},
                "currentValue": 0,
                "limit": 100,
                "unit": "Count",
                "status": "Included",
            }
            for model in self.models
        ]
        self.deployments = []

    def __call__(self, arguments):
        self.commands.append(list(arguments))
        if arguments[:2] == ["account", "show"]:
            return copy.deepcopy(self.account)
        if arguments[:2] == ["provider", "show"]:
            return copy.deepcopy(self.providers[arguments[arguments.index("--namespace") + 1]])
        assert arguments[:3] == ["rest", "--method", "GET"], "Preflight attempted a non-read-only operation"
        url = arguments[arguments.index("--url") + 1]
        if "/accounts/" in url and "/deployments?" not in url:
            return copy.deepcopy(self.openai_account)
        for suffix, data in (
            ("/permissions?", self.permissions),
            ("/models?", self.models),
            ("/usages?", self.usages),
            ("/deployments?", self.deployments),
        ):
            if suffix in url:
                return {"value": copy.deepcopy(data)}
        raise AssertionError(f"Unexpected Azure request: {arguments}")


@pytest.fixture
def azure():
    return AzureReadMock()


@pytest.fixture
def local_project(tmp_path):
    (tmp_path / "infra").mkdir()
    for relative in ("infra/main.bicep", "infra/resources.bicep", "azure.yaml"):
        (tmp_path / relative).write_text("local input", encoding="utf-8")
    (tmp_path / "infra/main.parameters.json").write_text("{}", encoding="utf-8")
    (tmp_path / "infra/deployment-models.json").write_text(
        (ROOT / "infra/deployment-models.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "profile.json").write_text('{"assistant_name": "Persona Assistant"}', encoding="utf-8")
    (tmp_path / "data/work").mkdir(parents=True)
    (tmp_path / "data/work/knowledge.md").write_text("# Example\nPublic knowledge.\n", encoding="utf-8")
    return tmp_path


def test_local_validation_needs_no_azure_environment_or_subprocess(local_project, monkeypatch):
    run = Mock(side_effect=AssertionError("No subprocess may run offline"))
    monkeypatch.setattr(preflight.subprocess, "run", run)
    assert preflight.validate_local(local_project, {}) == MODELS
    run.assert_not_called()


def test_local_overrides_only_select_packaging_sources(local_project):
    (local_project / "owner.json").write_text("{}", encoding="utf-8")
    (local_project / "notes").mkdir()
    (local_project / "notes/knowledge.md").write_text("Selected local source", encoding="utf-8")
    assert (
        preflight.validate_local(local_project, {"PROFILE_PATH": "owner.json", "KNOWLEDGE_DATA_DIR": "notes"}) == MODELS
    )


@pytest.mark.parametrize("relative", ["infra/main.bicep", "infra/resources.bicep", "azure.yaml"])
def test_missing_local_deployment_file_fails(local_project, relative):
    (local_project / relative).unlink()
    with pytest.raises(preflight.PreflightError, match="Missing deployment input"):
        preflight.validate_local(local_project, {})


@pytest.mark.parametrize("profile", ["[]", "not-json", ""])
def test_invalid_profile_fails_offline(local_project, profile):
    (local_project / "profile.json").write_text(profile, encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="JSON"):
        preflight.validate_local(local_project, {})


@pytest.mark.parametrize("content", [b"", b"   \n", b"\xff"])
def test_empty_or_non_utf8_knowledge_fails(local_project, content):
    (local_project / "data/work/knowledge.md").write_bytes(content)
    with pytest.raises(preflight.PreflightError, match="empty|UTF-8"):
        preflight.validate_local(local_project, {})


def test_nested_only_knowledge_fails(local_project):
    (local_project / "data/work/knowledge.md").unlink()
    (local_project / "data/work/nested").mkdir()
    (local_project / "data/work/nested/knowledge.md").write_text("Nested content", encoding="utf-8")
    with pytest.raises(preflight.PreflightError, match="No top-level markdown"):
        preflight.validate_local(local_project, {})


@pytest.mark.parametrize(
    "change",
    [
        lambda data: data.pop("realtime"),
        lambda data: data.pop("transcription"),
        lambda data: data["realtime"].pop("version"),
        lambda data: data["realtime"].update(version=" "),
        lambda data: data["realtime"].update(capacity=0),
        lambda data: data["realtime"].update(capacity=True),
        lambda data: data["realtime"].update(name="gpt-4o-realtime-preview"),
        lambda data: data["realtime"].update(sku="Standard"),
        lambda data: data["realtime"].update(deployment=data["text"]["deployment"]),
        lambda data: data["transcription"].update(deployment=data["realtime"]["deployment"]),
        lambda data: data.update(voice=""),
    ],
)
def test_incomplete_or_text_only_model_configuration_fails(local_project, change):
    path = local_project / "infra/deployment-models.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(preflight.PreflightError):
        preflight.load_models(local_project)


def test_cloud_success_uses_only_read_operations(deployment_env, azure):
    preflight.validate_cloud(MODELS, deployment_env, azure)
    assert len(azure.commands) == 9
    assert azure.commands[0] == ["account", "show", "--subscription", SUBSCRIPTION]
    assert all(
        command[:2] in (["account", "show"], ["provider", "show"], ["rest", "--method"]) for command in azure.commands
    )


def test_missing_transcription_deployment_model_fails_before_provision(deployment_env, azure):
    azure.models = [model for model in azure.models if model["model"]["name"] != MODELS["transcription"].name]
    with pytest.raises(preflight.PreflightError, match="Mandatory transcription model"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


def test_transcription_quota_is_required_even_when_voice_quota_is_available(deployment_env, azure):
    for usage in azure.usages:
        if usage["name"]["value"] == f"OpenAI.GlobalStandard.{MODELS['transcription'].name}":
            usage["limit"] = 0
    with pytest.raises(preflight.PreflightError, match="Insufficient quota.*gpt-4o-mini-transcribe"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


@pytest.mark.parametrize(
    "name,value,message",
    [
        ("AZURE_ENV_NAME", "", "AZURE_ENV_NAME"),
        ("AZURE_ENV_NAME", "../unsafe", "AZURE_ENV_NAME"),
        ("AZURE_ENV_NAME", "x" * 33, "AZURE_ENV_NAME"),
        ("AZURE_LOCATION", "", "AZURE_LOCATION"),
        ("AZURE_LOCATION", "East US 2", "AZURE_LOCATION"),
        ("AZURE_SUBSCRIPTION_ID", "not-a-uuid", "UUID"),
        ("AZURE_CLIENT_APP_ID", "not-a-uuid", "UUID"),
        ("ADMIN_USER_OID", "not-a-uuid", "UUID"),
        ("AZURE_TENANT_ID", OWNER, "differs"),
    ],
)
def test_invalid_cloud_inputs_fail_before_resource_checks(deployment_env, azure, name, value, message):
    deployment_env[name] = value
    with pytest.raises(preflight.PreflightError, match=message):
        preflight.validate_cloud(MODELS, deployment_env, azure)
    assert len(azure.commands) <= 1


@pytest.mark.parametrize("key,value", [("state", "Disabled"), ("environmentName", "AzureUSGovernment")])
def test_disabled_or_nonpublic_subscription_fails(deployment_env, azure, key, value):
    azure.account[key] = value
    with pytest.raises(preflight.PreflightError, match="subscription"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


@pytest.mark.parametrize("name", ["AZURE_CLIENT_APP_ID", "ADMIN_USER_OID"])
def test_service_principal_cannot_infer_owner_or_create_spa(deployment_env, azure, name):
    azure.account["user"]["type"] = "servicePrincipal"
    deployment_env.pop(name)
    with pytest.raises(preflight.PreflightError, match=f"{name} is required"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


def test_delegated_owner_setup_may_supply_auth_values_after_preflight(deployment_env, azure):
    deployment_env.pop("AZURE_CLIENT_APP_ID")
    deployment_env.pop("ADMIN_USER_OID")
    preflight.validate_cloud(MODELS, deployment_env, azure)


def test_role_assignment_permission_is_required(deployment_env, azure):
    azure.permissions = [{"actions": ["*"], "notActions": ["Microsoft.Authorization/*/write"]}]
    with pytest.raises(preflight.PreflightError, match="roleAssignments/write"):
        preflight.validate_cloud(MODELS, deployment_env, azure)
    assert all(command[0] != "provider" for command in azure.commands)


def test_permission_union_does_not_treat_notactions_as_global_deny():
    action = "Microsoft.Authorization/roleAssignments/write"
    contributor = {"actions": ["*"], "notActions": ["Microsoft.Authorization/*/Write"]}
    rbac_admin = {"actions": ["microsoft.authorization/roleassignments/*"]}
    assert not preflight.permission_allows(contributor, action)
    assert preflight.permission_allows(rbac_admin, action)
    assert any(preflight.permission_allows(role, action) for role in (contributor, rbac_admin))


def test_unregistered_provider_fails_with_manual_registration_steps(deployment_env, azure):
    azure.providers["Microsoft.App"]["registrationState"] = "NotRegistered"
    with pytest.raises(preflight.PreflightError, match="az provider register --namespace Microsoft.App"):
        preflight.validate_cloud(MODELS, deployment_env, azure)
    assert all("register" not in command for command in azure.commands)


def test_unavailable_container_apps_region_fails(deployment_env, azure):
    azure.providers["Microsoft.App"]["resourceTypes"][0]["locations"] = ["West Europe"]
    with pytest.raises(preflight.PreflightError, match="managedEnvironments is not advertised"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


@pytest.mark.parametrize("types", [None, "invalid", [1]])
def test_malformed_provider_metadata_fails_actionably(deployment_env, azure, types):
    azure.providers["Microsoft.App"]["resourceTypes"] = types
    with pytest.raises(preflight.PreflightError, match="invalid provider resource types"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


def test_malformed_provider_locations_fail_actionably(deployment_env, azure):
    azure.providers["Microsoft.App"]["resourceTypes"][0]["locations"] = None
    with pytest.raises(preflight.PreflightError, match="invalid locations"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


def test_malformed_permissions_fail_actionably(deployment_env, azure):
    azure.permissions[0]["actions"] = "not-an-array"
    with pytest.raises(preflight.PreflightError, match="invalid ARM permissions"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


@pytest.mark.parametrize("field,value", [("name", "other"), ("version", "2024-12-17")])
def test_missing_exact_realtime_model_has_no_text_only_fallback(deployment_env, azure, field, value):
    azure.models[1]["model"][field] = value
    with pytest.raises(preflight.PreflightError, match="Mandatory realtime.*Text-only deployment is not supported"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


def test_missing_realtime_sku_fails(deployment_env, azure):
    azure.models[1]["model"]["skus"][0]["name"] = "Standard"
    with pytest.raises(preflight.PreflightError, match="Mandatory realtime"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


@pytest.mark.parametrize("state", ["Preview", "Deprecated"])
def test_preview_or_deprecated_model_fails(azure, state):
    azure.models[1]["model"]["lifecycleStatus"] = state
    with pytest.raises(preflight.PreflightError, match="stable model"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


@pytest.mark.parametrize("retirement", ["2000-01-01T00:00:00Z", "2000-01-01T00:00:00"])
def test_retired_model_fails(azure, retirement):
    azure.models[1]["model"]["deprecation"] = {"inference": retirement}
    with pytest.raises(preflight.PreflightError, match="inference has retired"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


@pytest.mark.parametrize("date", ["not-a-date", 123])
def test_unparseable_retirement_date_fails(azure, date):
    azure.models[1]["model"]["deprecation"] = {"inference": date}
    with pytest.raises(preflight.PreflightError, match="Cannot verify.*retirement"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


@pytest.mark.parametrize(
    "capacity",
    [
        {"minimum": 2, "maximum": 100, "step": 1},
        {"minimum": 0, "maximum": 100, "step": 2},
        {"minimum": 1, "maximum": 0, "step": 1},
        {"minimum": 1, "maximum": 100, "step": 0},
    ],
)
def test_advertised_model_capacity_constraints_are_enforced(azure, capacity):
    azure.models[1]["model"]["skus"][0]["capacity"] = capacity
    with pytest.raises(preflight.PreflightError, match="template requests 1"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


def test_missing_usage_name_fails_instead_of_guessing_quota(azure):
    azure.models[1]["model"]["skus"][0].pop("usageName")
    with pytest.raises(preflight.PreflightError, match="quota usageName"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


def test_quota_name_comes_from_model_registry_not_model_name(azure):
    usage_name = "opaque-quota-family-returned-by-azure"
    azure.models[1]["model"]["skus"][0]["usageName"] = usage_name
    azure.usages[1]["name"]["value"] = usage_name
    preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


def test_missing_model_quota_fails(azure):
    azure.usages.pop()
    with pytest.raises(preflight.PreflightError, match="Cannot find unambiguous quota"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


@pytest.mark.parametrize("status", ["Blocked", "InOverage", "Unknown"])
def test_blocked_quota_fails(azure, status):
    azure.usages[1]["status"] = status
    with pytest.raises(preflight.PreflightError, match="Insufficient quota"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


@pytest.mark.parametrize("invalid", [None, True, "100", float("nan"), float("inf"), -1])
def test_invalid_quota_values_fail(azure, invalid):
    azure.usages[1]["limit"] = invalid
    with pytest.raises(preflight.PreflightError, match="quota cannot be verified"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


def test_negative_quota_usage_fails(azure):
    azure.usages[1]["currentValue"] = -1
    with pytest.raises(preflight.PreflightError, match="quota cannot be verified"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


@pytest.mark.parametrize("metadata", ["invalid", {"name": "unrelated"}])
def test_malformed_or_missing_model_metadata_fails_actionably(azure, metadata):
    azure.models[1]["model"] = metadata
    with pytest.raises(preflight.PreflightError):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


def test_malformed_model_sku_list_fails_actionably(azure):
    azure.models[1]["model"]["skus"] = None
    with pytest.raises(preflight.PreflightError, match="invalid model SKUs"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


def test_insufficient_realtime_quota_stops_preprovision(deployment_env, azure):
    azure.usages[1]["limit"] = 0
    with pytest.raises(preflight.PreflightError, match="1 additional capacity required"):
        preflight.validate_cloud(MODELS, deployment_env, azure)
    assert all(command[0] not in {"deployment", "group", "containerapp"} for command in azure.commands)


def test_shared_quota_usage_names_are_summed(azure):
    for model in azure.models:
        model["model"]["skus"][0]["usageName"] = "shared"
    azure.usages = [{"name": {"value": "shared"}, "limit": 10, "currentValue": 0}]
    with pytest.raises(preflight.PreflightError, match="12 additional capacity required"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2")


def test_reprovision_does_not_require_quota_for_existing_capacity(deployment_env, azure):
    deployment_env.update(AZURE_OPENAI_SERVICE="oai-persona-existing", AZURE_RESOURCE_GROUP="rg-persona-dev")
    azure.deployments = [
        {
            "name": spec.deployment,
            "properties": {"model": {"name": spec.name, "version": spec.version}},
            "sku": {"name": spec.sku, "capacity": spec.capacity},
        }
        for spec in MODELS.values()
    ]
    for usage in azure.usages:
        usage["currentValue"] = usage["limit"]
    preflight.validate_cloud(MODELS, deployment_env, azure)
    command = azure.commands[-1]
    assert "/accounts/oai-persona-existing/deployments?" in command[command.index("--url") + 1]


def test_existing_allocation_in_another_region_cannot_satisfy_selected_region_quota(deployment_env, azure):
    deployment_env.update(AZURE_OPENAI_SERVICE="oai-persona-existing", AZURE_RESOURCE_GROUP="rg-persona-dev")
    azure.openai_account["location"] = "westeurope"
    with pytest.raises(preflight.PreflightError, match="different OpenAI region"):
        preflight.validate_cloud(MODELS, deployment_env, azure)


def test_unrelated_account_is_not_counted_as_target_deployment_capacity(deployment_env, azure):
    deployment_env.update(AZURE_OPENAI_SERVICE="legacy-account", AZURE_RESOURCE_GROUP="legacy-group")
    azure.openai_account["tags"] = {}
    azure.usages[1]["limit"] = 0
    with pytest.raises(preflight.PreflightError, match="Insufficient quota"):
        preflight.validate_cloud(MODELS, deployment_env, azure)
    assert all("/deployments?" not in " ".join(command) for command in azure.commands)


def test_existing_other_version_does_not_hide_new_quota_requirements(azure):
    existing = [
        {
            "name": "realtime",
            "properties": {"model": {"name": MODELS["realtime"].name, "version": "old"}},
            "sku": {"name": "GlobalStandard", "capacity": 1},
        }
    ]
    azure.usages[1]["limit"] = 0
    with pytest.raises(preflight.PreflightError, match="Insufficient quota"):
        preflight.check_models(MODELS, azure.models, azure.usages, "eastus2", existing)


def test_arm_list_paginates_with_read_only_requests():
    prefix = f"{preflight.ARM_ENDPOINT}/subscriptions/{SUBSCRIPTION}/"
    runner = Mock(side_effect=[{"value": [{"a": 1}], "nextLink": prefix + "page2"}, {"value": [{"a": 2}]}])
    values = preflight.arm_values(f"/subscriptions/{SUBSCRIPTION}/page1", SUBSCRIPTION, runner)
    assert values == [{"a": 1}, {"a": 2}]
    assert runner.call_args_list[1].args[0] == [
        "rest",
        "--method",
        "GET",
        "--url",
        prefix + "page2",
        "--subscription",
        SUBSCRIPTION,
    ]


@pytest.mark.parametrize(
    "link",
    [
        "https://example.com/steal",
        f"{preflight.ARM_ENDPOINT}/subscriptions/{OWNER}/other",
        f"{preflight.ARM_ENDPOINT}/subscriptions/{SUBSCRIPTION}/page1",
        123,
    ],
)
def test_unsafe_or_repeated_arm_pagination_fails(link):
    runner = Mock(return_value={"value": [], "nextLink": link})
    with pytest.raises(preflight.PreflightError, match="pagination"):
        preflight.arm_values(f"/subscriptions/{SUBSCRIPTION}/page1", SUBSCRIPTION, runner)
    runner.assert_called_once()


@pytest.mark.parametrize("response", [{}, {"value": "invalid"}, {"value": [1]}])
def test_malformed_arm_list_fails(response):
    with pytest.raises(preflight.PreflightError, match="invalid list"):
        preflight.arm_values(f"/subscriptions/{SUBSCRIPTION}/page1", SUBSCRIPTION, Mock(return_value=response))


@pytest.mark.parametrize(
    "failure,message",
    [
        (FileNotFoundError(), "Azure CLI is missing"),
        (subprocess.TimeoutExpired("az", 120), "read-only check failed"),
        (OSError("network unavailable"), "read-only check failed"),
    ],
)
def test_azure_cli_execution_failures_are_actionable(monkeypatch, failure, message):
    monkeypatch.setattr(preflight.subprocess, "run", Mock(side_effect=failure))
    with pytest.raises(preflight.PreflightError, match=message):
        preflight.run_az_json(["account", "show"])


def test_azure_cli_authorization_failure_is_not_bypassed(monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess(["az"], 1, "", "AuthorizationFailed: insufficient rights"))
    monkeypatch.setattr(preflight.subprocess, "run", run)
    with pytest.raises(preflight.PreflightError, match="AuthorizationFailed"):
        preflight.run_az_json(["rest", "--method", "GET", "--url", preflight.ARM_ENDPOINT])
    assert run.call_args.kwargs["timeout"] == 120
    assert run.call_args.kwargs["check"] is False


def test_invalid_cli_json_fails(monkeypatch):
    monkeypatch.setattr(
        preflight.subprocess, "run", Mock(return_value=subprocess.CompletedProcess(["az"], 0, "invalid", ""))
    )
    with pytest.raises(preflight.PreflightError, match="invalid JSON"):
        preflight.run_az_json(["account", "show"])


def test_cli_help_never_runs_cloud_or_local_checks(monkeypatch, capsys):
    validate = Mock(side_effect=AssertionError("help must not validate"))
    monkeypatch.setattr(preflight, "validate_local", validate)
    with pytest.raises(SystemExit) as exc:
        preflight.main(["--help"])
    assert exc.value.code == 0
    assert "--validate-local" in capsys.readouterr().out
    validate.assert_not_called()


def test_cli_local_mode_never_calls_azure(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "validate_local", Mock(return_value=MODELS))
    cloud = Mock(side_effect=AssertionError("offline must not call Azure"))
    monkeypatch.setattr(preflight, "validate_cloud", cloud)
    assert preflight.main(["--validate-local"]) == 0
    assert "No Azure calls" in capsys.readouterr().out
    cloud.assert_not_called()


def test_cli_default_runs_read_only_checks_and_does_not_claim_capacity(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "validate_local", Mock(return_value=MODELS))
    cloud = Mock()
    monkeypatch.setattr(preflight, "validate_cloud", cloud)
    assert preflight.main([]) == 0
    cloud.assert_called_once()
    assert "Capacity is not reserved" in capsys.readouterr().out


def test_cli_failure_returns_nonzero_without_cloud_fallback(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "validate_local", Mock(side_effect=preflight.PreflightError("Missing profile")))
    cloud = Mock()
    monkeypatch.setattr(preflight, "validate_cloud", cloud)
    assert preflight.main(["--validate-local"]) == 1
    assert "Deployment preflight failed: Missing profile" in capsys.readouterr().err
    cloud.assert_not_called()


def test_script_entrypoint_is_offline_with_validate_local(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["deploy_preflight.py", "--validate-local"])
    run = Mock(side_effect=AssertionError("The offline entrypoint must not run a subprocess"))
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ROOT / "scripts/deploy_preflight.py"), run_name="__main__")
    assert exc.value.code == 0
    assert "No Azure calls" in capsys.readouterr().out
    run.assert_not_called()
