#!/usr/bin/env python3
"""Delegation guard — checks if the active project is delegated to a plugin.

Used by all RAPIDS guards and gates to bypass validation when execution
has been delegated to an external plugin (e.g., Data Assist Toolkit).
"""

import sys


def is_delegated_project() -> bool:
    """Check if the active project has execution_mode == 'delegated'.

    Returns False on any error (no manifest, missing field, etc.)
    so that RAPIDS guards continue to function normally.
    """
    try:
        from aah.core.common.manifest import load_manifest
        manifest = load_manifest()
        return manifest.get("execution_mode") == "delegated"
    except (SystemExit, Exception):
        return False


def exit_if_delegated() -> None:
    """Exit 0 (success/no-op) if the project is delegated.

    Call this at the top of any guard or gate main() function
    to skip validation for plugin-managed projects.
    """
    if is_delegated_project():
        sys.exit(0)
