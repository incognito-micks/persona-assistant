# Knowledge, Profiles, And Packaging

Despite the historical filename, this is file-based configuration, not an ingestion service.

## Public Example And Private Sources

The repository includes a fictional Alex Morgan profile and example projects. Edit [profile.json](../profile.json) and top-level UTF-8 markdown in [data/work](../data/work), or keep real sources outside the repository and select them explicitly with `PROFILE_PATH` and `KNOWLEDGE_DATA_DIR`.

Only load material you have permission to share with every visitor. Do not include credentials, client documents, internal employer information, or another person's private data.

The profile validator rejects unknown fields, malformed values, unsafe image/link schemes, and oversized files. A configured missing/unreadable profile or knowledge directory fails explicitly.

The markdown loader sorts filenames, skips empty files, and formats each source with a filename label. It fails for missing, empty, or unreadable required knowledge. Nested files and non-markdown content are not ingested.

## Prepare A Deployment Package

From the repository root with dependencies installed:

```sh
source .venv/bin/activate
python app/backend/package_knowledge.py
npm run build --prefix app/frontend
```

For external sources, set `PROFILE_PATH` and `KNOWLEDGE_DATA_DIR` in the environment before these commands. Relative selections resolve from the repository root (or backend root in a backend-only artifact), independent of the command's working directory. Do not copy those local paths into container runtime variables.

The script validates the selected inputs, refreshes generated backend markdown, removes stale staged markdown, and bundles the profile. Vite prepares public assets and the metadata placeholders consumed by Quart.

Do not edit or commit generated [backend data](../app/backend/data) or the generated backend profile. The Dockerfile copies prepared application assets; it does not run a frontend build.

Changing knowledge requires restart/repackaging. Changing a profile file fills defaults but does not replace fields already persisted through the owner UI. Back up existing settings before an intentional profile migration.

## Verification

```sh
source .venv/bin/activate
python -m pytest tests/test_profile.py tests/test_knowledgebase.py
```

Packaging tests do not build an image or contact a deployed service.
