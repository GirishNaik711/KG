"""RAPIDS version_control module.

A pluggable, feature.md-authoritative issue-tracker sync engine. ``feature.md`` is
the single source of truth; the tracker (GitHub Issues today, Jira later) is a
projection. Conflict handling is non-blocking: the engine records conflicts and
prints them for the LLM, which decides and calls back into ``resolve``.

See html_plans/version-control-module.html for the design.
"""
