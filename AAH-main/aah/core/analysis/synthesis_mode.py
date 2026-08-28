#!/usr/bin/env python3
"""
Compute the adaptive synthesis mode from decision registry signals.

Reads the decision registry and manifest to determine whether the project
should use light, standard, or full architecture synthesis. Also returns
the activated Tier 2 sections based on project archetype.

Usage:
    aah run core.analysis.synthesis_mode compute \
      --registry "$AAH_DIR/decision-registry.yaml" \
      --manifest "$AAH_DIR/manifest.yaml"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aah.core.common.io_utils import read_yaml, write_yaml


# Archetype-gated Tier 2 section sets.
# Each archetype activates a predefined set of Tier 2 sections.
# Adding a new archetype = adding one entry here.
ARCHETYPE_SECTIONS: dict[str, list[str]] = {
    "shared": [],
    "ai-applications": [
        "data-knowledge-architecture",
        "security-trust-architecture",
        "model-llm-architecture",
        "agent-topology",
        "integration-tool-surface",
    ],
    "ai-platforms": [
        "data-knowledge-architecture",
        "security-trust-architecture",
        "model-llm-architecture",
        "agent-topology",
        "integration-tool-surface",
        "deployment-environment",
        "multitenancy-cost-model",
        "observability-operational",
        "validation-architecture",
    ],
    "ai-infra-platforms": [
        "data-knowledge-architecture",
        "security-trust-architecture",
        "model-llm-architecture",
        "agent-topology",
        "integration-tool-surface",
        "deployment-environment",
        "multitenancy-cost-model",
        "observability-operational",
        "validation-architecture",
    ],
    "data-modernization": [
        "data-knowledge-architecture",
        "migration-transformation",
        "validation-architecture",
    ],
}


def compute_synthesis_mode(registry: dict, manifest: dict) -> dict:
    """
    Compute synthesis mode from registry and manifest signals.

    Returns:
        {
            "synthesis_mode": "light" | "standard" | "full",
            "reason": str,
            "activated_sections": list[str],
            "archetype": str
        }
    """
    # Extract signals
    compliance_regimes = registry.get("context", {}).get("compliance_regimes", [])
    integrations = registry.get("context", {}).get("integrations", [])
    decisions = registry.get("decisions", [])
    resolved_ddrs = [d for d in decisions if d.get("status") == "resolved"]
    resolved_count = len(resolved_ddrs)
    integration_count = len(integrations)

    # Determine archetype from registry context or manifest
    archetype = registry.get("context", {}).get("archetype", "")
    if not archetype:
        archetype = manifest.get("archetype", "shared")
    archetype = archetype.lower().strip()

    # Mode determination — highest priority wins
    mode = "light"
    reason = "Default: no complexity signals detected"

    if compliance_regimes:
        mode = "full"
        reason = f"Compliance regimes present: {', '.join(compliance_regimes)}"
    elif archetype in ("ai-platforms", "ai-infra-platforms"):
        mode = "full"
        reason = f"Archetype '{archetype}' requires full synthesis"
    elif resolved_count > 40:
        mode = "full"
        reason = f"High DDR count ({resolved_count} resolved decisions)"
    elif integration_count > 8:
        mode = "full"
        reason = f"High integration count ({integration_count} integrations)"
    elif archetype == "ai-applications":
        mode = "standard"
        reason = f"Archetype '{archetype}' requires standard synthesis (floor)"
    elif archetype == "data-modernization":
        mode = "standard"
        reason = f"Archetype '{archetype}' requires standard synthesis (floor)"
    elif resolved_count >= 16:
        mode = "standard"
        reason = f"Moderate DDR count ({resolved_count} resolved decisions)"
    elif integration_count >= 3:
        mode = "standard"
        reason = f"Moderate integration count ({integration_count} integrations)"

    # Section activation — archetype-gated
    activated_sections = list(ARCHETYPE_SECTIONS.get(archetype, []))

    # Compliance modifier: add compliance-nfr-matrix if regimes present
    if compliance_regimes and "compliance-nfr-matrix" not in activated_sections:
        activated_sections.append("compliance-nfr-matrix")

    return {
        "synthesis_mode": mode,
        "reason": reason,
        "activated_sections": activated_sections,
        "archetype": archetype,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute synthesis mode")
    sub = parser.add_subparsers(dest="command", required=True)

    compute_p = sub.add_parser("compute", help="Compute synthesis mode from registry signals")
    compute_p.add_argument("--registry", type=Path, required=True, help="Path to decision-registry.yaml")
    compute_p.add_argument("--manifest", type=Path, required=True, help="Path to manifest.yaml")

    args = parser.parse_args()

    if args.command == "compute":
        if not args.registry.exists():
            print(f"Error: registry not found at {args.registry}", file=sys.stderr)
            sys.exit(1)
        if not args.manifest.exists():
            print(f"Error: manifest not found at {args.manifest}", file=sys.stderr)
            sys.exit(1)

        registry = read_yaml(args.registry)
        manifest = read_yaml(args.manifest)

        result = compute_synthesis_mode(registry, manifest)

        # Write synthesis_mode to manifest
        manifest["synthesis_mode"] = result["synthesis_mode"]
        write_yaml(manifest, args.manifest)

        # Output JSON to stdout
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
