"""API-key storage and credential hygiene.

* The key lives in ONE file (a dedicated Docker volume or a secrets-manager
  mount), mode 0600, readable only by the service user. It is never written to
  PostgreSQL, logs, the container environment, or the repository.
* ``assert_clean_environment`` refuses to run if ambient Anthropic credential
  variables are present: SentinelOps always passes its key explicitly, and an
  ambient ``ANTHROPIC_*`` credential (or a signed-in CLI profile) must never be
  picked up by accident.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import SecretStr

# Variables the Anthropic SDK / CLI would otherwise consult for credentials,
# profiles, federation, extra headers or an alternative host.
FORBIDDEN_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BASE_URL",
)
MIN_KEY_LENGTH = 20
MAX_KEY_LENGTH = 400


class CredentialError(Exception):
    """Missing, malformed or ambiguous credentials. Message never contains the key."""


def assert_clean_environment(environ: Mapping[str, str] | None = None) -> None:
    env = os.environ if environ is None else environ
    present = sorted(name for name in FORBIDDEN_ENV if name in env)
    if present:
        raise CredentialError(
            "refusing to use ambient Anthropic credentials/config from the environment: "
            + ", ".join(present)
            + ". SentinelOps reads its API key only from its key file."
        )


def validate_key_format(raw: str) -> str:
    key = raw.strip()
    if not (MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH) or any(c.isspace() for c in key):
        raise CredentialError("API key has an invalid format")
    return key


def fingerprint(key: SecretStr) -> str:
    """Non-reversible identifier safe to display/log (never the key itself)."""
    return "sha256:" + hashlib.sha256(key.get_secret_value().encode()).hexdigest()[:12]


def read_api_key(path: Path) -> SecretStr:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CredentialError("no API key configured (run the onboarding command)") from exc
    except OSError as exc:
        raise CredentialError(f"API key file unreadable: {type(exc).__name__}") from exc
    return SecretStr(validate_key_format(raw))


def write_api_key(path: Path, key: SecretStr) -> None:
    """Atomically replace the key file with 0600 permissions."""
    value = validate_key_format(key.get_secret_value())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, value.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def delete_api_key(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
