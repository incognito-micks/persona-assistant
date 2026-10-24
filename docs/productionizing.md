# Production Operation And Release Checks

## Before A Public Release

1. Use fictional example sources/media and review publication rights.
2. Review final staged files and any published history, not only the working tree.
3. Run a local credential/content scan; configure repository secret scanning and private vulnerability reporting when available.
4. Preserve upstream license notices and publish maintainer/contribution policies.
5. Validate clean dependency installation, tokenizer preparation, frontend build, unit/API tests, browser tests, and the actual container image.
6. Perform an explicitly authorized fresh deployment with a supported region and sufficient text/realtime capacity.
7. Verify real microphone permissions, audio, interruption, disconnect, restart, owner authorization, and budget exhaustion.
8. Have a second person configure another fictional profile without changing application code.

The tests do not constitute a privacy, legal, or production-security certification.

## Costs And Abuse

Voice is a core default and creates paid model usage. Public access therefore needs shared admission limits, bounded sessions/concurrency, application-enforced quotas, monitoring, and an incident response plan.

The generic public request cap is 200 admissions per rolling 24 hours, in addition to per-identity rate limits. Realtime has its own admission/concurrency/session bounds. These limit usage, not currency: rates, input size, output, transcription, and active audio affect actual cost.

`DISABLE_PUBLIC_ACCESS=true` prevents new public admissions. It does not retroactively erase records or instantly terminate already-running requests. Use ingress controls or stop the service during an incident that requires immediate termination. Budget alerts do not automatically stop usage.

Apply request/connection limits at a trusted gateway, validate proxy behavior, and limit network egress. Never bypass a configured admission store because it is unavailable.

## Data

Publish a privacy notice describing browser-local history, model processing, and your configured operational telemetry. Choose and implement a retention/deletion policy appropriate to your deployment. The report's 30-day window does not delete stored records; existing analytics are not backfilled or automatically erased.

Do not enable prompt-content instrumentation or audio recording by default. Review provider-side logging/retention separately. Limit administrative access and review backups, exports, and diagnostic logs.

## Deployment And Recovery

Use one supported Container Apps topology and the declared model defaults. Preflight availability/quota checks are advisory and do not reserve capacity. Directory app creation and role assignments require appropriate permissions.

Keep the environment name and selected subscription/region stable for updates. Back up owner settings before migrations. Test redeployment and teardown in a disposable environment before operating a supported instance.

Existing deployments from the upstream parameter matrix should first be evaluated in a new environment. Removing a resource from an incremental template does not guarantee deletion of an old billable resource. Review old resources, redirect URIs, and saved profile overrides explicitly; no cloud cleanup or data migration is performed by source-code sanitization.

No production/cloud provisioning should be run by a coding agent without explicit authorization for the target subscription and costs.
