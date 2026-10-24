# Instructions For Coding Agents

Persona Assistant is a single-owner AI portfolio: anonymous text chat and realtime voice, owner-only administration, and browser-local visitor history. Quart supplies shared markdown directly to the model. There is no active Search, embedding/indexing, ingestion, document ACL, or Ask-page runtime.

Preserve unrelated staged/working changes and respect delegated file ownership. Keep these instructions and [README.md](README.md) current when changing behavior.

## Key Surfaces

| Path | Responsibility |
| --- | --- |
| [profile.json](profile.json) | Public fictional sample profile |
| [app/backend/core/profile.py](app/backend/core/profile.py) | Profile validation, path resolution, shared text/voice persona |
| [app/backend/app.py](app/backend/app.py) | Quart routes, model/credential setup, metadata, admission |
| [app/backend/approaches/chat.py](app/backend/approaches/chat.py) | Grounded chat/streaming |
| [app/backend/approaches/approach.py](app/backend/approaches/approach.py) | Completion and token-budget support |
| [app/backend/approaches/prompts/persona_answer.prompty](app/backend/approaches/prompts/persona_answer.prompty) | Active text prompt |
| [app/backend/admin/settings_cosmosdb.py](app/backend/admin/settings_cosmosdb.py) | Persisted owner overrides and bounded operational analytics |
| [app/backend/core](app/backend/core) | Auth, knowledge, profile, URL validation, analytics, usage |
| [app/backend/realtime](app/backend/realtime) | Strict bounded audio relay |
| [app/backend/package_knowledge.py](app/backend/package_knowledge.py) | Validate/stage selected profile and markdown |
| [app/frontend/src](app/frontend/src) | Public chat/voice, profile UI, owner administration |
| [data/work](data/work) | Fictional top-level UTF-8 example markdown |
| [infra](infra) and [azure.yaml](azure.yaml) | Opinionated Container Apps deployment |
| [tests](tests) | Offline unit/API and explicit mocked browser tests |

## Runtime Contracts

- Missing owner configuration disables administration, not public chat/voice. Authentication and exact owner authorization remain on the server; never default to any signed-in user.
- Preserve bounded JWT signing-key caching and validation. Do not restore unused Search OBO or visitor-login requirements.
- Profile precedence is neutral defaults, selected profile, then persisted owner fields. Await `get_current_settings()` per request; preserve ETag checks and validation on updates.
- Text and voice share the profile/persona and complete knowledge. Query classification affects tone, never access to grounding.
- Public requests cannot override or inject owner system prompts. Persona configuration belongs to validated profile/settings, not visitor overrides.
- Preserve prompt budgeting, complete-turn trimming, and explicit mandatory-context errors. Never trim saved browser history.
- Preserve SSRF destination/redirect validation and response-byte caps.
- Realtime accepts only allowlisted audio events. Preserve cancellation, session/concurrency/daily admission limits, safe close reasons, and microphone cleanup.
- Input transcription is a default Azure capability, not browser speech recognition. Forward bounded transcript text with validated `item_id`, correlate it to committed turns, and handle asynchronous ordering without duplicating replies or saving voice transcripts as text-chat history.
- Shared admission failures must fail explicitly. Local fallback is not replica-global; never describe it as production-wide protection.
- New analytics must not retain raw questions, owner identities, detailed model errors, or audio. Session metrics are not verified unique people. Reporting windows are not retention/deletion policies.
- Do not remove existing analytics/settings or migrate personal data without explicit authorization.
- Keep historical Cosmos-history ordering/batch/input-validation protections if touching retained compatibility code.
- Do not add a provider/hosting/feature-toggle matrix. Profile fields and internal deployment outputs should not become mandatory user environment variables.

## Packaging And Privacy

Read [personalization and packaging](docs/personalization-and-packaging.md). Use `PROFILE_PATH` and `KNOWLEDGE_DATA_DIR` only for explicitly selected sources. Do not accidentally bundle local/private inputs in the public template or copy local paths to hosted runtime settings.

Generated backend profile/data/static assets are not hand-edited or committed. Manual container preparation must stage profile/knowledge and build the frontend. Preserve the tokenizer-cache warm-up for restricted runtime startup.

Use fictional fixtures/media. Review publication rights and working/staged trees separately; ignore rules do not unstage or erase private material.

## Validation

Use the repository `.venv`, existing tooling, and mocked services. Run each command block from the repository root; type-check subshells preserve that directory. Run a focused test first.

When Bicep is installed, warm its public module cache with `az bicep restore --file infra/main.bicep` before offline tests. Do not remove `--no-restore` from the compilation fixtures or introduce network/service calls inside tests. Mock telemetry configuration/instrumentors in unit tests so dummy connection strings never start live exporters.

```sh
source .venv/bin/activate
python -m pytest tests/test_profile.py tests/test_chatapproach.py
python -m pytest tests
python -m ruff check .
python -m black . --check
```

Run application/script type checks, not tests:

```sh
source .venv/bin/activate
(cd app/backend && mypy . --config-file=../../pyproject.toml)
```

```sh
source .venv/bin/activate
(cd scripts && mypy . --config-file=../pyproject.toml)
```

Frontend changes require TypeScript and production build. Browser tests are not discovered by default:

```sh
npm run build --prefix app/frontend
source .venv/bin/activate
python -m pytest tests/e2e.py
```

Keep `noUnusedLocals`/`noUnusedParameters`. Preserve markdown sanitization and lazy rendering. Shared React/CommonJS initialization must remain cycle-free; a successful build does not prove browser startup.

Use coverage annotations for touched missing paths. Use markdownlint and verify local documentation links. Report mocked tests, packaging, image builds, real devices, and live deployment checks separately.

## Conventions And Release

Reuse existing helpers and typing patterns. Do not introduce single-underscore private Python names, unrelated renames, broad catches, silent defaults on malformed input, or success-shaped errors.

Regenerate dependency locks with package tooling; do not fabricate or hand-edit generated resolution when a registry is unavailable. Keep script-only dependencies separate from runtime inputs.

No cloud provisioning, deployment, remote builds, identity changes, publishing, or commits without explicit authorization. Follow [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and the [pull-request template](.github/PULL_REQUEST_TEMPLATE.md).

For authorized Azure work, use the explicit target environment and separate `az`/`azd` authentication checks in [operations](docs/operations.md). Never infer the intended subscription or teardown target from a previously selected default environment.

Use the manifest's minimum supported azd version for real deployment; the validated remote build requires azd 1.37.1 or newer. A local Bicep compilation cannot prove Container Apps API acceptance: disabling saved environment logs uses a null `appLogsConfiguration`, not the rejected literal destination `"none"`.

Preserve the running image on reprovision through generated `SERVICE_BACKEND_IMAGE_NAME`/`backendImage` input, never an ARM self-reference to the Container App being updated. Model preflight must include text, realtime, and input transcription without adding mandatory user feature switches.
