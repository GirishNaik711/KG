#!/usr/bin/env python3
"""Error-code taxonomy for code AAH scaffolds INTO a delivered application.

These codes belong to the **generated app**, not to the harness. A scaffolded
project's startup env checker raises ``ERR_CDR_78_EX_CONFIG`` when a required
variable is missing or malformed, and this module is the single place the code
and its user-facing message shape are defined so every stack's loader template
renders the same failure.

Naming convention — ``ERR_<AREA>_<NN>_<KIND>_<TOPIC>``:

  * ``AREA``  — subsystem owning the failure (``CDR`` = core delivery runtime)
  * ``NN``    — stable numeric slot within the area; never reused once assigned
  * ``KIND``  — ``EX`` for an exception the app raises and exits on
  * ``TOPIC`` — what went wrong (``CONFIG``, …)

Keep this minimal. Add an entry only when a scaffolded app needs to fail with a
code a user will see and search for; the harness's own failures use structured
verification-failure codes instead (see ``verification_contracts``).
"""

# Missing or malformed required environment configuration at application
# startup. The ONLY entry today — this taxonomy exists because the env checker
# needs a stable, greppable identifier, not because a large code space is
# planned.
ERR_CDR_78_EX_CONFIG = "ERR_CDR_78_EX_CONFIG"

#: Every code defined here, for tests and docs to enumerate.
ERROR_CODES = (ERR_CDR_78_EX_CONFIG,)

#: The canonical failure text a scaffolded app prints. ``{missing_block}`` is a
#: pre-rendered, newline-joined list of ``      - VAR_NAME`` lines. Every stack's
#: loader template renders THIS string, so the failure UX is identical whether
#: the app is Python, Node, Go, or Java.
ERR_CDR_78_EX_CONFIG_MESSAGE = """\
{code}  —  configuration mismatch

  ✗ Required environment variables are missing:
{missing_block}
  → Check .env.example, copy it to .env, and fill in proper values:
      cp .env.example .env    # then edit .env
  See .env.example for the full list and expected format."""


def render_config_error(missing: list[str]) -> str:
    """Render the ``ERR_CDR_78_EX_CONFIG`` failure text for ``missing`` keys.

    Used by tests and by any harness-side tooling that needs to show the same
    message the scaffolded app would print. The app itself does not import this
    module — it carries the rendered template in its own checker, so a delivered
    application never depends on the harness at runtime.
    """
    missing_block = "\n".join(f"      - {name}" for name in missing)
    return ERR_CDR_78_EX_CONFIG_MESSAGE.format(
        code=ERR_CDR_78_EX_CONFIG, missing_block=missing_block
    )
