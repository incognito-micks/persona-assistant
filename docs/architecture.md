# Architecture

## Supported Product

Persona Assistant is a single-owner portfolio with anonymous text chat and realtime voice. It is not a multi-tenant service, generic agent framework, or document-permission system.

React and Quart are served from one Container Apps deployment. Azure OpenAI supplies separate text, realtime, and input-speech transcription deployments. Managed identity provides hosted service access. Cosmos stores owner settings and shared operational admission state independently of visitor login/history.

The small container uses one asynchronous worker rather than multiplying the host's reported CPU count. Scale replicas deliberately; admission state remains shared. Process drain allows bounded voice sessions to finish where the hosting platform's own termination policy permits.

Visitors do not need accounts. Browser history is local. Owner-only administration uses one configured Entra identity and never becomes public when owner configuration is missing.

## Persona And Knowledge

[profile.json](../profile.json) is validated at startup and packaging. It supplies identity, tone, branding, links, and examples. Neutral built-in defaults are overridden by the profile file and then persisted owner fields. Existing persisted fields are not silently overwritten when changing the profile file.

The complete markdown knowledge set is loaded at startup and is available to both conversation modes. Text and voice share the same persona rules. The assistant identifies itself as AI and does not invent facts or commit the owner to actions.

The realtime service produces input transcription events identified by `item_id`. The relay forwards only bounded text and its correlation ID; the browser updates the matching spoken turn even when recognition finishes after an AI reply begins. Partial text is not fabricated, and a pending/generic marker remains if no usable recognition arrives. Voice conversation state is not persisted to browser text-chat history or Cosmos.

There are no embeddings, search calls, or document-level ACLs. A visitor who can chat can potentially receive any loaded knowledge.

Profile facts and contact settings are also conversation context. Excluding a field from the public status response does not guarantee that the model cannot mention it; configure only information appropriate for visitors.

## Public And Private Surfaces

- `/chat` and `/chat/stream`: public, validated, metered model requests.
- `/realtime`: public audio-only protocol with limits and bounded sessions.
- `/api/status`: public profile fields, never private manager contact or storage metadata.
- `/api/admin/*`: explicitly owner-authorized settings and operational metrics.
- `/#/admin`: lazy-loaded owner sign-in and administration UI.

The client capability response describes UI availability; it does not authorize requests.

## Configuration Categories

Normal users customize the profile and knowledge. Infrastructure supplies service/resource identifiers and model deployment names. Owner bootstrap establishes the explicit administrator. Emergency controls and documented internal limits are operational policy, not a feature-toggle matrix.

`PROFILE_PATH` and `KNOWLEDGE_DATA_DIR` select local packaging sources. The prepared backend bundle contains validated copies; local source paths must not be injected into hosted runtime settings.

The published container image is passed from azd's generated `SERVICE_BACKEND_IMAGE_NAME` into infrastructure as internal `backendImage` state. Reprovisioning must not read the same Container App resource being written: such a self-reference creates an ARM circular dependency. New environments use the bootstrap image until application deployment replaces it.

## Limits And Storage

Text prompt budgeting reserves output and a safety margin, then removes oldest complete turns only. Oversized mandatory knowledge/latest question fails instead of silently losing grounding. Browser history remains unchanged.

Rate-limit and voice-admission records use logical expiry and Cosmos item TTL. The settings container default never expires durable owner settings. Hosted startup requires this shared storage; local development can use process-scoped fallback limits, which are not replica-global production controls.

Operational analytics do not store new raw visitor questions, owner identities, or detailed errors. Session counts are approximate and not verified unique people. Existing records and retention are not changed by that privacy default.
