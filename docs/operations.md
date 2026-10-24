# Operations

Use Azure CLI (`az`) and Azure Developer CLI (`azd`) 1.37.1 or newer. Start with the [deployment quickstart](../README.md#deploy-to-azure). The examples below use Bash from the repository root.

Always name the environment explicitly. `azd env new` changes the local default, and different `az` and `azd` sessions can hold different identities. Never assume a previously selected environment or subscription is the intended target.

## Before A Public Release

1. Use fictional example sources/media and review publication rights.
2. Review final staged files and any published history, not only the working tree.
3. Run a local credential/content scan; configure available repository secret scanning and enable GitHub private vulnerability reporting when the repository becomes public.
4. Preserve upstream license notices and publish maintainer/contribution policies.
5. Validate clean dependency installation, tokenizer preparation, frontend build, unit/API tests, browser tests, and the actual container image.
6. Perform an explicitly authorized fresh deployment with a supported region and sufficient text, realtime, and input-transcription quota.
7. Verify real microphone permissions, audio, recognized speech, interruption, disconnect, restart, owner authorization, readable administration, and usage-limit exhaustion.
8. Have a second person configure another fictional profile without changing application code.

The tests and GitHub's community-profile score do not constitute a privacy, legal, or production-security certification. The MIT license does not require a personal email address; see [security reporting](../SECURITY.md) and the [code of conduct](../.github/CODE_OF_CONDUCT.md) for GitHub-based reporting options.

## Owner Access

Public visitors do not sign in. The supported hosted deployment nevertheless needs one configured human owner for administration.

The preprovision hook tries to create a single-tenant Entra SPA application and identify the delegated human owner. Resource-creation and role-assignment permissions in the subscription do not themselves grant directory app-registration permissions.

If automatic owner setup is unavailable:

1. In the intended tenant's **App registrations**, create or select an application restricted to that tenant.
2. Configure the **Single-page application** platform with `http://localhost:50505/redirect`, `http://localhost:5173/redirect`, and the deployed HTTPS origin followed by `/redirect`. Add the hosted URI once the deployment provides it.
3. Record the application **client ID** and the human owner's **user object ID** in that tenant. The owner ID is not the app registration object ID or the CI service principal ID.
4. Set the values for the intended environment and retry deployment:

```sh
ENV_NAME=persona-example
azd env set AZURE_TENANT_ID "<your-tenant-id>" --environment "$ENV_NAME"
azd env set AZURE_CLIENT_APP_ID "<your-spa-client-id>" --environment "$ENV_NAME"
azd env set ADMIN_USER_OID "<your-human-user-object-id>" --environment "$ENV_NAME"
azd up --environment "$ENV_NAME"
```

Do not add a client secret, Search permissions, exposed API scopes, or broad Graph permissions to the owner SPA. Do not manually mark an externally supplied application as template-managed. The hooks preserve external registrations, so their redirect changes remain your responsibility.

After deployment, open `/#/admin` on your application origin. The backend validates the configured tenant/application and requires the exact owner user ID. Missing or incorrect configuration must not open administration to other users.

For local administration, put the same three owner values in the ignored root `.env`. Local public chat can run without them. CI deployment uses a federation identity separately; it must not become the application owner.

## Deploy, Update, And Recover

Keep the environment name, tenant, subscription, and region stable for updates. Confirm the target before invoking any command that changes resources:

```sh
ENV_NAME=persona-example
azd env get-value AZURE_SUBSCRIPTION_ID --environment "$ENV_NAME"
azd env get-value AZURE_RESOURCE_GROUP --environment "$ENV_NAME"
```

For an application-only update:

```sh
azd deploy backend --environment "$ENV_NAME"
```

For infrastructure or runtime-setting changes, provision first and then deploy application changes if needed:

```sh
azd provision --environment "$ENV_NAME"
azd deploy backend --environment "$ENV_NAME"
```

These are billable cloud operations, not offline validation. Provisioning preserves the deployed image through azd's generated `SERVICE_BACKEND_IMAGE_NAME` state. Keep that environment metadata; deleting it before reprovisioning can select the bootstrap image instead. Do not commit `.azure` environment files or share their full contents in issues.

Back up owner settings before migrations. Changing the profile file does not overwrite already-persisted owner fields. If a deployment fails, inspect its actual resource state before retrying; some resources or the dedicated directory application may already exist.

Existing upstream deployments should first be evaluated in a new environment. Removing a resource from an incremental template does not guarantee deletion of an old billable resource. Review old resources, redirect URIs, and saved profile overrides explicitly; source-code cleanup is not a cloud or data migration.

## Costs And Abuse

Voice is a core default and creates paid model usage. Public access therefore needs shared admission limits, bounded sessions/concurrency, application-enforced quotas, monitoring, and an incident response plan.

### Default Usage Limits

The defaults are implemented in the [request limiter](../app/backend/core/ratelimiter.py), [application setup](../app/backend/app.py), and [voice admission service](../app/backend/core/voiceusage.py):

| Limit | Default |
| --- | --- |
| Public model-route/session admissions per identity or anonymous address | 20 per rolling 60 seconds |
| Deployment-wide public admissions | 200 per rolling 24 hours |
| Voice sessions | 20 per UTC calendar day |
| Concurrent voice sessions | 3 |
| Duration of one voice session | 300 seconds |
| Idle audio connection timeout | 60 seconds |

These limit usage, not currency. Model prices, input size, output, transcription, and active audio affect actual charges. Azure budgets and alerts do not automatically stop requests or enforce a hard spending cap.

Anonymous address limits can group visitors behind shared networks and do not identify verified people. Hosted deployments require shared Cosmos admission storage; local process-only fallback limits are not replica-global. Never bypass a configured store's failures.

Container Apps can scale to zero, but that does not eliminate registry, stored-data, or other service charges. Plan for model usage, Container Registry, Cosmos storage/requests, and any operational services you enable. Review Azure Cost Management rather than assuming an idle or disabled app is free.

Apply request/connection limits at a trusted gateway, validate proxy behavior, and limit network egress.

## Public Access Kill Switch

To stop new public model admissions through the deployment configuration:

```sh
ENV_NAME=persona-example
azd env set DISABLE_PUBLIC_ACCESS true --environment "$ENV_NAME"
azd provision --environment "$ENV_NAME"
```

Changing a local azd value alone does not change the running application. The switch applies when the updated runtime configuration is deployed. It does not retroactively erase records or instantly terminate already-running requests or voice sessions. Use hosting ingress/revision controls if an incident requires immediate termination.

To restore access, set the value to `false` for the same environment and apply provisioning again. A kill switch does not remove resources or stop every background service charge.

## Data

Publish a privacy notice describing browser-local history, model processing, and your configured operational telemetry. Choose and implement a retention/deletion policy appropriate to your deployment. The report's 30-day window does not delete stored records; existing analytics are not backfilled or automatically erased.

Do not enable prompt-content instrumentation or audio recording by default. Review provider-side logging/retention separately. Limit administrative access and review backups, exports, and diagnostic logs.

## Safe Teardown

Teardown deletes the deployment's Azure resources, including durable settings and analytics. Export any data you need first. Confirm the selected subscription and generated resource group, and record whether the owner application is template-managed:

```sh
ENV_NAME=persona-example
azd env get-value AZURE_SUBSCRIPTION_ID --environment "$ENV_NAME"
azd env get-value AZURE_RESOURCE_GROUP --environment "$ENV_NAME"
azd env get-value AZURE_CLIENT_APP_ID --environment "$ENV_NAME"
azd env get-value AZURE_AUTH_MANAGED_CLIENT_APP_ID --environment "$ENV_NAME"
```

The last value is absent for externally supplied owner applications. Its absence is not permission to delete one. Do not post the full environment file or its values in a public issue.

Only after confirming the target, run the interactive teardown:

```sh
azd down --environment "$ENV_NAME"
```

Read the resource-deletion confirmation. Do not add `--force`, `--purge`, or `--no-prompt` by default; provider soft-delete and purge decisions deserve separate review.

The owner SPA is a directory object, not a resource inside the Azure resource group. After resource teardown, use **Entra ID > App registrations** to locate the recorded template-managed client ID. Verify that it is not used by another environment before separately deleting it. Never delete an externally supplied or shared registration as part of template cleanup.

Verify that the intended resource group has been removed and review residual charges/soft-deleted resources in Azure. Teardown does not delete the local application source or automatically clear a visitor's browser-local history.

No production/cloud provisioning should be run by a coding agent without explicit authorization for the target subscription and costs.
