"""aah-discuss deterministic backbone.

Modules:
    guidance             Loader for aah/_resources/_references/discuss/decisions_guidance.yaml
    registry             CRUD over .aah/discuss/decision-registry.yaml
    validate_constraints Constraint gate (Checks A-F) — the §10a hard gate
    activations          Flatten + dedup option.activates[] across the registry

The discuss-prd.md file is authored by /aah-discuss Step 10b directly from the
registry + confirmed header + the template at aah/_resources/_templates/discuss/discuss-prd.md.
Output lands at .aah/discuss/discuss-prd.md. No Python renderer.
"""
