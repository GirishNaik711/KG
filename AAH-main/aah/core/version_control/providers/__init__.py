"""Tracker provider adapters behind the TrackerProvider port.

Importing this package registers all built-in providers with the registry.
Add a new tracker by creating a sub-package, implementing TrackerProvider, and
calling ``register(name, cls)`` at the bottom of its provider module.
"""

# Importing the provider modules triggers their register() calls.
from aah.core.version_control.providers import github  # noqa: F401
