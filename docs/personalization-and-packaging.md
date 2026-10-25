# Personalization And Packaging

Personalization uses profile configuration and local markdown, not an ingestion service.

## Public Example And Private Sources

The repository includes a fictional Alex Morgan profile and example projects. Edit [profile.json](../profile.json) and top-level UTF-8 markdown in [data/work](../data/work), or keep real sources outside the repository and select them explicitly with `PROFILE_PATH` and `KNOWLEDGE_DATA_DIR`.

Only load material you have permission to share with every visitor. Do not include credentials, client documents, internal employer information, or another person's private data.

The profile validator rejects unknown fields, malformed values, unsafe image/link schemes, and oversized files. A configured missing/unreadable profile or knowledge directory fails explicitly.

The markdown loader sorts filenames, skips empty files, and formats each source with a filename label. It fails for missing, empty, or unreadable required knowledge. Nested files and non-markdown content are not ingested.

## Optional Mentorship Booking

Set `mentorship_url` in your profile or the owner workspace's **Mentorship Booking URL** field to an HTTPS booking/profile page. It is separate from `calendar_url`, which remains the general professional-call link. No provider integration, embedded widget, or new environment variable is needed.

When configured, **Book mentorship** appears in the persistent visitor footer and the owner's local draft preview. Clearing the field hides the footer link; an explicitly saved empty value overrides the packaged default. Existing profiles/settings without the field use the empty default or the selected profile value, with no storage migration.

Text and voice instructions use the current mentorship URL, distinguish it from the general calendar, and refer visitors to the destination for current offerings, pricing, and availability. Keep mentorship topics in approved knowledge, but avoid duplicating a static booking address there: an old URL must not override current owner settings. The assistant cannot book sessions or promise outcomes.

## Time-Sensitive Knowledge

Trusted markdown supports `[[today]]` for the current UTC date and `[[elapsed:YYYY-MM]]` for a calendar-month duration. For example, `Experience since March 2019: approximately [[elapsed:2019-03]], as of [[today]].` Use "approximately" when the exact start day is unknown. Overlapping roles must not be added together.

The loader validates these tokens but retains the raw source. Text chat and realtime voice resolve them anew for each request/session before token budgeting; dates are not frozen during packaging or backend startup. Invalid tokens, invalid months, and future elapsed start months fail explicitly. This is a limited date formatter, not arbitrary template/code execution.

Keep fixed achievement counts attached to their reporting period; do not make historical funding, audience, or project metrics grow with elapsed time.

## Template Contributions Versus Personal Deployments

The reusable template and your deployed persona have different publication needs:

- **Contributing to the template:** keep committed sample content fictional. The [publication checker](../scripts/check_release.py) expects the sample owner name `Alex Morgan` and the two example markdown filenames. Its optional pre-commit hook checks the staged tree, so replacing the committed sample with a real owner name can deliberately block a template commit.
- **Deploying your own persona:** prefer profile and knowledge files outside the checkout, selected with `PROFILE_PATH` and `KNOWLEDGE_DATA_DIR`. You can also use ignored local inputs, such as `profile.local.json` and a directory under `private/`. They are packaging inputs, not additional application-code changes.

Editing the bundled sample is fine for local experimentation. Restore fictional examples before submitting template contributions. A deliberately personalized public fork needs its own content review and publication policy; the template checker does not establish consent or publication rights, and passing it does not prove content is private-data-free.

Do not put secrets in either public or private persona inputs: the selected knowledge and profile can be supplied to model services and disclosed to visitors.

## Prepare A Deployment Package

From the repository root with dependencies installed:

```sh
source .venv/bin/activate
python app/backend/package_knowledge.py
npm run build --prefix app/frontend
```

For external sources, set `PROFILE_PATH` and `KNOWLEDGE_DATA_DIR` in the environment before these commands. Relative selections resolve from the repository root (or backend root in a backend-only artifact), independent of the command's working directory. Do not copy those local paths into container runtime variables.

The script validates the selected inputs, refreshes generated backend markdown, removes stale staged markdown, and bundles the profile. Vite prepares public assets and the metadata placeholders consumed by Quart.

Do not edit or commit generated `app/backend/data/work`, `app/backend/profile.json`, or `app/backend/static` outputs. These paths are intentionally absent from the published source tree. The [Dockerfile](../app/backend/Dockerfile) copies prepared application assets; it does not run a frontend build.

Changing knowledge requires restart/repackaging. Changing a profile file fills defaults but does not replace fields already persisted through the owner UI. Back up existing settings before an intentional profile migration.

## Verification

```sh
source .venv/bin/activate
python -m pytest tests/test_profile.py tests/test_knowledgebase.py
```

Packaging tests do not build an image or contact a deployed service.
