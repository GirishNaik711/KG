"""Startup environment-configuration check.

Fails the application at boot with ``ERR_CDR_78_EX_CONFIG`` when any required
variable is missing or empty, naming every absent variable at once and pointing
the user at ``.env.example``.

CONTRACT — every module that reads a new environment variable must:
  1. add it to ``REQUIRED_ENV`` below (name + one-line purpose comment), and
  2. add it to the committed ``.env.example`` catalog with a safe placeholder.

Call ``check_env()`` from the application entrypoint BEFORE any module reads
configuration, so a misconfiguration surfaces as this message rather than as a
downstream KeyError, a None-typed client, or a connection timeout.

This module deliberately has NO third-party dependency and does NOT import the
AAH harness — a delivered application must run standalone.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ERR_CDR_78_EX_CONFIG = "ERR_CDR_78_EX_CONFIG"

# Variables that MUST be present and non-empty at startup.
# One entry per line: "NAME",  # purpose — who reads it
REQUIRED_ENV: tuple[str, ...] = (
    "APP_ENV",           # runtime environment — application
    "BACKEND_CORS_ORIGINS",  # allowed frontend origins — API
)


class ConfigError(RuntimeError):
    """Raised when required environment configuration is missing or malformed."""


def load_dotenv(path: Path | None = None) -> None:
    """Load ``KEY=value`` pairs from the root ``.env`` without overriding the
    real environment.

    A minimal parser on purpose: no dependency, no interpolation, no export
    syntax. Values already set in the process environment always win, so
    container and CI configuration is never shadowed by a stale local file.
    """
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def missing_env(required: tuple[str, ...] = REQUIRED_ENV) -> list[str]:
    """Names from ``required`` that are absent or empty in the environment."""
    return [name for name in required if not (os.environ.get(name) or "").strip()]


def format_config_error(missing: list[str]) -> str:
    """The canonical ERR_CDR_78_EX_CONFIG failure text."""
    listed = "\n".join(f"      - {name}" for name in missing)
    return (
        f"{ERR_CDR_78_EX_CONFIG}  \u2014  configuration mismatch\n"
        "\n"
        "  \u2717 Required environment variables are missing:\n"
        f"{listed}\n"
        "  \u2192 Check .env.example, copy it to .env, and fill in proper values:\n"
        "      cp .env.example .env    # then edit .env\n"
        "  See .env.example for the full list and expected format."
    )


def check_env(required: tuple[str, ...] = REQUIRED_ENV) -> None:
    """Load ``.env``, then raise ``ConfigError`` if anything required is absent.

    Reports EVERY missing variable in one message — fixing configuration one
    restart at a time is the failure mode this exists to prevent.
    """
    load_dotenv()
    missing = missing_env(required)
    if missing:
        raise ConfigError(format_config_error(missing))


def main() -> int:
    """Standalone check: ``python config/env_check.py``."""
    try:
        check_env()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 78
    print("env check: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
