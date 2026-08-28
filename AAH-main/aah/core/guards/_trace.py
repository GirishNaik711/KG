"""Optional success-path tracing for AAH guards.

Off by default. Set ``AAH_GUARD_TRACE=1`` to emit one stderr line
per guard invocation that ends without blocking. Useful for
post-incident audits that need to prove a guard ran (vs. silently
no-op'ing).

Output format (one line per call):

    [aah-guard:trace] {guard_name} {action} target={target}

Where ``action`` is ``allow`` (guard ran and allowed the operation),
``noop`` (guard short-circuited because the configuration it consults
is absent), or a guard-specific label like ``tier1-allow``. Blocks
(``sys.exit(2)``) are already visible via stderr; this helper covers
the silent paths only.

Why opt-in: in default operation, blocks are the only signal we want
to surface. Tracing every allow path on every keystroke would flood
the user's terminal with noise. The env-var gate keeps the production
default quiet while giving auditors a switch they can flip without a
code change.
"""

from __future__ import annotations

import os
import sys


_ENV_VAR = "AAH_GUARD_TRACE"


def trace(guard_name: str, action: str, target: str = "") -> None:
    """Emit a single trace line iff AAH_GUARD_TRACE=1.

    Only the literal string ``"1"`` enables tracing. Other truthy-ish
    values (``"true"``, ``"yes"``, ``"0"``) are intentionally rejected
    so a user typo doesn't accidentally enable tracing in production.
    """
    if os.environ.get(_ENV_VAR) != "1":
        return
    msg = f"[aah-guard:trace] {guard_name} {action}"
    if target:
        msg += f" target={target}"
    print(msg, file=sys.stderr)
