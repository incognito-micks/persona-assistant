import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "infra/main.bicep").read_text(encoding="utf-8")
RESOURCES = (ROOT / "infra/resources.bicep").read_text(encoding="utf-8")
PARAMETERS = json.loads((ROOT / "infra/main.parameters.json").read_text(encoding="utf-8"))["parameters"]
MODELS = json.loads((ROOT / "infra/deployment-models.json").read_text(encoding="utf-8"))
AZURE_YAML = (ROOT / "azure.yaml").read_text(encoding="utf-8")
GITHUB = (ROOT / ".github/workflows/azure-dev.yml").read_text(encoding="utf-8")
AZDO = (ROOT / ".azdo/pipelines/azure-dev.yml").read_text(encoding="utf-8")
VALIDATION = (ROOT / ".github/workflows/azure-dev-validation.yaml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compiled_main():
    compiler = shutil.which("bicep")
    if not compiler:
        cached = Path.home() / ".azure/bin/bicep"
        if cached.is_file():
            compiler = str(cached)
    if not compiler:
        pytest.skip("Bicep CLI is not installed; source contract checks still run.")
    result = subprocess.run(
        [compiler, "build", str(ROOT / "infra/main.bicep"), "--no-restore", "--stdout"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "Warning" not in result.stderr, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def compiled_resources(compiled_main):
    modules = [item for item in compiled_main["resources"] if item["type"] == "Microsoft.Resources/deployments"]
    assert len(modules) == 1
    return modules[0]["properties"]["template"]


def resource(template, resource_type):
    matches = [item for item in template["resources"] if item["type"] == resource_type]
    assert len(matches) == 1
    return matches[0]


def module(template, name):
    return next(
        item
        for item in template["resources"]
        if item["type"] == "Microsoft.Resources/deployments" and item["name"] == name
    )


def test_python_ci_bootstraps_public_modules_before_offline_infrastructure_tests():
    workflow = yaml.safe_load((ROOT / ".github/workflows/python-test.yaml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["test_package"]["steps"]
    names = [step.get("name") for step in steps]
    restore = next(step for step in steps if step.get("name") == "Restore public Bicep modules for offline tests")
    assert restore["if"] == "runner.os != 'Windows'"
    assert restore["run"] == "az bicep restore --file infra/main.bicep"
    assert names.index(restore["name"]) < names.index("Run Python tests")


def test_owner_redirect_hook_is_inline_and_safe_for_workspace_paths_with_spaces():
    hooks = yaml.safe_load(AZURE_YAML)["hooks"]["postprovision"]
    assert hooks["posix"]["run"] == "sh ./scripts/auth_update.sh"
    assert "& ./scripts/auth_update.ps1" in hooks["windows"]["run"]
    assert hooks["posix"]["continueOnError"] is False
    assert hooks["windows"]["continueOnError"] is False


def test_only_location_environment_and_owner_setup_are_required(compiled_main):
    required = {name for name, value in compiled_main["parameters"].items() if "defaultValue" not in value}
    assert required == {"environmentName", "location", "clientAppId", "adminUserOid"}
    assert set(PARAMETERS) == required | {"tenantId", "disablePublicAccess", "backendImage"}
    assert PARAMETERS == {
        "environmentName": {"value": "${AZURE_ENV_NAME}"},
        "location": {"value": "${AZURE_LOCATION}"},
        "tenantId": {"value": "${AZURE_TENANT_ID}"},
        "clientAppId": {"value": "${AZURE_CLIENT_APP_ID}"},
        "adminUserOid": {"value": "${ADMIN_USER_OID}"},
        "disablePublicAccess": {"value": "${DISABLE_PUBLIC_ACCESS=false}"},
        "backendImage": {
            "value": "${SERVICE_BACKEND_IMAGE_NAME=mcr.microsoft.com/azuredocs/containerapps-helloworld:latest}"
        },
    }
    assert compiled_main["parameters"]["disablePublicAccess"]["defaultValue"] is False
    assert compiled_main["parameters"]["backendImage"]["defaultValue"] == (
        "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
    )


def test_generated_resource_outputs_are_not_deployment_choices(compiled_main):
    outputs = compiled_main["outputs"]
    for key in (
        "BACKEND_URI",
        "AZURE_OPENAI_SERVICE",
        "AZURE_OPENAI_CHATGPT_DEPLOYMENT",
        "AZURE_OPENAI_REALTIME_DEPLOYMENT",
        "AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT",
        "AZURE_COSMOSDB_ACCOUNT",
        "AZURE_ADMIN_SETTINGS_DATABASE",
        "AZURE_ADMIN_SETTINGS_CONTAINER",
        "AZURE_ANALYTICS_CONTAINER",
    ):
        assert key in outputs
    assert "AZURE_CLIENT_ID" not in outputs  # Do not replace CI's federation identity.
    assert "AZURE_OPENAI_SERVICE" not in json.dumps(PARAMETERS)
    assert "param location string" in MAIN
    assert "@allowed(" not in MAIN


def test_exact_stable_model_pins_are_shared_with_preflight():
    assert MODELS == {
        "text": {
            "name": "gpt-4.1-mini",
            "version": "2025-04-14",
            "deployment": "text",
            "sku": "GlobalStandard",
            "capacity": 10,
        },
        "realtime": {
            "name": "gpt-realtime-2.1-mini",
            "version": "2026-07-07",
            "deployment": "realtime",
            "sku": "GlobalStandard",
            "capacity": 1,
        },
        "transcription": {
            "name": "gpt-4o-mini-transcribe",
            "version": "2025-12-15",
            "deployment": "transcription",
            "sku": "GlobalStandard",
            "capacity": 1,
        },
        "voice": "alloy",
    }
    assert "loadJsonContent('deployment-models.json')" in RESOURCES
    assert "AZURE_OPENAI_REALTIME_MODEL" not in json.dumps(PARAMETERS)


def test_text_voice_and_transcription_deployments_are_unconditional(compiled_resources):
    openai = module(compiled_resources, "openai")
    assert "condition" not in openai
    deployments = openai["properties"]["parameters"]["deployments"]["value"]
    assert len(deployments) == 3
    for name, deployment in zip(("text", "realtime", "transcription"), deployments):
        assert deployment["name"] == f"[variables('{name}').deployment]"
        assert deployment["model"] == {
            "format": "OpenAI",
            "name": f"[variables('{name}').name]",
            "version": f"[variables('{name}').version]",
        }
        assert deployment["sku"] == {
            "name": f"[variables('{name}').sku]",
            "capacity": f"[variables('{name}').capacity]",
        }
    assert "useRealtimeVoice" not in MAIN + RESOURCES


@pytest.mark.parametrize(
    "name,value",
    [
        ("RUNNING_IN_PRODUCTION", "'true'"),
        ("USE_REALTIME_VOICE", "'true'"),
        ("USE_CHAT_HISTORY_BROWSER", "'true'"),
        ("USE_CHAT_HISTORY_COSMOS", "'false'"),
        ("OPENAI_HOST", "'azure'"),
        ("AZURE_OPENAI_CHATGPT_CONTEXT_WINDOW", "'16384'"),
        ("AZURE_CLIENT_ID", "identity.properties.clientId"),
        ("AZURE_TENANT_ID", "tenantId"),
        ("AZURE_CLIENT_APP_ID", "clientAppId"),
        ("ADMIN_USER_OID", "adminUserOid"),
        ("AZURE_OPENAI_SERVICE", "openAi.outputs.name"),
        ("AZURE_OPENAI_CHATGPT_MODEL", "text.name"),
        ("AZURE_OPENAI_CHATGPT_DEPLOYMENT", "text.deployment"),
        ("AZURE_OPENAI_REALTIME_DEPLOYMENT", "realtime.deployment"),
        ("AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT", "transcription.deployment"),
        ("AZURE_OPENAI_REALTIME_VOICE", "models.voice"),
        ("AZURE_COSMOSDB_ACCOUNT", "cosmosDb.outputs.name"),
        ("AZURE_ADMIN_SETTINGS_DATABASE", "databaseName"),
        ("AZURE_ADMIN_SETTINGS_CONTAINER", "settingsContainerName"),
        ("AZURE_ANALYTICS_CONTAINER", "analyticsContainerName"),
    ],
)
def test_required_runtime_environment_is_wired(name, value):
    assert re.search(rf"^  {name}: {re.escape(value)}$", RESOURCES, re.MULTILINE)
    assert "DISABLE_PUBLIC_ACCESS: disablePublicAccess ? 'true' : 'false'" in RESOURCES


def test_one_consumption_host_serves_the_same_origin(compiled_resources):
    backend = resource(compiled_resources, "Microsoft.App/containerApps")
    environment = resource(compiled_resources, "Microsoft.App/managedEnvironments")
    assert "createObject('azd-service-name', 'backend')" in backend["tags"]
    assert backend["properties"]["workloadProfileName"] == "Consumption"
    assert environment["properties"]["workloadProfiles"] == [
        {"name": "Consumption", "workloadProfileType": "Consumption"}
    ]
    assert environment["properties"]["appLogsConfiguration"] is None
    ingress = backend["properties"]["configuration"]["ingress"]
    assert ingress == {"external": True, "allowInsecure": False, "targetPort": 8000, "transport": "auto"}
    assert "corsPolicy" not in ingress
    assert backend["properties"]["template"]["scale"]["minReplicas"] == 0
    assert backend["properties"]["template"]["scale"]["maxReplicas"] == 3
    assert backend["properties"]["template"]["containers"][0]["image"] == "[parameters('backendImage')]"
    assert "existingBackend" not in RESOURCES
    assert "host: containerapp" in AZURE_YAML
    assert "name: persona-assistant" in AZURE_YAML
    assert "project: ./app/backend" in AZURE_YAML
    assert "context: ." in AZURE_YAML
    manifest = yaml.safe_load(AZURE_YAML)
    assert manifest["requiredVersions"]["azd"] == ">= 1.37.1"


def test_cosmos_settings_and_analytics_are_unconditional_and_serverless(compiled_resources):
    cosmos = module(compiled_resources, "cosmosdb")
    assert "condition" not in cosmos
    parameters = cosmos["properties"]["parameters"]
    assert parameters["capabilitiesToAdd"]["value"] == ["EnableServerless"]
    assert parameters["disableLocalAuth"]["value"] is True
    databases = parameters["sqlDatabases"]["value"]
    assert len(databases) == 1
    assert "throughput" not in databases[0]
    containers = databases[0]["containers"]
    assert containers == [
        {"name": "[variables('settingsContainerName')]", "kind": "Hash", "paths": ["/id"], "defaultTtl": -1},
        {"name": "[variables('analyticsContainerName')]", "kind": "Hash", "paths": ["/user_oid"]},
    ]
    assert compiled_resources["variables"]["databaseName"] == "persona-assistant"
    assert compiled_resources["variables"]["settingsContainerName"] == "admin_settings"
    assert compiled_resources["variables"]["analyticsContainerName"] == "analytics"
    assert "chat-history" not in json.dumps(containers)


def test_application_uses_managed_identity_and_resource_scoped_roles(compiled_resources):
    backend = resource(compiled_resources, "Microsoft.App/containerApps")
    registry = resource(compiled_resources, "Microsoft.ContainerRegistry/registries")
    pull = resource(compiled_resources, "Microsoft.Authorization/roleAssignments")
    cosmos = resource(compiled_resources, "Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments")
    openai = module(compiled_resources, "openai")["properties"]["parameters"]
    assert backend["identity"]["type"] == "UserAssigned"
    assert len(backend["identity"]["userAssignedIdentities"]) == 1
    assert "userAssignedIdentities" in backend["properties"]["configuration"]["registries"][0]["identity"]
    assert registry["properties"]["adminUserEnabled"] is False
    assert registry["properties"]["policies"]["azureADAuthenticationAsArmPolicy"]["status"] == "enabled"
    assert "7f951dda-4ed3-4680-a7ca-43fe172d538d" in pull["properties"]["roleDefinitionId"]
    assert "Microsoft.ContainerRegistry/registries" in pull["scope"]
    assert openai["disableLocalAuth"]["value"] is True
    assert openai["publicNetworkAccess"]["value"] == "Enabled"
    assert openai["networkAcls"]["value"] == {"defaultAction": "Allow"}
    roles = openai["roleAssignments"]["value"]
    assert len(roles) == 1
    assert roles[0]["roleDefinitionIdOrName"] == "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
    assert roles[0]["principalType"] == "ServicePrincipal"
    assert roles[0]["principalId"] == cosmos["properties"]["principalId"] == pull["properties"]["principalId"]
    assert "00000000-0000-0000-0000-000000000002" in cosmos["properties"]["roleDefinitionId"]
    assert "Microsoft.DocumentDB/databaseAccounts" in cosmos["properties"]["scope"]
    assert "secrets" not in backend["properties"]["configuration"]


@pytest.mark.parametrize(
    "retired",
    [
        "appservice",
        "usePrivateEndpoint",
        "useVpnGateway",
        "b2c",
        "serverAppSecret",
        "clientAppSecret",
        "SpeechServices",
        "useEval",
        "useAiProject",
        "storageAccount",
        "ingestion",
        "AZURE_OPENAI_API_KEY",
        "OPENAI_API_KEY",
    ],
)
def test_legacy_matrix_and_secrets_are_not_in_the_primary_path(retired):
    assert retired.lower() not in (MAIN + RESOURCES + json.dumps(PARAMETERS) + GITHUB + AZDO).lower()


def test_local_build_overrides_are_never_container_environment():
    assert "PROFILE_PATH" not in MAIN + RESOURCES + json.dumps(PARAMETERS)
    assert "KNOWLEDGE_DATA_DIR" not in MAIN + RESOURCES + json.dumps(PARAMETERS)
    assert "@secure()" not in MAIN + RESOURCES
    assert "listKeys" not in MAIN + RESOURCES
    assert (ROOT / "LICENSE").is_file()


def test_all_local_bicep_module_references_resolve():
    reachable = set()

    def visit(path):
        if path in reachable:
            return
        reachable.add(path)
        content = path.read_text(encoding="utf-8")
        for reference in re.findall(r"module\s+\w+\s+'([^']+)'", content):
            if reference.startswith("br/"):
                continue
            target = path.parent / reference
            assert target.is_file(), f"Missing local module: {target}"
            visit(target.resolve())

    visit((ROOT / "infra/main.bicep").resolve())
    visit((ROOT / "infra/main.test.bicep").resolve())
    assert reachable == {path.resolve() for path in (ROOT / "infra").rglob("*.bicep")}


def test_build_hooks_package_knowledge_and_frontend_with_locked_dependencies():
    for name in ("prepackage", "prebuild"):
        section = AZURE_YAML.split(f"      {name}:", 1)[1].split("\nhooks:", 1)[0]
        assert "python package_knowledge.py" in section
        assert "npm ci" in section
        assert "npm run build" in section
    assert "npm install" not in AZURE_YAML
    assert "remoteBuild: true" in AZURE_YAML


def test_preflight_is_a_fail_closed_preprovision_hook():
    preprovision = AZURE_YAML.split("  preprovision:", 1)[1].split("  postprovision:", 1)[0]
    assert preprovision.count("python scripts/deploy_preflight.py") == 2
    assert "python scripts/deploy_preflight.py && ./scripts/auth_init.sh" in preprovision
    assert preprovision.index("python scripts/deploy_preflight.py") < preprovision.index("./scripts/auth_init.ps1")
    assert "continueOnError: true" not in AZURE_YAML
    assert "./scripts/auth_update.sh" in AZURE_YAML
    assert "./scripts/auth_update.ps1" in AZURE_YAML


def test_deployment_workflows_are_explicit_and_secretless():
    assert "workflow_dispatch:" in GITHUB
    assert re.search(r"^\s+push:", GITHUB, re.MULTILINE) is None
    assert "trigger: none" in AZDO
    assert "pr: none" in AZDO
    assert "secrets." not in GITHUB
    assert "SECRET" not in GITHUB + AZDO
    for name in ("AZURE_ENV_NAME", "AZURE_LOCATION", "AZURE_TENANT_ID", "AZURE_CLIENT_APP_ID", "ADMIN_USER_OID"):
        assert name + ": ${{ vars." + name + " }}" in GITHUB
        assert name + ": $[ variables." + name + " ]" in AZDO
    assert "auth.useAzCliAuth true" in GITHUB
    assert "auth.useAzCliAuth true" in AZDO


def test_validation_workflow_has_no_azure_admission_or_application_build():
    assert "--validate-local" in VALIDATION
    assert "--no-restore" in VALIDATION
    assert "--noconftest" in VALIDATION
    for command in ("azd provision", "azd deploy", "az login", "azd auth login", "docker build", "npm run build"):
        assert command not in VALIDATION
    assert "az bicep build" in VALIDATION
