# SentinelOps threat model (Phase 6)

**Scope:** the local and single-VM Docker Compose deployment of SentinelOps and the one isolated
demo application it manages. **Method:** assets → trust boundaries → threats. Each threat lists
the existing mitigation, evidence (test names; tiers per `docs/traceability.md`), residual risk,
and the recommended production mitigation.

**Status legend:**
- *Mitigated*: the control exists and is tested.
- *Partial*: a control exists, but a known gap remains.
- *Accepted*: documented and not mitigated in V1.

## 1. Assets
| Asset | Where it lives | Impact if compromised |
|---|---|---|
| Anthropic API key | `ai_secrets` volume, file 0600, read-only in the worker; never in the DB, logs or env | Spend on the owner's account; model misuse |
| Operator tokens | shown once; only SHA-256 in `operators` | Approving restarts (approver role); reading approvals |
| API read token | `.env` → api env | Reading incidents, reports, metrics |
| Executor token + **action signing key** | `.env` → worker and executor env | Requesting the (single) demo restart |
| **Approval signing key** | `.env` → api **and worker** env (HMAC: the same key signs and verifies) | Forging approval signatures (see T8) |
| Webhook signing secret | `.env` → notifier (+ dev sink) env | Forging notifications to the receiver |
| PostgreSQL | `pgdata` volume | Authoritative history; tampering would falsify records |
| Executor ledger | `executor_state` volume (SQLite) | Loss could re-enable an executed action (mitigated by restore tombstones) |
| Evidence, reports, audit | PostgreSQL (reports and audit are append-only) | Integrity of the incident record |
| Backups | `backups/` (0600), should be copied off-host | Contain everything above except `ai_secrets` |
| Docker socket | host; mounted into `ops-reader` (ro) and `executor` | **Root-equivalent on the host** |

## 2. Trust boundaries
| # | Boundary | Controls |
|---|---|---|
| B1 | operator / user → API (`127.0.0.1:8000`, TLS proxy on the VM) | bearer tokens (fail closed with 503/401); operator roles; header-only auth (no cookies); JSON-only POST; cross-site/Origin refusal; no remediation endpoint |
| B2 | monitor → demo app (`demo_net`) | GET `/health` only; bounded timeout |
| B3 | worker → Anthropic (`ai_egress`) | official SDK; explicit key and base URL; ambient-credential guard; typed errors; budgets |
| B4 | worker → ops-reader (`ops_net`, internal) | token; GET-only inspect, logs and stats of one labelled container; no env or config returned |
| B5 | worker → executor (`exec_net`, internal) | token; HMAC-signed, expiring, action-bound request; fixed target; ledger; fencing; hourly cap |
| B6 | notifier → webhook (`notify_net`; egress only via override) | config-only URL, host allowlist, HTTPS outside dev, no redirects, bounded, signed |
| B7 | services → PostgreSQL / Redis (`backend`, internal) | passwords; no published ports (except the test-ports override); PostgreSQL is authoritative, Redis is transport |
| B8 | executor / ops-reader → Docker socket | narrow code paths; hardened containers; **no substitute for socket isolation** |
| B9 | model output → system | schema validation, allowlist, evidence ownership, deterministic policy, deterministic verification, report validator; the model can only *propose* |

## 3. Threats
| ID | Threat | Existing mitigation | Evidence | Residual risk | Recommended production mitigation | Status |
|---|---|---|---|---|---|---|
| T1 | **Prompt injection** via application logs (investigation) | logs are redacted and wrapped as `data_is_untrusted`; fixed system prompt and tools; allowlist; evidence and schema validation; policy decides | `test_prompt_injection_in_logs_is_contained` | a real model's resistance is unmeasured (no live test) | live evaluation set; keep policy default-deny | Mitigated (mock) |
| T2 | Prompt injection into **reports** | logs reach the report model only as `untrusted_log_excerpts`; fixed prompt and tool; the validator rejects unsupported claims; facts sections are rendered from records | `test_prompt_injection_in_logs_cannot_steer_the_report` | narrative wording heuristics can miss a novel phrasing (facts are unaffected) | review AI narratives; keep facts record-rendered | Mitigated / Partial |
| T3 | **Fabricated AI evidence** / invented actions, recovery, approvals, operators, costs | evidence existence and ownership checks; exact structured cross-checks; prose claim checks | `test_report_validator.py` (36), `test_fabricated_evidence_id_rejected_then_corrected`, `test_evidence_from_another_incident_rejected` | as T2 | — | Mitigated |
| T4 | Malicious logs exfiltrating secrets | redaction of logs before they reach the model; no secrets in the demo container; the key is never sent anywhere but Anthropic | `test_no_secrets_in_any_service_logs`, `test_no_secrets_in_any_phase5_service_logs` | the model sees application log content (by design, redacted) | keep the demo app free of secrets | Mitigated |
| T5 | **Credential leakage** (logs, DB, reports, notifications) | `SecretStr`; JSON log redaction (keys and patterns: `sk-ant-`, `sop_`, bearer, signatures, URL passwords); no credential columns; whitelisted payloads | unit redaction tests; `test_key_never_stored_in_database`; `test_payloads_never_contain_secrets`; live log and payload scans; committable-file scan | secrets in container env are visible to anyone with Docker access (`docker inspect`) | Docker secrets or a secrets manager; restrict Docker group membership | Partial |
| T6 | **Replay** of a restart request or an approval decision | signed `not_after` (≤ 600 s); ledger replay returns the recorded result; decision is one conditional UPDATE (409 on replay) | `test_duplicate_stream_delivery_never_restarts_twice`, `test_concurrent_decisions_exactly_one_wins`, live post-restore replay | — | — | Mitigated |
| T7 | **Duplicate actions** (at-least-once delivery, crashes, restore) | deterministic action ID; `uq_action_attempts_one_per_incident`; executor ledger at-most-once; reconciliation before retry; restore merge plus tombstones | crash matrix in `test_remediation.py`; `test_crash_during_policy_evaluation_…`; live `test_worker_killed_during_execution_…`; live backup/restore replay | — | — | Mitigated |
| T8 | **Forged approval** | HMAC-signed decisions verified by the worker; decider must be an active approver; fingerprint-bound | `test_forged_approval_row_is_not_authorization` | **HMAC is symmetric: the worker holds the approval signing key**, so a *fully compromised worker process* could sign an approval. The model has no access to it. | asymmetric signatures (the API signs with a private key; the worker verifies with the public key) | Partial (finding recorded in Phase 6) |
| T9 | **Stale worker** acting after losing its lease | fencing tokens on every write; DB fencing trigger; executor refuses lower tokens; report-job fencing | `test_two_workers_racing_…`, `test_stale_worker_cannot_execute`, `test_stale_worker_cannot_overwrite_verification`, `test_stale_report_worker_is_fenced_out` | — | — | Mitigated |
| T10 | **SSRF** via notifications | destination only from configuration; allowlist; HTTPS; link-local/metadata refused; private refused in production; no redirects; model never supplies URLs | `test_untrusted_webhook_destinations_rejected`, `test_webhook_sends_signed_idempotent_request_without_following_redirects` | DNS of an allowlisted host could resolve to an internal address | egress firewall; resolve-and-pin or proxy | Mitigated / Partial |
| T11 | **Unauthorized Docker control** | the model has no socket; the worker has no socket; ops-reader is GET-only; executor has one verb and a fixed target; internal networks | `test_privilege_boundaries_live`, `test_datastores_not_published_by_default` (socket holders are exactly ops-reader and executor), `test_request_cannot_choose_target` | **socket group access is root-equivalent**: a compromised ops-reader or executor process owns the host | a socket proxy allowing only the required verbs for the labelled container, or rootless Docker | Accepted (documented) |
| T12 | **Notification exfiltration** (secrets or raw logs sent out) | payload key whitelist plus redaction; no fingerprints or tokens; model prose never sent | `test_payload_whitelist_drops_secrets_and_unknown_keys` | titles include service and incident IDs (by design) | — | Mitigated |
| T13 | **DB tampering** (edit history, forge approvals, delete audit) | append-only triggers (audit, policy decisions, reports; TRUNCATE refused); signed approvals; fencing | `test_reports_and_audit_are_append_only`, `test_audit_events_append_only` | one DB superuser role for all services, so triggers can be disabled by that role | per-service least-privilege roles; audit shipping to write-once storage | Partial |
| T14 | **Backup tampering** | checksummed manifest verified before restore; ledger integrity check; restore never downgrades the ledger | `restore.sh` refuses on checksum mismatch; unit `test_cli_refuses_corrupt_backup`; live drill | the manifest is not signed; an attacker able to rewrite both files and manifest defeats it | sign manifests; immutable off-host storage with versioning | Partial |
| T15 | **Compromised worker** | cannot reach Docker or the demo app; the executor accepts one action on one target once per incident per ledger plus an hourly cap | privilege-boundary tests | holds the executor token, action signing key and approval key: it can trigger the one demo restart and could forge approvals (T8) | asymmetric approvals; executor re-verifies policy via a read-only DB role | Partial |
| T16 | **Compromised executor** | internal network only, no egress, fixed target | `test_privilege_boundaries_live` | Docker socket = host root (T11) | socket proxy / rootless Docker | Accepted |
| T17 | **Host compromise** | out of scope for container controls | — | total | hardened VM, patching, SSH hardening, off-host backups and monitoring | Accepted |
| T18 | Unauthenticated API use / CSRF | fail-closed bearer auth; header-only; CSRF guard; dashboard shows data only via the API | `test_api_v1_auth.py`, `test_approval_api.py`, `test_production_hides_docs_and_dashboard` | operator tokens do not expire | expiry and rotation schedule; SSO in front of the proxy | Partial |
| T19 | Cost exhaustion (AI abuse) | per-investigation, per-incident and daily budgets; concurrency slots; no idle calls | `test_daily_budget_…`, `test_per_incident_budget_…`, `test_no_ai_calls_while_idle` | provider-side limits are still required | Console spend limits | Mitigated |

## 4. Explicit statements
- **Docker socket access is root-equivalent.** Read-only mounts, `cap_drop`, `no-new-privileges`, a
  read-only root FS and narrow code paths reduce the chance of misuse. They do **not** prevent a
  compromised `ops-reader` or `executor` process from controlling the host.
- The AI (the model) has no credentials, no socket, no shell, no filesystem, no URL fetch and no
  write path. The *worker process* that hosts the orchestrator holds service credentials; its
  compromise is T15.
- No live Claude model has been evaluated against these threats (see `PHASE_STATUS.md`).
