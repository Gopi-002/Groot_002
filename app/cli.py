"""SentinelOps operator CLI: onboarding, model selection and AI status.

    docker compose run --rm onboard                 # interactive onboarding
    docker compose run --rm onboard status
    docker compose run --rm onboard change-model
    docker compose run --rm onboard set-key         # rotate the API key
    docker compose run --rm onboard remove-key
    docker compose run --rm onboard report-request INCIDENT_ID   # new report version
    docker compose run --rm onboard notify-test                  # test notification

Never prints, logs or stores the API key anywhere but the key file; the key is
typed with masked input and only saved after the provider accepts it. Only the
auth MODE and model ID are persisted in PostgreSQL.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import SecretStr
from sqlalchemy import Engine, text

from app.agent.anthropic_gateway import AnthropicGateway
from app.agent.gateway import GatewayError, ModelDescriptor, ModelGateway
from app.agent.mock_gateway import DeterministicMockGateway
from app.agent.model_config import ModelSelection, active_selection, save_selection
from app.auth.decision import API_KEY, SUBSCRIPTION
from app.auth.secrets import (
    CredentialError,
    delete_api_key,
    fingerprint,
    read_api_key,
    validate_key_format,
    write_api_key,
)
from app.config import Settings, get_settings
from app.persistence.db import make_engine

BILLING_NOTE = (
    "Billing: Anthropic API usage is billed pay-as-you-go to the Claude Console account "
    "that owns the key. It is separate from, and not covered by, any Claude subscription "
    "(Pro/Max/Team/Enterprise). Set spend limits in the Console."
)
ROTATION_NOTE = (
    "Rotation: create a new key in the Claude Console, run 'set-key', then delete the old "
    "key in the Console. Use a service-account key for this unattended workload."
)


@dataclass
class IO:
    ask: Callable[[str], str]
    ask_secret: Callable[[str], str]
    say: Callable[[str], None]


def real_io() -> IO:
    return IO(ask=input, ask_secret=getpass.getpass, say=print)


GatewayBuilder = Callable[[Settings, SecretStr | None], ModelGateway]


def build_gateway(settings: Settings, key: SecretStr | None) -> ModelGateway:
    if settings.ai_gateway == "mock":
        return DeterministicMockGateway()
    if key is None:
        raise CredentialError("no API key")
    return AnthropicGateway(
        key,
        base_url=settings.anthropic_base_url,
        timeout_seconds=min(settings.ai_request_timeout_seconds, 30.0),
        max_retries=1,
    )


def _operator() -> str:
    return "cli:" + os.environ.get("SENTINEL_OPERATOR", "operator")[:40]


def _describe(m: ModelDescriptor) -> str:
    ctx = f"{m.max_input_tokens:,} ctx" if m.max_input_tokens else "ctx n/a"
    out = f"{m.max_output_tokens:,} max out" if m.max_output_tokens else "max out n/a"
    return f"{m.id}  -  {m.display_name}  ({ctx}, {out})"


class Cli:
    def __init__(
        self, settings: Settings, engine: Engine, io: IO, builder: GatewayBuilder = build_gateway
    ) -> None:
        self.s = settings
        self.engine = engine
        self.io = io
        self.builder = builder

    @property
    def mock(self) -> bool:
        return self.s.ai_gateway == "mock"

    # --- onboarding --------------------------------------------------------------
    def onboard(self) -> int:
        say = self.io.say
        say("Welcome to SentinelOps")
        if self.mock:
            say("!! MOCK AI GATEWAY ACTIVE - a deterministic TEST model, not Claude. !!")
        while True:
            say("")
            say("Choose authentication:")
            say(f"1. {SUBSCRIPTION.label}  [unavailable]")
            say(f"2. {API_KEY.label}")
            say("3. Exit")
            choice = self.io.ask("Selection [1-3]: ").strip()
            if choice == "1":
                say(SUBSCRIPTION.reason)
                say("Choose option 2 to use an Anthropic API key.")
            elif choice == "2":
                return self._api_key_flow()
            elif choice == "3":
                say("Exiting; nothing was changed.")
                return 0
            else:
                say("Please enter 1, 2 or 3.")

    def _api_key_flow(self) -> int:
        say = self.io.say
        say(BILLING_NOTE)
        gateway = self._authenticate_new_key() if not self.mock else self.builder(self.s, None)
        if gateway is None:
            return 1
        return self._select_model(gateway)

    def _authenticate_new_key(self) -> ModelGateway | None:
        say = self.io.say
        for _ in range(3):
            raw = self.io.ask_secret("Anthropic API key (input hidden): ")
            try:
                key = SecretStr(validate_key_format(raw))
            except CredentialError as exc:
                say(f"Rejected: {exc}.")
                continue
            try:
                gateway = self.builder(self.s, key)
                check = gateway.authenticate()  # free call: lists models, no tokens used
            except GatewayError as exc:
                say(f"Authentication failed ({exc.kind}). The key was NOT saved.")
                if not exc.pause_ai and not exc.retryable:
                    continue
                return None
            except CredentialError as exc:
                say(f"Rejected: {exc}.")
                return None
            write_api_key(self.s.anthropic_api_key_file, key)
            say(f"API key verified and stored (fingerprint {check.credential_fingerprint}).")
            say(ROTATION_NOTE)
            return gateway
        say("Too many failed attempts; nothing was saved.")
        return None

    def _existing_gateway(self) -> ModelGateway | None:
        if self.mock:
            return self.builder(self.s, None)
        try:
            key = read_api_key(self.s.anthropic_api_key_file)
            gateway = self.builder(self.s, key)
            gateway.authenticate()
            return gateway
        except CredentialError as exc:
            self.io.say(f"No usable API key: {exc}. Run onboarding or 'set-key' first.")
        except GatewayError as exc:
            self.io.say(f"Stored API key failed authentication ({exc.kind}). Run 'set-key'.")
        return None

    def _select_model(self, gateway: ModelGateway) -> int:
        say = self.io.say
        try:
            models = gateway.list_models()
        except GatewayError as exc:
            say(f"Could not list models ({exc.kind}); nothing changed.")
            return 1
        if not models:
            say("The provider returned no models for this key; nothing changed.")
            return 1
        say("Models available to this key (from the provider's model-list endpoint):")
        for i, m in enumerate(models, 1):
            say(f"{i:>3}. {_describe(m)}")
        current = active_selection(self.engine)
        if current:
            say(f"Current selection: {current.model_id} ({current.auth_mode})")
        for _ in range(3):
            raw = self.io.ask(f"Select a model [1-{len(models)}]: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(models):
                break
            say("Invalid selection.")
        else:
            say("No model selected; nothing changed.")
            return 1
        chosen = models[int(raw) - 1]
        try:
            verified = gateway.get_model(chosen.id)  # provider-side validation
        except GatewayError as exc:
            say(f"Model {chosen.id} could not be validated ({exc.kind}); nothing changed.")
            return 1
        selection = ModelSelection(
            auth_mode="mock" if self.mock else "api_key", model_id=verified.id
        )
        with self.engine.begin() as conn:
            save_selection(conn, selection, _operator())
        say(f"Selected model: {verified.id} (auth mode: {selection.auth_mode}).")
        say(
            "New investigations use this model; investigations already started keep "
            "the model they were pinned to."
        )
        return 0

    # --- other commands ------------------------------------------------------------
    def change_model(self) -> int:
        gateway = self._existing_gateway()
        return 1 if gateway is None else self._select_model(gateway)

    def set_key(self) -> int:
        if self.mock:
            self.io.say("The mock gateway uses no key.")
            return 1
        self.io.say(BILLING_NOTE)
        return 0 if self._authenticate_new_key() is not None else 1

    def remove_key(self) -> int:
        removed = delete_api_key(self.s.anthropic_api_key_file)
        self.io.say(
            "API key removed; AI investigations will pause (monitoring continues)."
            if removed
            else "No API key was stored."
        )
        return 0

    def status(self) -> int:
        say = self.io.say
        sel = active_selection(self.engine)
        say(
            f"AI gateway: {self.s.ai_gateway}"
            + ("  (TEST/DEMO ONLY - not Claude)" if self.mock else "")
        )
        say(f"Subscription auth: unavailable - {SUBSCRIPTION.reason[:80]}...")
        say(f"Selected: {sel.model_id} ({sel.auth_mode})" if sel else "Selected: none")
        if not self.mock:
            try:
                key = read_api_key(self.s.anthropic_api_key_file)
                say(f"API key: configured ({fingerprint(key)})")
            except CredentialError as exc:
                say(f"API key: {exc}")
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT status, COALESCE(outcome, '-') AS outcome, count(*) FROM tasks "
                    "WHERE status IN ('awaiting_investigation','awaiting_policy','running') "
                    "GROUP BY 1, 2 ORDER BY 1, 2"
                )
            ).all()
        for status, outcome, n in rows:
            say(f"Tasks {status} ({outcome}): {n}")
        return 0

    # --- operators / approvals ---------------------------------------------------------
    def operator_add(self, name: str, role: str) -> int:
        from app.safety.approvals import create_operator

        if role not in ("viewer", "approver"):
            self.io.say("role must be 'viewer' or 'approver'")
            return 2
        with self.engine.begin() as conn:
            token = create_operator(conn, name, role, _operator())
        self.io.say(f"Operator {name!r} created with role {role}.")
        self.io.say(
            "Token (shown ONCE; only its SHA-256 is stored - keep it in a password manager):"
        )
        self.io.say(token)
        return 0

    def operator_disable(self, name: str) -> int:
        from app.safety.approvals import disable_operator

        with self.engine.begin() as conn:
            ok = disable_operator(conn, name, _operator())
        self.io.say("Operator disabled." if ok else "No such active operator.")
        return 0 if ok else 1

    def list_approvals(self) -> int:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, incident_id, proposed_action, target_service, action_fingerprint, "
                    "risk, expires_at FROM approvals WHERE status='pending' ORDER BY requested_at"
                )
            ).all()
        if not rows:
            self.io.say("No pending approvals.")
        for r in rows:
            self.io.say(f"approval {r.id}  incident {r.incident_id}")
            self.io.say(
                f"  action {r.proposed_action} on {r.target_service}; expires "
                f"{r.expires_at.isoformat()}"
            )
            self.io.say(f"  risk: {r.risk}")
            self.io.say(f"  action_fingerprint: {r.action_fingerprint}")
        self.io.say(
            "Decide via the authenticated API: POST /v1/approvals/<id>/approve|reject "
            'with {"action_fingerprint": ...} and an approver token.'
        )
        return 0

    # --- reports / notifications ---------------------------------------------------------
    def report_request(self, incident_id: str) -> int:
        """Request a NEW report version for an incident (e.g. after a failed job)."""
        import uuid as _uuid
        from datetime import UTC, datetime

        from app.reporting.jobs import request_report

        try:
            iid = _uuid.UUID(incident_id)
        except ValueError:
            self.io.say("incident id must be a UUID")
            return 2
        with self.engine.begin() as conn:
            exists = conn.execute(text("SELECT 1 FROM incidents WHERE id=:i"), {"i": iid}).first()
            if not exists:
                self.io.say("No such incident.")
                return 1
            job = request_report(
                conn,
                incident_id=iid,
                task_id=None,
                reason=f"operator_request:{datetime.now(UTC).isoformat(timespec='seconds')}",
                actor=_operator(),
                max_attempts=self.s.report_job_max_attempts,
            )
        self.io.say(f"Report job {job} queued; read it with GET /v1/incidents/{iid}/report.")
        return 0

    def notify_test(self) -> int:
        """Enqueue a harmless test notification through the durable pipeline."""
        from datetime import UTC, datetime

        from app.notifications.events import enqueue

        with self.engine.begin() as conn:
            eid = enqueue(
                conn,
                event_type="test",
                severity="info",
                dedup_key=f"test:{datetime.now(UTC).isoformat()}",
                actor=_operator(),
                payload={"title": "SentinelOps test notification", "summary": "Delivery test."},
            )
        self.io.say(f"Test notification {eid} queued; the notifier delivers it to all channels.")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinelops")
    parser.add_argument(
        "command",
        nargs="?",
        default="onboard",
        choices=[
            "onboard",
            "status",
            "change-model",
            "set-key",
            "remove-key",
            "operator-add",
            "operator-disable",
            "approvals",
            "report-request",
            "notify-test",
        ],
    )
    parser.add_argument("args", nargs="*")
    args = parser.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings.database_url)
    cli = Cli(settings, engine, real_io())
    try:
        if args.command == "operator-add":
            if len(args.args) != 2:
                parser.error("usage: operator-add NAME viewer|approver")
            return cli.operator_add(args.args[0], args.args[1])
        if args.command == "operator-disable":
            if len(args.args) != 1:
                parser.error("usage: operator-disable NAME")
            return cli.operator_disable(args.args[0])
        if args.command == "report-request":
            if len(args.args) != 1:
                parser.error("usage: report-request INCIDENT_ID")
            return cli.report_request(args.args[0])
        return {
            "onboard": cli.onboard,
            "status": cli.status,
            "change-model": cli.change_model,
            "set-key": cli.set_key,
            "remove-key": cli.remove_key,
            "approvals": cli.list_approvals,
            "notify-test": cli.notify_test,
        }[args.command]()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted; nothing further was changed.")
        return 130
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
