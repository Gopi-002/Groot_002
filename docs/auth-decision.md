# Authentication decision: Claude access for SentinelOps

**Decision date:** 2026-09-22 · **Status:** in force · **Code:** `app/auth/decision.py` (the UI text comes from there)

## Decision

| Option | Supported in SentinelOps | Why |
|---|---|---|
| **Claude Subscription** (Pro/Max/Team/Enterprise login) | **No.** It appears in onboarding but is disabled, with the message *"Subscription integration unavailable for this application"* | Anthropic does not allow third-party products to offer claude.ai login or subscription rate limits without prior approval, and SentinelOps has none. Subscription access is meant for Anthropic's own apps. Shared or production automation is directed to API keys. |
| **Anthropic API key** | **Yes (implemented)** | Officially supported for servers and unattended workloads through the official Client SDK. |
| Workload Identity Federation (WIF) | Officially supported; **not implemented yet** | This is Anthropic's recommended production path for eliminating static keys. It is the planned upgrade for the VM deployment (see Limitations). |

SentinelOps does **not**:
- ask for a Claude password;
- scrape cookies;
- read `~/.claude`, `claude`/`ant` CLI credential stores or profiles;
- proxy through `claude -p`;
- impersonate Claude Code;
- implement any OAuth flow of its own.

## Sources (read 2026-09-22)

1. **Agent SDK overview**, https://code.claude.com/docs/en/agent-sdk/overview:
   > "Unless previously approved, Anthropic does not allow third party developers to offer claude.ai login or rate limits for their products, including agents built on the Claude Agent SDK. Use the API key authentication methods described in the Quickstart instead."

   The same page's branding rules forbid products that "appear to be Claude Code".
2. **Log in to your Claude account**, https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account:
   > "The preferred way to access Anthropic services using third-party software, tools, or services … is through API key authentication through Claude Console or a supported cloud provider."

   > "Applications that misrepresent their identity to Anthropic's servers, attempt to route third-party traffic against subscription limits, or otherwise violate applicable terms or policies are prohibited."

   Subscription OAuth is "designed to support ordinary use of native Anthropic applications".
3. **Use the Claude Agent SDK with your Claude plan**, https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan (last updated 2026-06-16). The June 15 billing change is **paused**: "Claude Agent SDK, `claude -p`, and third-party app usage still draw from your subscription's usage limits." The article also says:
   > "Teams running shared production automation should use Claude Platform with an API key for predictable pay-as-you-go billing."

   Subscription usage limits "stay reserved for interactive use".
4. **Claude API authentication**, https://platform.claude.com/docs/en/manage-claude/authentication. The supported methods are API keys, Workload Identity Federation and App Attest. Personal or service-account keys are recommended; "For shared or automated workloads (CI, production services), have an organization admin create a service account." Keys can be given an expiry, and an expired key returns `401 authentication_error`.

Conclusion: subscription login is **not an officially permitted integration for this custom, unattended third-party app**. Even if it were, subscription OAuth cannot be relied on for unattended 24/7 refresh. The API key is the only supported path.

## SDK and version (verified against the installed package, not assumed)

- **`anthropic` 1.8.0** (official Anthropic Python Client SDK, built on `httpx2` 2.13.0). It is pinned in `pyproject.toml` as `>=1.8,<2` and locked in `uv.lock`.
- **The Claude Agent SDK (`claude-agent-sdk`, 0.2.157 on PyPI) is deliberately not used.** It embeds Claude Code's built-in Bash, file-editing and web tools and its subscription-oriented auth. That conflicts with the contract that the model gets only six read-only tools and no shell or filesystem access. The plain Client SDK plus our own bounded loop fits "one orchestrator, no agent framework".
- Methods used: `models.list`, `models.retrieve` and `messages.create` (with `tool_choice: auto`). Error classes: `AuthenticationError`, `PermissionDeniedError`, `NotFoundError`, `RateLimitError`, `APIStatusError` (≥500, 529, 402/`billing_error`), `APITimeoutError` and `APIConnectionError`.
- **Credential isolation**, from the installed `anthropic/_client.py` docstring: "When any of these [`api_key=`, `auth_token=`, `credentials=`, `config=`, `profile=`] is passed, environment variables are not consulted for credentials." Profile and WIF discovery run only when no credential is passed. SentinelOps always passes `api_key=` and `base_url=` explicitly. In addition, `assert_clean_environment()` refuses to run if any of these are set: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_PROFILE`, `ANTHROPIC_CONFIG_DIR`, the WIF variables, `ANTHROPIC_CUSTOM_HEADERS` or `ANTHROPIC_BASE_URL`. A signed-in `claude` CLI is present on the build host and is never consulted.

## How the API key is handled

| Concern | Implementation |
|---|---|
| Entry | `docker compose run --rm onboard` shows the menu (Subscription [unavailable] / API key / Exit). The key is typed with **masked input** (`getpass`). |
| Validation | A **free** provider call, `GET /v1/models`, which consumes no tokens. The key is saved **only after** the provider accepts it. No billable call happens without explicit consent (none is made at all). |
| Storage | One file, `/var/lib/sentinel-secrets/anthropic_api_key`, in the dedicated `ai_secrets` Docker volume. The file is mode 0600 and owned by uid 10001, written atomically. It is mounted **read-only** into the worker and read-write only into the on-demand `onboard` tool. |
| Never stored in | PostgreSQL (`model_config` stores only `auth_mode` + `model_id`; tests scan every table), container env vars, `.env`, logs (redaction plus tests), or the repository. |
| Display | Only a SHA-256 fingerprint (`sha256:xxxxxxxxxxxx`). |
| Rotation | Create a new key in the Console, run `onboard set-key`, then delete the old key. A service-account key is recommended. `onboard remove-key` revokes locally, which pauses AI while monitoring continues. |
| Billing | The onboarding text says so explicitly: **API usage is pay-as-you-go on the Console account and is separate from any Claude subscription.** Set Console spend limits. The optional SentinelOps per-investigation cost cap needs operator-supplied prices; none are hardcoded. |

## Model selection

- Models come from the provider's **model-list endpoint** (`client.models.list()`, auto-paginated) for the authenticated key. The list is never hardcoded, and ID, name, context window and max output are shown.
- The chosen ID is validated with `models.retrieve(id)` before it is persisted. Only `auth_mode` and `model_id` are stored, and the change is audited (`model_selected`, with the previous selection).
- The model is **pinned per task** when its investigation starts (`tasks.model_id`, fenced write). `change-model` affects new tasks only, and a resumed investigation keeps its pinned model.
- No silent fallback: a missing or unavailable pinned model is escalated to a human (`model_unavailable`). A refusal becomes `ModelRefused` and escalates. The SDK's server-side refusal `fallbacks` feature is deliberately not enabled.

## Failure handling (typed errors → behaviour)

| Provider situation | Typed error | Behaviour |
|---|---|---|
| No key / key file missing | `CredentialsMissing` | Task parked (`ai_paused_credentials_missing`), attempt not consumed, retried after `SENTINEL_AI_PAUSE_SECONDS`, alert log and `sentinel_ai_paused_tasks` metric. **Monitoring and queueing continue.** |
| Invalid / revoked / **expired** key (401) | `AuthenticationFailed` | Same as above (pause, alert). |
| 403 | `PermissionDenied` | Pause. |
| 402 / `billing_error` / "credit balance too low" | `QuotaExceeded` | Pause. |
| 429 | `RateLimited` (+`retry-after`) | Pause for `retry-after`. |
| 5xx / 529 / network | `ProviderUnavailable` | Bounded task retries with backoff (max 3), then dead-lettered and **escalated**. |
| Timeout | `RequestTimeout` | As above. |
| 404 model | `ModelUnavailable` | Investigation `failed`, task and incident **escalated**. The model is not switched. |
| Refusal | `ModelRefused` | Escalated. |

## Tested behaviour and evidence

- **Unit tests (real SDK, offline).** `tests/unit/test_anthropic_gateway.py` drives the installed `anthropic` 1.8.0 client through an in-process `httpx2.MockTransport`. It covers request shape (no sampling or thinking params, no fallbacks), `x-api-key` sent explicitly with ambient `ANTHROPIC_AUTH_TOKEN` never sent, pagination, all error classes, and refusal.
- **Unit and integration auth tests.** `tests/unit/test_auth.py` and `tests/integration/test_cli_onboarding.py` cover: subscription disabled; API-key path with a mock provider; key file 0600; no key in any DB table or output; invalid key not saved; model selection and change audited.
- **Live, unauthenticated probe (no credential, no tokens, no cost).** From inside the stack, `AnthropicGateway.authenticate()` was called against the real `https://api.anthropic.com` with a deliberately invalid placeholder key. The provider answered **HTTP 401**, which the gateway classified as `AuthenticationFailed` with `pause_ai=True`. This proves the egress path, TLS and live error mapping. **It does not prove that a valid key works.**
- **Live model test: NOT executed.** No valid Anthropic credentials are available, and usage cost has not been authorized. All AI behaviour in tests uses deterministic mock models (`mock-investigator-v1`, provider `mock`, labelled "TEST/DEMO ONLY - not Claude"). **Mocked tests are not evidence that live subscription or API authentication works.**

## Limitations and re-verification

- Subscription mode stays disabled until Anthropic documents a permitted integration for this kind of app *and* the approval exists. Re-check the four sources above before changing `app/auth/decision.py`.
- The API key is a long-lived static secret in a Docker volume. For the always-on VM, prefer a secrets manager, or Workload Identity Federation (not yet implemented).
- Before relying on AI in production, a live smoke test with a real key is required. It sends one small request, which is a billable action, and needs explicit authorization.
