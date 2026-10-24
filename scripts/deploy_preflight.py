"""Persona Assistant deployment checks. Never provisions or modifies Azure resources."""

import argparse
import fnmatch
import json
import math
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

ARM_ENDPOINT = "https://management.azure.com"
COGNITIVE_API_VERSION = "2024-10-01"
PROVIDER_TYPES = {
    "Microsoft.App": ("managedEnvironments", "containerApps"),
    "Microsoft.ContainerRegistry": ("registries",),
    "Microsoft.CognitiveServices": ("accounts",),
    "Microsoft.DocumentDB": ("databaseAccounts",),
    "Microsoft.ManagedIdentity": ("userAssignedIdentities",),
}
REQUIRED_ACTIONS = (
    "Microsoft.Resources/subscriptions/resourceGroups/write",
    "Microsoft.Resources/deployments/write",
    "Microsoft.ManagedIdentity/userAssignedIdentities/write",
    "Microsoft.ManagedIdentity/userAssignedIdentities/assign/action",
    "Microsoft.App/managedEnvironments/write",
    "Microsoft.App/containerApps/write",
    "Microsoft.ContainerRegistry/registries/write",
    "Microsoft.ContainerRegistry/registries/scheduleRun/action",
    "Microsoft.ContainerRegistry/registries/listBuildSourceUploadUrl/action",
    "Microsoft.ContainerRegistry/registries/push/write",
    "Microsoft.CognitiveServices/accounts/write",
    "Microsoft.CognitiveServices/accounts/deployments/write",
    "Microsoft.DocumentDB/databaseAccounts/write",
    "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/write",
    "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/write",
    "Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments/write",
    "Microsoft.Authorization/roleAssignments/write",
)
AzureRunner = Callable[[Sequence[str]], Any]


class PreflightError(Exception):
    """An actionable failure that must stop the azd preprovision hook."""


@dataclass(frozen=True)
class ModelSpec:
    name: str
    version: str
    deployment: str
    sku: str
    capacity: int


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise PreflightError(f"Cannot read valid UTF-8 JSON from {path}: {exc}") from exc


def load_models(root: Path) -> dict[str, ModelSpec]:
    data = read_json(root / "infra/deployment-models.json")
    if not isinstance(data, dict) or set(data) != {"text", "realtime", "transcription", "voice"}:
        raise PreflightError(
            "infra/deployment-models.json must define mandatory text, realtime, transcription and voice entries."
        )
    models = {}
    for kind in ("text", "realtime", "transcription"):
        value = data[kind]
        if not isinstance(value, dict) or set(value) != {"name", "version", "deployment", "sku", "capacity"}:
            raise PreflightError(f"The {kind} model specification is incomplete.")
        if any(
            not isinstance(value[key], str) or not value[key].strip()
            for key in ("name", "version", "deployment", "sku")
        ):
            raise PreflightError(f"The {kind} model name, version, deployment and SKU must be nonempty strings.")
        if type(value["capacity"]) is not int or value["capacity"] < 1:
            raise PreflightError(f"The {kind} deployment capacity must be a positive integer.")
        if value["sku"] != "GlobalStandard" or "preview" in value["name"].lower():
            raise PreflightError(f"The {kind} model must use a stable Azure OpenAI model and GlobalStandard SKU.")
        models[kind] = ModelSpec(**value)
    if len({model.deployment for model in models.values()}) != len(models):
        raise PreflightError("Text, realtime and transcription must have distinct deployment names.")
    if not isinstance(data["voice"], str) or not data["voice"].strip():
        raise PreflightError("The realtime voice must be a nonempty string.")
    return models


def local_path(root: Path, value: str, default: str) -> Path:
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else root / path


def validate_local(root: Path, env: Mapping[str, str]) -> dict[str, ModelSpec]:
    models = load_models(root)
    for relative in ("infra/main.bicep", "infra/resources.bicep", "infra/main.parameters.json", "azure.yaml"):
        if not (root / relative).is_file():
            raise PreflightError(f"Missing deployment input: {relative}. Restore it before deployment.")
    profile_path = local_path(root, env.get("PROFILE_PATH", ""), "profile.json")
    if not isinstance(read_json(profile_path), dict):
        raise PreflightError(f"{profile_path} must contain a profile JSON object.")
    knowledge_path = local_path(root, env.get("KNOWLEDGE_DATA_DIR", ""), "data/work")
    try:
        sources = sorted(knowledge_path.glob("*.md"))
        if not sources:
            raise PreflightError(f"No top-level markdown in {knowledge_path}. Add UTF-8 knowledge before deployment.")
        for source in sources:
            if not source.read_text(encoding="utf-8").strip():
                raise PreflightError(f"Knowledge source {source} is empty. Populate or remove it before deployment.")
    except (OSError, UnicodeError) as exc:
        raise PreflightError(f"Knowledge sources must be readable UTF-8 markdown: {exc}") from exc
    return models


def run_az_json(arguments: Sequence[str]) -> Any:
    command = ["az", *arguments, "--output", "json", "--only-show-errors"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    except FileNotFoundError as exc:
        raise PreflightError(
            "Azure CLI is missing. Install it and sign in explicitly before running cloud checks."
        ) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError(f"Azure CLI read-only check failed: {exc}. Verify connectivity and retry.") from exc
    if result.returncode:
        detail = result.stderr.strip()[-2000:] or "Azure CLI returned no error details."
        raise PreflightError(f"Azure CLI read-only check failed: {detail}")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise PreflightError(
            "Azure CLI returned invalid JSON. Update the CLI and retry the read-only preflight."
        ) from exc


def arm_values(path: str, subscription: str, runner: AzureRunner) -> list[dict[str, Any]]:
    prefix = f"{ARM_ENDPOINT}/subscriptions/{subscription}/"
    url = f"{ARM_ENDPOINT}{path}"
    seen: set[str] = set()
    values = []
    while url:
        if not url.startswith(prefix) or url in seen or len(seen) >= 100:
            raise PreflightError("Azure returned an unsafe, repeated or excessive pagination link; preflight stopped.")
        seen.add(url)
        page = runner(["rest", "--method", "GET", "--url", url, "--subscription", subscription])
        if not isinstance(page, dict) or not isinstance(page.get("value"), list):
            raise PreflightError("Azure returned an invalid list response; availability cannot be verified.")
        if any(not isinstance(item, dict) for item in page["value"]):
            raise PreflightError("Azure returned an invalid list item; availability cannot be verified.")
        values.extend(page["value"])
        url = page.get("nextLink") or ""
        if not isinstance(url, str):
            raise PreflightError("Azure returned an invalid pagination link; preflight stopped.")
    return values


def valid_uuid(value: str, name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise PreflightError(f"{name} must be a UUID. Set it with `azd env set {name} <uuid>`.") from exc


def object_value(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PreflightError(f"Azure returned invalid {label}; the deployment prerequisite cannot be verified.")
    return value


def object_list(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise PreflightError(f"Azure returned invalid {label}; the deployment prerequisite cannot be verified.")
    return value


def permission_allows(permission: Mapping[str, Any], action: str) -> bool:
    def matches(patterns: Sequence[str]) -> bool:
        if not isinstance(patterns, list) or any(not isinstance(pattern, str) for pattern in patterns):
            raise PreflightError("Azure returned invalid ARM permissions; have an administrator verify access.")
        return any(fnmatch.fnmatchcase(action.lower(), pattern.lower()) for pattern in patterns)

    return matches(permission.get("actions", [])) and not matches(permission.get("notActions", []))


def check_permissions(subscription: str, runner: AzureRunner) -> None:
    permissions = arm_values(
        f"/subscriptions/{subscription}/providers/Microsoft.Authorization/permissions?api-version=2022-04-01",
        subscription,
        runner,
    )
    missing = [action for action in REQUIRED_ACTIONS if not any(permission_allows(p, action) for p in permissions)]
    if missing:
        raise PreflightError(
            "Deployment principal lacks subscription-scope permissions: "
            + ", ".join(missing)
            + ". Ask an administrator for Contributor plus Role Based Access Control Administrator "
            "at the subscription (or equivalent least-privilege permissions), then retry. "
            "Conditional assignments, deny assignments and Azure Policy still need operator review."
        )


def normalize_location(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def check_providers(subscription: str, location: str, runner: AzureRunner) -> None:
    for namespace, types in PROVIDER_TYPES.items():
        provider = runner(["provider", "show", "--namespace", namespace, "--subscription", subscription])
        if not isinstance(provider, dict) or provider.get("registrationState") != "Registered":
            raise PreflightError(
                f"{namespace} is not registered. Have an administrator run "
                f"`az provider register --namespace {namespace} --subscription {subscription}` and wait for Registered."
            )
        resource_types = object_list(provider.get("resourceTypes", []), "provider resource types")
        for resource_type in types:
            matching = [
                item for item in resource_types if item.get("resourceType", "").lower() == resource_type.lower()
            ]
            locations: list[str] = []
            for item in matching:
                regions = item.get("locations", [])
                if not isinstance(regions, list) or any(not isinstance(region, str) for region in regions):
                    raise PreflightError(f"Azure returned invalid locations for {namespace}/{resource_type}.")
                locations.extend(normalize_location(region) for region in regions)
            if location not in locations:
                raise PreflightError(
                    f"{namespace}/{resource_type} is not advertised in {location}. "
                    "Select a region supporting Container Apps, Cosmos and all required models "
                    "(start with eastus2), set AZURE_LOCATION, and rerun preflight."
                )


def number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PreflightError(f"Azure returned invalid {label}; quota cannot be verified.")
    return float(value)


def check_model_capacity(spec: ModelSpec, sku: Mapping[str, Any]) -> None:
    capacity = object_value(sku.get("capacity") or {}, "model capacity")
    minimum = number(capacity.get("minimum", 1), "model capacity minimum")
    maximum = number(capacity.get("maximum", spec.capacity), "model capacity maximum")
    step = number(capacity.get("step", 1), "model capacity step")
    if step <= 0 or not minimum <= spec.capacity <= maximum or (spec.capacity - minimum) % step != 0:
        raise PreflightError(
            f"{spec.name} requires capacity {minimum:g}..{maximum:g} in increments of {step:g}; "
            f"the template requests {spec.capacity}. Review infra/deployment-models.json and quota before deployment."
        )


def existing_capacity(spec: ModelSpec, deployments: Sequence[Mapping[str, Any]]) -> float:
    for deployment in deployments:
        properties = object_value(deployment.get("properties") or {}, "existing deployment properties")
        model = object_value(properties.get("model") or {}, "existing deployment model")
        sku = object_value(deployment.get("sku") or {}, "existing deployment SKU")
        if (
            deployment.get("name") == spec.deployment
            and model.get("name") == spec.name
            and model.get("version") == spec.version
            and sku.get("name") == spec.sku
        ):
            return max(0, number(sku.get("capacity"), "existing deployment capacity"))
    return 0


def check_models(
    models: Mapping[str, ModelSpec],
    available: Sequence[Mapping[str, Any]],
    usages: Sequence[Mapping[str, Any]],
    location: str,
    deployments: Sequence[Mapping[str, Any]] = (),
) -> None:
    required: dict[str, float] = {}
    catalog = [
        object_value(item.get("model") or {}, "model catalog") for item in available if item.get("kind") == "OpenAI"
    ]
    for kind, spec in models.items():
        matches = [
            model for model in catalog if model.get("name") == spec.name and model.get("version") == spec.version
        ]
        skus = [
            sku
            for model in matches
            for sku in object_list(model.get("skus", []), "model SKUs")
            if sku.get("name") == spec.sku
        ]
        if not matches or not skus:
            raise PreflightError(
                f"Mandatory {kind} model {spec.name} ({spec.version}, {spec.sku}) is unavailable "
                f"to this subscription in {location}. Select a supported region (start with eastus2), "
                "request model access if required, and rerun. Text-only deployment is not supported."
            )
        for model in matches:
            if str(model.get("lifecycleStatus") or "").lower() in {"preview", "deprecated"}:
                raise PreflightError(
                    f"{spec.name} is not a supported stable model in {location}; review the model pin."
                )
            retirement = object_value(model.get("deprecation") or {}, "model deprecation").get("inference")
            if retirement:
                try:
                    if not isinstance(retirement, str):
                        raise ValueError("Retirement date is not a string.")
                    retired = datetime.fromisoformat(retirement.replace("Z", "+00:00"))
                    if retired.tzinfo is None:
                        retired = retired.replace(tzinfo=UTC)
                except (ValueError, TypeError) as exc:
                    raise PreflightError(
                        f"Cannot verify {spec.name} retirement date; review model availability."
                    ) from exc
                if retired <= datetime.now(UTC):
                    raise PreflightError(
                        f"{spec.name} inference has retired; update the pinned model before deployment."
                    )
        sku = skus[0]
        check_model_capacity(spec, sku)
        usage_name = sku.get("usageName")
        if not isinstance(usage_name, str) or not usage_name:
            raise PreflightError(
                f"Azure did not return a quota usageName for {spec.name}; verify its quota in the portal."
            )
        required[usage_name] = required.get(usage_name, 0) + max(
            0, spec.capacity - existing_capacity(spec, deployments)
        )
    for usage_name, needed in required.items():
        matching = [
            usage for usage in usages if object_value(usage.get("name") or {}, "quota name").get("value") == usage_name
        ]
        if len(matching) != 1:
            raise PreflightError(f"Cannot find unambiguous quota {usage_name} in {location}. Request quota and retry.")
        usage = matching[0]
        limit = number(usage.get("limit"), "quota limit")
        current = number(usage.get("currentValue"), "quota usage")
        if min(limit, current) < 0:
            raise PreflightError("Azure returned negative quota values; quota cannot be verified.")
        available_capacity = limit - current
        if usage.get("status") in {"Blocked", "InOverage", "Unknown"} or available_capacity < needed:
            raise PreflightError(
                f"Insufficient quota for {usage_name} in {location}: {available_capacity:g} available, "
                f"{needed:g} additional capacity required. Request quota or select another supported region. "
                "No resources were provisioned and no fallback model was selected."
            )


def validate_cloud(models: Mapping[str, ModelSpec], env: Mapping[str, str], runner: AzureRunner = run_az_json) -> None:
    environment = env.get("AZURE_ENV_NAME", "")
    location = env.get("AZURE_LOCATION", "").strip().lower()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-]{0,31}", environment):
        raise PreflightError("Set AZURE_ENV_NAME to an azd environment name of 1..32 letters, digits or hyphens.")
    if not re.fullmatch(r"[a-z][a-z0-9]+", location):
        raise PreflightError("Set AZURE_LOCATION to a region name, for example `azd env set AZURE_LOCATION eastus2`.")
    subscription = env.get("AZURE_SUBSCRIPTION_ID", "")
    arguments = ["account", "show"]
    if subscription:
        subscription = valid_uuid(subscription, "AZURE_SUBSCRIPTION_ID")
        arguments.extend(["--subscription", subscription])
    account = runner(arguments)
    if not isinstance(account, dict) or account.get("state") != "Enabled":
        raise PreflightError("No enabled Azure subscription is selected. Sign in explicitly and select a subscription.")
    if account.get("environmentName") != "AzureCloud":
        raise PreflightError("This template supports the Azure public cloud only. Select an AzureCloud subscription.")
    subscription = valid_uuid(account.get("id", ""), "AZURE_SUBSCRIPTION_ID")
    tenant = valid_uuid(account.get("tenantId", ""), "AZURE_TENANT_ID")
    if env.get("AZURE_TENANT_ID") and valid_uuid(env["AZURE_TENANT_ID"], "AZURE_TENANT_ID") != tenant:
        raise PreflightError(
            "AZURE_TENANT_ID differs from the selected subscription tenant. Select matching owner auth."
        )
    for name in ("AZURE_CLIENT_APP_ID", "ADMIN_USER_OID"):
        if env.get(name):
            valid_uuid(env[name], name)
        elif account.get("user", {}).get("type", "").lower() != "user":
            raise PreflightError(
                f"{name} is required for a service-principal deployment. Complete owner SPA setup with a human "
                "account first and set the resulting client ID and owner user object ID. Never use the CI principal OID."
            )
    check_permissions(subscription, runner)
    check_providers(subscription, location, runner)
    base = f"/subscriptions/{subscription}/providers/Microsoft.CognitiveServices/locations/{location}"
    available = arm_values(f"{base}/models?api-version={COGNITIVE_API_VERSION}", subscription, runner)
    usages = arm_values(f"{base}/usages?api-version={COGNITIVE_API_VERSION}", subscription, runner)
    deployments = []
    if env.get("AZURE_OPENAI_SERVICE") and env.get("AZURE_RESOURCE_GROUP"):
        group = quote(env["AZURE_RESOURCE_GROUP"], safe="")
        service = quote(env["AZURE_OPENAI_SERVICE"], safe="")
        account_path = (
            f"/subscriptions/{subscription}/resourceGroups/{group}/providers/Microsoft.CognitiveServices/"
            f"accounts/{service}"
        )
        metadata = object_value(
            runner(
                [
                    "rest",
                    "--method",
                    "GET",
                    "--url",
                    f"{ARM_ENDPOINT}{account_path}?api-version={COGNITIVE_API_VERSION}",
                    "--subscription",
                    subscription,
                ]
            ),
            "existing OpenAI account",
        )
        tags = object_value(metadata.get("tags") or {}, "existing OpenAI tags")
        if tags.get("application") == "persona-assistant" and tags.get("azd-env-name") == environment:
            if normalize_location(str(metadata.get("location", ""))) != location:
                raise PreflightError(
                    "The existing environment uses a different OpenAI region. Create a new azd environment "
                    "when changing regions; existing allocations cannot satisfy the selected region's quota."
                )
            deployments = arm_values(
                f"{account_path}/deployments?api-version={COGNITIVE_API_VERSION}", subscription, runner
            )
    check_models(models, available, usages, location, deployments)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Persona Assistant deployment inputs. By default performs read-only Azure CLI "
            "checks for the selected subscription, region, permissions, mandatory models and quota. "
            "It never signs in, registers providers, builds an image or provisions resources."
        ),
        epilog=(
            "Before cloud checks, sign in explicitly with Azure CLI and select the same subscription as azd. "
            "Use azd env values AZURE_ENV_NAME, AZURE_LOCATION (start with eastus2), AZURE_SUBSCRIPTION_ID "
            "and AZURE_TENANT_ID. CI also requires AZURE_CLIENT_APP_ID and ADMIN_USER_OID from owner setup. "
            "Entra app-registration/redirect permissions are checked separately by the owner-auth hooks. "
            "A passing check does not reserve capacity, bypass Azure Policy or guarantee deployment or cost."
        ),
    )
    parser.add_argument(
        "--validate-local",
        action="store_true",
        help="Offline only: check model pins, profile JSON and markdown sources; no Azure CLI, staging or image builds.",
    )
    args = parser.parse_args(argv)
    try:
        models = validate_local(Path(__file__).resolve().parents[1], os.environ)
        if args.validate_local:
            print("Local deployment inputs valid. No Azure calls, packaging, image builds or provisioning performed.")
        else:
            validate_cloud(models, os.environ)
            print(
                "Read-only Azure preflight passed. Capacity is not reserved; costs, Azure Policy, RBAC conditions "
                "and Entra app-registration/redirect permissions still require operator review."
            )
        return 0
    except PreflightError as exc:
        print(f"Deployment preflight failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
