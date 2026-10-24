# Persona Assistant

Your story, grounded text chat, and realtime voice. A single-owner AI portfolio template built with React, TypeScript, Quart, and Azure OpenAI.

Visitors can chat or start a voice conversation without creating an account. The microphone starts only after an explicit action and browser permission. Owner-only administration is separate from public visitor access.

The included Alex Morgan profile and projects are fictional. Replace them with content you have permission to publish; do not treat the example as a real person's resume.

## What You Get

- Text chat and realtime voice, enabled by default.
- One shared, validated profile for branding and both conversation modes.
- Markdown knowledge supplied directly to the model: no Search index, embeddings, ingestion service, or document ACLs.
- Browser-local visitor history and owner-authorized settings/operational analytics.
- An opinionated Container Apps deployment with managed identity, text/realtime model deployments, and Cosmos settings/shared usage storage.
- Bounded public usage, explicit failures, and a deployment kill switch.

This project descends from [Azure Search OpenAI Demo](https://github.com/Azure-Samples/azure-search-openai-demo). It is an independent adaptation, not a Microsoft-operated service. Preserve the upstream [MIT license](LICENSE) when redistributing it.

## Personalize

1. Edit [profile.json](profile.json): owner and assistant names, introduction, tone, images, optional contact links, and example questions.
2. Replace the fictional markdown under [data/work](data/work).
3. Put redistributable images in [app/frontend/public](app/frontend/public) and reference site-relative URLs from the profile.
4. Restart locally, or rebuild/redeploy your application package.

No Python or TypeScript edits are needed to change persona. Empty contact links are hidden. Settings precedence is built-in neutral defaults, profile file, then persisted owner overrides. Changing the profile file does not overwrite fields already saved in Cosmos.

For private source files outside the checkout, set `PROFILE_PATH` and `KNOWLEDGE_DATA_DIR` when preparing the package. They select build inputs; they are not container filesystem paths. Never commit a private profile, credentials, or private knowledge. See [knowledge and packaging](docs/data_ingestion.md).

## Local Development

Use Python 3.10 or newer, Node.js 20.19 or newer (22 LTS recommended), and the repository virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -c "import tiktoken; tiktoken.get_encoding('cl100k_base'); tiktoken.get_encoding('o200k_base')"
npm ci --prefix app/frontend
```

The tokenizer warm-up downloads public vocabulary tables, not application data. Container builds cache these tables; network-restricted non-container environments must also prepare the cache.

If Bicep is installed, restore its public module dependencies once before running the offline infrastructure tests: `az bicep restore --file infra/main.bicep`. This downloads published modules, not application data, and does not provision resources. The tests deliberately compile with `--no-restore`; fresh CI runners perform this bootstrap explicitly.

For live chat, copy [.env.example](.env.example) to an ignored root `.env` and supply your model service. Authenticate your local Azure development credential. Live requests use paid model services; unit and mocked browser tests do not.

The VS Code `Development` task runs Vite and Quart. Alternatively:

```sh
# Terminal 1, from the repository root
source .venv/bin/activate
cd app/backend
python -m quart --app main:app run --port 50505
```

```sh
# Terminal 2, from the repository root
npm run dev --prefix app/frontend
```

The frontend is normally at `http://localhost:5173`. Public chat does not require an owner registration. Administration remains unavailable until owner authentication is configured.

## Deploy To Azure

Deployment creates billable resources. Review the defaults, permissions, usage limits, and selected region before running cloud commands.

```sh
azd auth login
azd up
```

The supported path is Container Apps with one text model and one realtime model, not a matrix of hosting/providers/features. Select an environment name and subscription/region, and complete the owner-authentication setup. Resource identifiers and service endpoints are wired by infrastructure.

Preflight checks must succeed for both model capabilities; voice is not silently omitted. Model availability, quota, directory-registration permissions, and role-assignment permissions remain subscription/tenant prerequisites.

See [architecture](docs/architecture.md) and [production operation](docs/productionizing.md). Changes to infrastructure require `azd provision`; application-only updates use `azd deploy`. These commands contact Azure and are not offline checks.

This streamlined template is not an automatic infrastructure migration for an existing upstream deployment. Validate it in a new environment first, back up owner settings, and review any old resources separately before an authorized teardown.

## Validation

```sh
source .venv/bin/activate
python -m pytest tests
python -m ruff check .
python -m black . --check
cd app/backend && mypy . --config-file=../../pyproject.toml
```

```sh
npm run build --prefix app/frontend
source .venv/bin/activate
python -m pytest tests/e2e.py
```

Browser tests use built assets and mocked services; install the Playwright Chromium browser first. They are not live Azure or real-microphone validation. For supported contribution checks and type checking of scripts, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Privacy And Limits

- The knowledge set is shared context for every visitor. Publish only information suitable for that audience; owner login does not create document-level permissions.
- Profile settings can also become model context. A field omitted from `/api/status` is not a confidential vault; do not configure private contact details or secrets that visitors must never receive.
- The assistant identifies itself as AI, does not promise actions for the owner, and should decline unsupported factual questions.
- Public operational analytics omit raw visitor questions, owner identities, and detailed error messages. Anonymous sessions are not verified unique people.
- A 30-day analytics report window is not a data-retention policy. Existing records are not automatically deleted or migrated.
- Browser history remains on the visitor's device. Model providers process submitted text/audio under the deployment's service terms. Do not submit sensitive information.
- Chat trims complete older turns from the model request to fit its token budget, without truncating saved browser history. Mandatory knowledge/latest-question overflow fails explicitly.
- Public admission has per-identity and deployment-wide limits. Hosted startup requires shared Cosmos settings/usage storage, and configured store failures do not bypass limits. Local development fallback limits apply only to one process.
- Set `DISABLE_PUBLIC_ACCESS=true` to stop new public admissions. Existing voice sessions are bounded separately. Billing alerts are not hard spending limits.
- Stronger gateway controls, trusted proxy configuration, retention policies, incident response, and production validation remain operator responsibilities.

Read [SECURITY.md](SECURITY.md) before public exposure.

## Publishing Your Fork

Use a neutral profile and sample assets, run local secret/content checks, review the final staged tree and history, and preserve upstream license notices. Configure private vulnerability reporting and your repository's maintainer/contact settings. No directory name or repository name grants permission to publish private content.

The template works from a folder of any name, including paths with spaces. `persona-assistant` is the recommended clone directory; renaming an open workspace is not required.
