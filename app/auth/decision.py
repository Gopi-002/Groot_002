"""The verified authentication decision (2026-09-22). Rationale, quotes and
sources: docs/auth-decision.md. Change this only after re-verifying the
official Anthropic documentation - never because a CLI happens to be signed in."""

from __future__ import annotations

from dataclasses import dataclass

VERIFIED_ON = "2026-09-22"
SDK_PACKAGE = "anthropic"  # official Anthropic Client SDK (Python)
SDK_MIN_VERSION = "1.8.0"


@dataclass(frozen=True)
class AuthOption:
    key: str
    label: str
    supported: bool
    reason: str


SUBSCRIPTION = AuthOption(
    key="subscription",
    label="Claude Subscription",
    supported=False,
    reason=(
        "Subscription integration unavailable for this application. Anthropic does not "
        "allow third-party developers to offer claude.ai login or rate limits for their "
        "products unless previously approved (SentinelOps has no such approval), and "
        "subscription limits are reserved for Anthropic's own apps. SentinelOps will not "
        "reuse Claude Code or claude.ai credentials."
    ),
)

API_KEY = AuthOption(
    key="api_key",
    label="Anthropic API Key",
    supported=True,
    reason=(
        "Officially supported for servers and unattended workloads via the Anthropic "
        "Client SDK. Billed pay-as-you-go by the Claude Console; API billing is "
        "separate from any Claude subscription (Pro/Max/Team/Enterprise)."
    ),
)

SOURCES = (
    "https://code.claude.com/docs/en/agent-sdk/overview",
    "https://support.claude.com/en/articles/13189465-log-in-to-your-claude-account",
    "https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan",
    "https://platform.claude.com/docs/en/manage-claude/authentication",
)
