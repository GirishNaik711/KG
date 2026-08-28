"""Host-agnostic hook definitions — source-of-truth data only.

``aah-hooks.yaml`` defines the framework's hooks once, in a host-neutral
form; ``settings-defaults.json`` carries per-host default env/permissions. The
install adapters (``aah/core/install/adapters/*.py``) render these into each
host's native format. This package holds no code.
"""
