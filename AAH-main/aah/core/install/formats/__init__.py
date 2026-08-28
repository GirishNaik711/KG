"""Config-file format primitives (JSON / TOML) with ``_aah`` provenance markers.

Reusable across adapters; not tied to any single host. An adapter picks the
module matching its host's config format (``jsonconf`` for Claude's
``settings.json``, ``tomlconf`` for Codex's ``config.toml``).
"""
