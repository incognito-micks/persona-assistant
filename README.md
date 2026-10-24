# Persona Assistant

Your story, grounded text chat, and realtime voice. A single-owner AI portfolio template built with React, TypeScript, Quart, and Azure OpenAI.

Visitors can chat or start a voice conversation without creating an account. The microphone starts only after an explicit action and browser permission. Owner-only administration is separate from public visitor access.

The included Alex Morgan profile and projects are fictional. Replace them with content you have permission to publish; do not treat the example as a real person's resume.

## What You Get

- Text chat, realtime voice, and recognized input-speech text, enabled by default.
- One shared, validated profile for branding and both conversation modes.
- Markdown knowledge supplied directly to the model: no Search index, embeddings, ingestion service, or document ACLs.
- Browser-local visitor history and owner-authorized settings/operational analytics.
- An opinionated Container Apps deployment with managed identity, text/realtime/transcription model deployments, and Cosmos settings/shared usage storage.
- Bounded public usage, explicit failures, and a deployment kill switch.

This project descends from [Azure Search OpenAI Demo](https://github.com/Azure-Samples/azure-search-openai-demo). It is an independent adaptation, not a Microsoft-operated service. Preserve the upstream [MIT license](LICENSE) when redistributing it.

The MIT license covers this project's source, not every external component or service it uses. Third-party dependencies and assets retain their own licenses, and Azure services/models remain subject to their service terms and charges. Review those terms before redistributing bundled components or container images.

## Personalize

1. Edit [profile.json](profile.json): owner and assistant names, introduction, tone, images, optional contact links, and example questions.
2. Replace the fictional markdown under [data/work](data/work).
3. Put redistributable images in [app/frontend/public](app/frontend/public) and reference site-relative URLs from the profile.
4. Restart locally, or rebuild/redeploy your application package.

No Python or TypeScript edits are needed to change persona. Empty contact links are hidden. Settings precedence is built-in neutral defaults, profile file, then persisted owner overrides. Changing the profile file does not overwrite fields already saved in Cosmos.

For private source files outside the checkout, set `PROFILE_PATH` and `KNOWLEDGE_DATA_DIR` when preparing the package. They select build inputs; they are not container filesystem paths. Never commit a private profile, credentials, or private knowledge. See [personalization and packaging](docs/personalization-and-packaging.md).

## Local Development

Use Python 3.10 or newer and Node.js 20.19 or newer (22 LTS recommended); see the [tested compatibility scope](#tested-compatibility) before choosing another runtime. Live development and deployment also need [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli) (`az`) and [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) (`azd`) 1.37.1 or newer. Docker is used for local container validation.

All shell examples use Bash and start at the repository root. Create and activate the repository virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.get_encoding('o200k_base')"
npm ci --prefix app/frontend
```

The tokenizer warm-up downloads public vocabulary tables, not application data. Container builds cache these tables; network-restricted non-container environments must also prepare the cache.

If Bicep is installed, restore its public module dependencies once before running the offline infrastructure tests: `az bicep restore --file infra/main.bicep`. This downloads published modules, not application data, and does not provision resources. The tests deliberately compile with `--no-restore`; fresh CI runners perform this bootstrap explicitly.

For live development, copy [.env.example](.env.example) to an ignored root `.env` without overwriting any existing configuration. Supply your model service, tenant, and actual deployment names. For a model resource provisioned by this template, the deployment names are:

```dotenv
AZURE_OPENAI_SERVICE=your-model-service
AZURE_TENANT_ID=your-tenant-id
AZURE_OPENAI_CHATGPT_DEPLOYMENT=text
AZURE_OPENAI_REALTIME_DEPLOYMENT=realtime
AZURE_OPENAI_TRANSCRIPTION_DEPLOYMENT=transcription
```

Authenticate the human development identity with `azd auth login --tenant-id "<your-tenant-id>"`. The backend uses `AzureDeveloperCliCredential` locally; that identity needs model data access, such as the **Cognitive Services OpenAI User** role on the model resource. The hosted managed identity's role does not automatically grant access to your local identity.

Live requests use paid model services. Unit and mocked browser tests do not. Local public chat can run without owner login; see [owner access](docs/operations.md#owner-access) to enable local administration.

The VS Code `Development` task runs Vite and Quart. Alternatively:

```sh
# Terminal 1, from the repository root
source .venv/bin/activate
(cd app/backend && python -m quart --app main:app run --port 50505)
```

```sh
# Terminal 2, from the repository root
npm run dev --prefix app/frontend
```

The frontend is normally at `http://localhost:5173`. On Windows, activate `.venv\Scripts\Activate.ps1` and use the equivalent PowerShell commands, or use the supplied VS Code tasks.

## Deploy To Azure

Deployment creates billable resources. Review the defaults, permissions, usage limits, and selected region before running cloud commands.

Use Azure Developer CLI (`azd`) 1.37.1 or newer. Older clients can fail the Container Registry remote-build upload with `InvalidCorrelationRequestId`; the manifest enforces the version validated by this template.

Choose a new environment name rather than reusing an existing application's environment. Replace the two IDs below with the authorized subscription and its tenant:

```sh
ENV_NAME=persona-example
SUBSCRIPTION_ID="<your-subscription-id>"
TENANT_ID="<your-tenant-id>"
LOCATION=eastus2

az login --tenant "$TENANT_ID"
azd auth login --tenant-id "$TENANT_ID"
azd env new "$ENV_NAME" --subscription "$SUBSCRIPTION_ID" --location "$LOCATION"
azd env set AZURE_TENANT_ID "$TENANT_ID" --environment "$ENV_NAME"
azd up --environment "$ENV_NAME"
```

Both sign-ins matter: preflight reads Azure through `az`, while owner setup and local model credentials use `azd`. They can hold different accounts; use the same intended tenant and subscription throughout.

The supported path is Container Apps with text, realtime, and input-transcription deployments, not a matrix of hosting/providers/features. The owner setup hook creates one single-tenant SPA registration when directory permissions allow it. Otherwise, complete the [manual owner setup](docs/operations.md#owner-access) before provisioning. Resource identifiers and service endpoints are wired by infrastructure.

Preflight checks must succeed for all three model capabilities; voice or transcription is not silently omitted. Model availability, quota, directory-registration permissions, and role-assignment permissions remain subscription/tenant prerequisites.

The deploying identity needs resource-creation and role-assignment permissions; directory app-registration permissions are separate from subscription permissions. Preflight checks do not reserve model capacity or override Azure Policy.

See [architecture](docs/architecture.md) and [operations](docs/operations.md) for updates, default usage limits, and safe teardown. Always pass the intended `--environment`; deployment commands contact Azure and are not offline checks.

This streamlined template is not an automatic infrastructure migration for an existing upstream deployment. Validate it in a new environment first, back up owner settings, and review any old resources separately before an authorized teardown.

## Validation

```sh
source .venv/bin/activate
python -m pytest tests
python -m ruff check .
python -m black . --check
(cd app/backend && mypy . --config-file=../../pyproject.toml)
(cd scripts && mypy . --config-file=../pyproject.toml)
```

```sh
npm run build --prefix app/frontend
source .venv/bin/activate
python -m playwright install chromium
python -m pytest tests/e2e.py
```

Browser tests use built assets and mocked services; install the Playwright Chromium browser first. They are not live Azure or real-microphone validation. For supported contribution checks and type checking of scripts, see [CONTRIBUTING.md](CONTRIBUTING.md).

### Tested Compatibility

The [CI workflow](.github/workflows/python-test.yaml) defines the repeatable test matrix:

| Surface | Validated scope |
| --- | --- |
| Python CI | 3.10, 3.11, 3.12, 3.13, and 3.14 |
| Node.js CI | 20.19 and 22 |
| Linux CI | Build, translations, lint, types, formatting, unit/API coverage, and Chromium browser tests |
| Windows CI | Build, translations, lint, types, and formatting; Python test suites and browser workflows are not run |
| Hosted container | Linux AMD64 with the Python 3.13 base image |
| Automated browser | Chromium with mocked service workflows |

These are tested combinations, not a promise that every newer runtime, browser, mobile device, or operating system works. In particular, Chromium automation does not establish Safari/Firefox support or replace a real microphone and hosted-service check.

## Privacy And Limits

- The knowledge set is shared context for every visitor. Publish only information suitable for that audience; owner login does not create document-level permissions.
- Profile settings can also become model context. A field omitted from `/api/status` is not a confidential vault; do not configure private contact details or secrets that visitors must never receive.
- The assistant identifies itself as AI, does not promise actions for the owner, and should decline unsupported factual questions.
- Public operational analytics omit raw visitor questions, owner identities, and detailed error messages. Anonymous sessions are not verified unique people.
- A 30-day analytics report window is not a data-retention policy. Existing records are not automatically deleted or migrated.
- Browser history remains on the visitor's device. Model providers process submitted text/audio under the deployment's service terms. Do not submit sensitive information.
- Voice input is transcribed by the configured Azure model and displayed alongside AI replies. Recognition can be inaccurate. Voice transcripts stay in the page's session state, not the template's saved text-chat history; provider-side processing and retention are separate responsibilities.
- Chat trims complete older turns from the model request to fit its token budget, without truncating saved browser history. Mandatory knowledge/latest-question overflow fails explicitly.
- Public admission has per-identity and deployment-wide [usage limits](docs/operations.md#default-usage-limits). Hosted startup requires shared Cosmos settings/usage storage, and configured store failures do not bypass limits. Local development fallback limits apply only to one process.
- Deploy `DISABLE_PUBLIC_ACCESS=true` to stop new public admissions. Changing a local environment value alone does not update the hosted app; see the [kill switch](docs/operations.md#public-access-kill-switch). Billing alerts are not hard spending limits.
- Stronger gateway controls, trusted proxy configuration, retention policies, incident response, and production validation remain operator responsibilities.

Read [SECURITY.md](SECURITY.md) before public exposure.

## Publishing Your Fork

Use a neutral profile and sample assets, run local secret/content checks, review the final staged tree and history, and preserve upstream license notices. Review the [release checklist](docs/operations.md#before-a-public-release). GitHub private vulnerability reporting is recommended when the repository becomes public; publishing a personal email address is not required. No directory name or repository name grants permission to publish private content.

The template works from a folder of any name, including paths with spaces. `persona-assistant` is the recommended clone directory; renaming an open workspace is not required.
