# Security

Persona Assistant is an independent open-source template, not a Microsoft-operated service or a production security certification.

## Reporting A Vulnerability

Do not disclose credentials, private data, or exploitable details in a public issue.

For a public repository with private vulnerability reporting enabled, use **Security > Advisories > Report a vulnerability**. This sends a private report to the repository maintainers; no public email address is necessary.

Maintainers should [enable GitHub private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository) when making the repository public. GitHub does not offer that reporting feature while a repository is private. This document does not claim that an unavailable reporting form is already enabled.

If the button is unavailable, open a minimal issue asking maintainers to enable private reporting or arrange a private channel. Do not include the vulnerability, reproduction, credentials, or affected private data in that issue; wait for a private route before sharing those details.

Include affected versions, prerequisites, impact, and a minimal reproduction using fictional data. Reports about this independent adaptation should not be sent to the upstream Microsoft project unless the issue also affects that project.

Until a versioned support policy is published, include the affected commit SHA and check whether the issue also affects the current default branch. There is no guaranteed response time or support commitment for arbitrary forks.

## Deployment Responsibilities

- All loaded markdown is shared context for permitted visitors. Never use public mode as protection for private knowledge.
- Keep model/service credentials server-side. Prefer managed identity for hosted resources.
- Owner administration must require a validated identity and explicit owner authorization. Missing owner configuration must fail closed.
- Treat profiles, URLs, user messages, model output, and markdown as untrusted at rendering/network boundaries.
- Preserve URL destination/redirect validation, byte caps, realtime event allowlisting, session limits, and shared admission checks.
- Trust forwarding headers only through a configured trusted proxy. Add gateway request/connection controls and network egress policy.
- Model services are billable. Enforce application usage caps and monitor actual charges; alerts alone do not stop spend.
- Establish consent, retention, deletion, telemetry access, backups, and incident-response procedures.
- Browser-local history is not suitable for a shared device without user awareness.

## Before Publishing

Review the working tree **and staged tree**, any existing history, build artifacts, screenshots, logs, and bundled knowledge. An ignore rule does not remove previously staged or committed material.

Scan locally for credentials and unintended personal data. Rotate credentials that were exposed; deleting the source file is not sufficient. Review rights to all sample content and media. Do not send private repository contents to an external scanning service without authorization.

Private reporting is a recommended disclosure practice, not a condition of the MIT license. Open-sourcing the code does not require publishing a personal email address or providing a support SLA.

## Dependency Maintenance

The backend installs the pinned [runtime requirements](app/backend/requirements.txt). Keep security minimums in [requirements.in](app/backend/requirements.in), including deliberate floors for vulnerable transitive dependencies, and regenerate the lock with the repository's documented targeted upgrade command. Development, scripts, and evaluation requirements include that runtime lock; do not duplicate its pins in those files.

Triage alerts by advisory and affected package, not just the number of inherited lockfile records. Distinguish runtime exposure from build-tool risks and conditional API usage, but do not dismiss an alert merely because an exploit has not been demonstrated. Check patched-version ranges and resolver compatibility, then run the relevant offline regressions. Major migrations and advisories without a compatible fix require explicit follow-up rather than forced dependency overrides.

Updating source requirements does not patch a running deployment. Rebuild and redeploy through the authorized release process before considering a hosted instance updated.

The text prompt adapter parses only repository-owned template roles before interpolating values. History stays structured, and sources, job descriptions, and questions remain literal text within their assigned messages. Preserve this boundary, the scoped template loader, and rejection of unsupported roles or non-text output when changing the prompt SDK.

## Validation Boundaries

Offline tests and infrastructure compilation do not validate a cloud deployment, directory configuration, quotas, gateway policy, real microphone, or every supported browser. Complete those checks before claiming production readiness.
