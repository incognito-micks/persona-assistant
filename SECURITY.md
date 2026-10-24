# Security

Persona Assistant is an independent open-source template, not a Microsoft-operated service or a production security certification.

## Reporting A Vulnerability

Do not disclose credentials, private data, or exploitable details in a public issue.

Use this repository's **Security > Report a vulnerability** flow when private reporting is enabled. If that option is unavailable, ask a maintainer for a private reporting channel without posting sensitive details. Repository maintainers must configure private reporting or a monitored private contact before publishing a supported release.

Include affected versions, prerequisites, impact, and a minimal reproduction using fictional data. Reports about this independent adaptation should not be sent to the upstream Microsoft project unless the issue also affects that project.

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

## Validation Boundaries

Offline tests and infrastructure compilation do not validate a cloud deployment, directory configuration, quotas, gateway policy, real microphone, or every supported browser. Complete those checks before claiming production readiness.
