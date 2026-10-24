# Contributing

Contributions to Persona Assistant are welcome. By submitting a contribution, confirm that you have permission to share it under the repository's [MIT license](LICENSE). This independent fork does not use Microsoft's CLA process.

## Before Opening A Pull Request

- Discuss significant behavior or deployment changes in an issue first.
- Keep changes focused and include regression tests.
- Use fictional test data. Never include credentials, personal conversations, real resumes, private audio, or employer/client material.
- Keep anonymous visitor access separate from owner authorization.
- Preserve strict realtime message validation, bounded resource usage, grounding, and explicit failures.
- Follow the [code of conduct](.github/CODE_OF_CONDUCT.md).

## Setup And Checks

Follow [README.md](README.md#local-development) and use the repository virtual environment. Run every block below from the repository root; the subshell type checks do not change your terminal's working directory. The examples use Bash.

```sh
source .venv/bin/activate
python -m pytest tests
python -m ruff check .
python -m black . --check
```

```sh
source .venv/bin/activate
(cd app/backend && mypy . --config-file=../../pyproject.toml)
```

```sh
source .venv/bin/activate
(cd scripts && mypy . --config-file=../pyproject.toml)
```

```sh
npm ci --prefix app/frontend
npm run build --prefix app/frontend
source .venv/bin/activate
python -m playwright install chromium
python -m pytest tests/e2e.py
```

Run a focused test first, then the relevant full suite. Browser tests require installed Playwright Chromium and the built frontend. Do not use live cloud calls in routine tests.

For coverage, run `python -m pytest --cov --cov-report=annotate:cov_annotate` and inspect touched missing lines. Do not update snapshots merely to make unexpected behavior pass.

Use the existing Prettier configuration for frontend changes and markdownlint configuration for documentation. Validate local documentation links.

Before a template release, run `python scripts/check_release.py` for working-tree hygiene and `python scripts/check_release.py --staged` for the exact index. These local checks print categories/paths, never secret values. They are not a complete credential, history, or publication-rights audit. The optional pre-commit configuration includes the staged check; a private or unstaged sample must be reconciled before a public commit.

These checks are specific to publishing the fictional template, not a general approval mechanism for someone's personal portfolio. See [template contributions versus personal deployments](docs/personalization-and-packaging.md#template-contributions-versus-personal-deployments) before committing customized profile or knowledge files.

## Pull Requests

Use the [pull-request template](.github/PULL_REQUEST_TEMPLATE.md), describe behavior changes and migration implications, and list checks actually performed. Distinguish mocked tests, image builds, real browser/device tests, and live deployments.

Never run provision/deploy commands or publish data without explicit authorization from the environment owner.
