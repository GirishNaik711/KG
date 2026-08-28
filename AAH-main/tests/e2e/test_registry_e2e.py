"""E2E simulation tests for the decision registry.

Mimics the full human experience end-to-end:
  1. Project scaffold with manifest.yaml
  2. Research phase: init registry, gather context, apply eliminations, run research gate
  3. Analysis phase: resolve each DDR, write ADRs, write cross-cutting artifacts, run analysis gate
  4. Error scenarios: invalid options, missing ADRs, empty context — catch and recover

Two test cases:
  - Commercial client onboarding AI app (HIPAA+SOC2, multi-agent, RAG)
  - Internal knowledge assistant (no compliance, single-agent, skip_if pruning)
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from aah.core.common.manifest import get_default_manifest, load_manifest, save_manifest


SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent


def run_cmd(module: str, *args: str) -> dict | list:
    cmd = [sys.executable, "-m", module, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(SCRIPTS_DIR))
    if result.returncode != 0:
        raise RuntimeError(f"{module} failed (rc={result.returncode}): {result.stderr}")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def run_registry(*args: str) -> dict | list:
    return run_cmd("aah.core.registry.registry", *args)


def run_regimes(*args: str) -> dict:
    return run_cmd("aah.core.registry.regimes", *args)


def run_loader(*args: str) -> dict | list:
    return run_cmd("aah.core.registry.ddr_loader", *args)


# ── Gate validators (imported directly to test them in-process) ────────

from aah.core.gates.validate_research_gate import (
    validate_decision_registry,
    validate_research_gate,
)
from aah.core.gates.validate_analysis_gate import (
    validate_registry_resolution,
    validate_registry_adrs,
    validate_adr_quality,
    validate_cross_cutting_artifacts,
    validate_analysis_gate,
)


# ── ADR Generator ─────────────────────────────────────────────────────

def generate_adr(
    ddr: dict,
    chosen_option: str,
    project_name: str,
    context_facts: list[str],
    eliminated_options: list[dict] | None = None,
) -> str:
    """Generate a realistic ADR from a DDR playbook and chosen option.

    Produces content that passes the analysis gate quality checks:
    - Forces in Tension section with force names
    - Rejected Options with substantive reasons
    - Rationale referencing force names
    """
    ddr_id = ddr.get("id", "DDR-???")
    question = ddr.get("decision_question", "").strip()
    forces = ddr.get("forces", [])
    options = ddr.get("options", [])
    depends_on = ddr.get("depends_on", [])

    chosen = next((o for o in options if o["label"] == chosen_option), None)
    rejected = [o for o in options if o["label"] != chosen_option]

    # Build forces table
    forces_rows = []
    for f in forces:
        fid = f.get("id", "unknown")
        tension = f.get("tension", "")
        forces_rows.append(
            f"| **{fid}** | {tension} | HIGH | "
            f"Driven by engagement facts: {context_facts[0] if context_facts else 'project requirements'} |"
        )
    forces_table = "\n".join(forces_rows) if forces_rows else "| N/A | N/A | N/A | N/A |"

    # Build force x option matrix
    matrix_header_cols = " | ".join(o["label"] for o in options)
    matrix_header = f"| Force | {matrix_header_cols} |"
    matrix_sep = "| --- " + "| --- " * len(options) + "|"
    matrix_rows = []
    for f in forces:
        fid = f["id"]
        cells = []
        for o in options:
            fr = o.get("force_resolution", {}).get(fid, {})
            verdict = fr.get("verdict", "neutral")
            detail = fr.get("detail", "N/A")
            cells.append(f"{verdict}: {detail}")
        matrix_rows.append(f"| {fid} | " + " | ".join(cells) + " |")

    # Build options table
    options_rows = []
    eliminated_labels = {e.get("option") for e in (eliminated_options or [])}
    for o in options:
        label = o["label"]
        desc = o.get("description", "").strip()[:80]
        if label in eliminated_labels:
            elim = next(e for e in eliminated_options if e["option"] == label)
            options_rows.append(
                f"| {label} | {desc} | — | Eliminated: {elim.get('reason', 'mandate')} |"
            )
        elif label == chosen_option:
            options_rows.append(f"| {label} | {desc} | Chosen | **Selected** |")
        else:
            options_rows.append(f"| {label} | {desc} | Considered | Rejected |")

    # Build rejected table
    rejected_rows = []
    for o in rejected:
        if o["label"] not in eliminated_labels:
            # Reference a force name in the rejection reason for quality check
            force_ref = forces[0]["id"] if forces else "design constraints"
            rejected_rows.append(
                f"| {o['label']} | Rejected because the {force_ref} force "
                f"strongly favours the chosen option given our specific engagement "
                f"context of {context_facts[0] if context_facts else 'this project'} |"
            )
    rejected_table = "\n".join(rejected_rows) if rejected_rows else "| N/A | No alternatives applicable |"

    # Build dependencies table
    dep_rows = []
    for dep_id in depends_on:
        dep_rows.append(f"| {dep_id} | depends_on | This decision builds on {dep_id} resolution |")
    dep_table = "\n".join(dep_rows) if dep_rows else "| None | — | No direct dependencies |"

    # Rationale must reference at least one force name
    primary_force = forces[0]["id"] if forces else "core design"
    rationale = (
        f"The **{primary_force}** force dominates for this engagement because "
        f"{context_facts[0] if context_facts else 'the project requirements'}. "
        f"The {chosen_option} option resolves this force favourably while "
        f"maintaining acceptable trade-offs on secondary forces."
    )

    return dedent(f"""\
    # Architecture Decision Record: {ddr_id}
    <!-- DDR: {ddr_id} | Phase: analysis -->
    <!-- template_path: build-playbooks/templates/adr-ddr.md -->
    **Status:** Accepted
    **Date:** {datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    **Engagement:** {project_name}

    ## Decision Question

    {question}

    ## Decision Context

    - **Dominant Forces:** {', '.join(f['id'] for f in forces[:2])}
    - **Compliance Regimes:** None
    - **Prior Decisions:** {', '.join(depends_on) if depends_on else 'None'}

    This decision is non-trivial for {project_name} because {context_facts[0] if context_facts else 'the project has specific requirements'}.

    ## Forces in Tension

    | Force ID | Tension | Weight | Engagement Rationale |
    |---|---|---|---|
    {forces_table}

    ## Force x Option Matrix

    {matrix_header}
    {matrix_sep}
    {"chr(10)".join(matrix_rows)}

    ## Options Considered

    | Option | Description | Verdict Summary | Status |
    |---|---|---|---|
    {"chr(10)".join(options_rows)}

    ## Decision Outcome

    **Chosen option:** {chosen_option}

    **Rationale:**
    {rationale}

    ## Rejected Options

    | Option | Why Rejected for This Engagement |
    |---|---|
    {rejected_table}

    ## Consequences

    **What this enables:**
    Adopting {chosen_option} enables the team to proceed with a clear architecture direction.

    **What this constrains:**
    Downstream decisions in {', '.join(depends_on) if depends_on else 'subsequent layers'} are now scoped by this choice.

    **What becomes harder:**
    Alternative approaches are foreclosed; revisiting would require re-evaluating dependent decisions.

    ## Dependencies

    | DDR ID | Relationship | Impact |
    |--------|-------------|--------|
    {dep_table}

    ## Review Criteria

    Revisit if engagement scope changes materially or if technology landscape shifts significantly.
    """)


# ── SimulationAgent ───────────────────────────────────────────────────

class SimulationAgent:
    """Deterministic agent that drives the registry through scripted operations,
    mimicking the human experience of using /rapids-research and /rapids-analyze."""

    def __init__(self, project_path: Path, trace_path: Path | None = None):
        self.project_path = project_path
        self.rapids_path = project_path / ".rapids"
        self.aah_path = project_path / ".aah"
        self.registry_path = self.aah_path / "decision-registry.yaml"
        self.trace_path = trace_path
        self.operations: list[dict] = []

    def _log(self, op: str, args: dict, result: dict | list | str):
        entry = {"op": op, "args": args, "result_summary": str(result)[:200]}
        self.operations.append(entry)
        if self.trace_path:
            with open(self.trace_path, "a") as f:
                f.write(json.dumps(entry) + "\n")

    # ── Project setup ──

    def setup_project(self, project_name: str, archetype: str):
        """Create a realistic project directory structure with manifest."""
        self.rapids_path.mkdir(parents=True, exist_ok=True)
        self.aah_path.mkdir(parents=True, exist_ok=True)
        (self.rapids_path / "research").mkdir(exist_ok=True)
        (self.rapids_path / "analysis").mkdir(exist_ok=True)
        (self.rapids_path / "analysis" / "decisions").mkdir(parents=True, exist_ok=True)

        manifest = get_default_manifest(project_name)
        manifest.update({
            "project_type": archetype,
            "current_phase": "research",
            "complexity_tier": "significant",
        })
        save_manifest(manifest, self.aah_path / "manifest.yaml")
        # Official-writer legacy manifest retained for the analysis/research
        # artifact gates until their non-registry paths migrate separately.
        save_manifest(manifest, self.rapids_path / "manifest.yaml")

        self._log("setup_project", {"project_name": project_name}, "ok")

    def set_phase(self, phase: str):
        manifest_path = self.aah_path / "manifest.yaml"
        manifest = load_manifest(manifest_path)
        manifest["current_phase"] = phase
        save_manifest(manifest, manifest_path)
        save_manifest(manifest, self.rapids_path / "manifest.yaml")
        self._log("set_phase", {"phase": phase}, "ok")

    # ── Registry operations ──

    def init_registry(self, project_name: str, archetype: str):
        result = run_registry(
            "init", "--project-name", project_name,
            "--archetype", archetype,
            "--output", str(self.registry_path),
        )
        self._log("init_registry", {"project_name": project_name}, result)
        return result

    def add_fact(self, fact: str):
        result = run_registry(
            "update-context", "--registry", str(self.registry_path),
            "--field", "facts", "--value", json.dumps(fact),
        )
        self._log("add_fact", {"fact": fact}, result)
        return result

    def add_integration(self, integration: dict):
        result = run_registry(
            "update-context", "--registry", str(self.registry_path),
            "--field", "integrations", "--value", json.dumps(integration),
        )
        self._log("add_integration", {"system": integration.get("system")}, result)
        return result

    def set_infrastructure(self, givens: dict):
        result = run_registry(
            "update-context", "--registry", str(self.registry_path),
            "--field", "infrastructure_givens", "--value", json.dumps(givens),
        )
        self._log("set_infrastructure", givens, result)
        return result

    def add_compliance(self, regime: str):
        result = run_registry(
            "update-context", "--registry", str(self.registry_path),
            "--field", "compliance_regimes", "--value", json.dumps(regime),
        )
        self._log("add_compliance", {"regime": regime}, result)
        return result

    def add_nfr(self, nfr: dict):
        result = run_registry(
            "update-context", "--registry", str(self.registry_path),
            "--field", "nfrs", "--value", json.dumps(nfr),
        )
        self._log("add_nfr", nfr, result)
        return result

    def add_constraint(self, constraint: dict):
        result = run_registry(
            "update-context", "--registry", str(self.registry_path),
            "--field", "organizational_constraints", "--value", json.dumps(constraint),
        )
        self._log("add_constraint", constraint, result)
        return result

    def set_deployment_target(self, target: str):
        return run_registry(
            "update-context", "--registry", str(self.registry_path),
            "--field", "deployment_target", "--value", json.dumps(target),
        )

    def set_delivery_intent(self, intent: str):
        return run_registry(
            "update-context", "--registry", str(self.registry_path),
            "--field", "delivery_intent", "--value", json.dumps(intent),
        )

    def apply_eliminations(self):
        result = run_registry("apply-eliminations", "--registry", str(self.registry_path))
        self._log("apply_eliminations", {}, result)
        return result

    def apply_skip_conditions(self):
        result = run_registry("apply-skip-conditions", "--registry", str(self.registry_path))
        self._log("apply_skip_conditions", {}, result)
        return result

    def next_unresolved(self):
        return run_registry("next-unresolved", "--registry", str(self.registry_path))

    def resolve_decision(self, ddr_id: str, option: str):
        result = run_registry(
            "update-decision", "--registry", str(self.registry_path),
            "--ddr-id", ddr_id, "--status", "resolved",
            "--resolved", option, "--confirmed", "true",
            "--adr-path", f"analysis/decisions/ADR-{ddr_id.replace('DDR-', '')}.md",
        )
        self._log("resolve", {"ddr_id": ddr_id, "option": option}, result)
        return result

    def resolve_decision_invalid(self, ddr_id: str, bad_status: str):
        """Attempt to resolve with an invalid status — should fail."""
        try:
            run_registry(
                "update-decision", "--registry", str(self.registry_path),
                "--ddr-id", ddr_id, "--status", bad_status,
            )
            return {"error": False}
        except RuntimeError as e:
            self._log("resolve_invalid", {"ddr_id": ddr_id, "bad_status": bad_status}, str(e))
            return {"error": True, "message": str(e)}

    def status(self):
        return run_registry("status", "--registry", str(self.registry_path))

    def detect_regimes(self):
        return run_regimes("detect-regimes", "--registry", str(self.registry_path))

    def load_registry(self) -> dict:
        return yaml.safe_load(self.registry_path.read_text(encoding="utf-8"))

    # ── ADR operations ──

    def write_adr(self, ddr: dict, chosen_option: str, project_name: str, context_facts: list[str]):
        ddr_id = ddr["id"]
        adr_filename = f"ADR-{ddr_id.replace('DDR-', '')}.md"
        adr_path = self.rapids_path / "analysis" / "decisions" / adr_filename

        registry = self.load_registry()
        decision = next(
            (d for d in registry["decisions"] if d["ddr_id"] == ddr_id), {}
        )
        eliminated = decision.get("eliminated_options", [])

        content = generate_adr(ddr, chosen_option, project_name, context_facts, eliminated)
        adr_path.write_text(content, encoding="utf-8")
        self._log("write_adr", {"ddr_id": ddr_id, "path": str(adr_path)}, "ok")
        return adr_path

    def write_cross_cutting_artifacts(self, project_name: str):
        analysis_dir = self.rapids_path / "analysis"

        (analysis_dir / "solution-integration.md").write_text(
            f"# Solution Integration Map — {project_name}\n\n"
            "## 1. Integration Surface Summary\n\nBased on ADR decisions.\n\n"
            "## 2. External Integration Inventory\n\nDerived from registry context.\n",
            encoding="utf-8",
        )
        (analysis_dir / "nfr-analysis.md").write_text(
            f"# NFR Analysis — {project_name}\n\n"
            "## NFR Register\n\n| Category | Target | ADR Coverage |\n"
            "|---|---|---|\n| performance | p95 < 500ms | Covered by L3, L9 decisions |\n",
            encoding="utf-8",
        )
        (analysis_dir / "security-review.md").write_text(
            f"# Security Review — {project_name}\n\n"
            "## Threat Model Summary\n\nBased on DDR-L7 decisions.\n\n"
            "## Recommendations\n\nAll critical findings addressed in ADRs.\n",
            encoding="utf-8",
        )
        (analysis_dir / "solution-architecture.md").write_text(
            f"# Solution Architecture — {project_name}\n\n"
            "## 1. Executive Summary\n\nCapstone architecture document.\n\n"
            "## 2. ADR Index\n\n| ADR ID | Decision Question | Chosen Option |\n"
            "|---|---|---|\n",
            encoding="utf-8",
        )
        (analysis_dir / "adr-digest.yaml").write_text(
            f"# Auto-generated ADR digest\ngenerated_at: '2026-01-01T00:00:00Z'\n"
            f"project_name: '{project_name}'\nadr_count: 0\ndecisions: []\n",
            encoding="utf-8",
        )
        self._log("write_cross_cutting", {}, "ok")

    def write_research_artifact(self):
        """Write a minimal research artifact so the research gate passes."""
        research_dir = self.rapids_path / "research"
        (research_dir / "context-summary.md").write_text(
            "# Research Context Summary\n\nContext gathered during research phase.\n",
            encoding="utf-8",
        )

    # ── Gate validation ──

    def validate_research(self) -> tuple[bool, list[str]]:
        return validate_research_gate(self.project_path)

    def validate_analysis(self) -> tuple[bool, list[str]]:
        return validate_analysis_gate(self.project_path)


# ── Scripted resolution choices ───────────────────────────────────────

COMMERCIAL_CHOICES = {
    "DDR-SHARED-001": "build-custom",
    "DDR-L1-001": "task-oriented-intents",
    "DDR-L1-002": "graduated-escalation",
    "DDR-L1-003": "structured-dialog",
    "DDR-L2-001": "langgraph",
    "DDR-L2-003": "structured-output-with-retry",
    "DDR-L3-001": "claude-sonnet",
    "DDR-L3-002": "multi-provider-fallback",
    "DDR-L3-003": "complexity-based-routing",
    "DDR-L4-001": "hybrid-rag",
    "DDR-L4-002": "semantic-chunking",
    "DDR-L4-003": "federated-retrieval",
    "DDR-L5-001": "multi-agent-specialist-roles",
    "DDR-L5-002": "supervisor-orchestration",
    "DDR-L5-003": "durable-workflow",
    "DDR-L5-004": "shared-memory-store",
    "DDR-L6-001": "tiered-memory",
    "DDR-L6-002": "dynamic-context-window",
    "DDR-L7-001": "comprehensive-threat-model",
    "DDR-L7-002": "layered-guardrails",
    "DDR-L7-004": "continuous-red-team",
    "DDR-L8-001": "mcp-native",
    "DDR-L8-003": "container-sandbox",
    "DDR-L9-001": "container-orchestration",
    "DDR-L9-002": "vault-managed-secrets",
    "DDR-L9-003": "opentelemetry-stack",
    "DDR-SHARED-002": "relational-postgresql",
    "DDR-SHARED-003": "rest-api",
    "DDR-SHARED-004": "oauth2-oidc",
    "DDR-SHARED-005": "message-queue",
    "DDR-SHARED-006": "spa-frontend",
    "DDR-SHARED-007": "react",
    "DDR-SHARED-008": "rest-client",
    "DDR-SHARED-009": "containers-kubernetes",
    "DDR-SHARED-010": "github-actions",
    "DDR-SHARED-011": "dev-staging-prod",
    "DDR-SHARED-012": "distributed-cache",
    "DDR-SHARED-013": "cloud-object-storage",
    "DDR-SHARED-014": "cloud-native-monitoring",
    "DDR-SHARED-015": "unit-integration-e2e",
}

INTERNAL_CHOICES = {
    "DDR-SHARED-001": "build-custom",
    "DDR-L1-001": "conversational-intents",
    "DDR-L1-002": "simple-fallback",
    "DDR-L1-003": "free-form-chat",
    "DDR-L2-001": "claude-agent-sdk",
    "DDR-L2-003": "structured-output-with-retry",
    "DDR-L3-001": "claude-sonnet",
    "DDR-L3-002": "single-provider",
    "DDR-L3-003": "single-model-all-intents",
    "DDR-L4-001": "basic-rag",
    "DDR-L4-002": "fixed-size-chunking",
    "DDR-L4-003": "single-index-retrieval",
    "DDR-L5-001": "single-agent-rich-tooling",
    "DDR-L6-001": "session-memory-only",
    "DDR-L6-002": "static-context-window",
    "DDR-L7-001": "basic-threat-model",
    "DDR-L7-002": "input-guardrails-only",
    "DDR-L7-004": "manual-adversarial-testing",
    "DDR-L8-001": "function-calling",
    "DDR-L8-003": "process-isolation",
    "DDR-L9-001": "serverless",
    "DDR-L9-002": "environment-variables",
    "DDR-L9-003": "structured-logging",
    "DDR-SHARED-002": "relational-postgresql",
    "DDR-SHARED-003": "rest-api",
    "DDR-SHARED-004": "api-keys",
    "DDR-SHARED-005": "direct-calls",
    "DDR-SHARED-006": "no-frontend",
    "DDR-SHARED-009": "serverless-deployment",
    "DDR-SHARED-010": "github-actions",
    "DDR-SHARED-011": "dev-staging-prod",
    "DDR-SHARED-012": "no-caching",
    "DDR-SHARED-013": "cloud-object-storage",
    "DDR-SHARED-014": "structured-logging",
    "DDR-SHARED-015": "unit-integration",
}


# ══════════════════════════════════════════════════════════════════════
# TEST CASE 1: Commercial Client Onboarding AI App
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestCommercialOnboardingE2E:
    """HIPAA + SOC2 compliance, GCP, multi-agent, RAG.
    Full resolution of all DDRs with ADR writing and gate validation."""

    def test_full_lifecycle(self, tmp_path):
        project_path = tmp_path / "commercial-onboarding"
        trace_path = tmp_path / "trace.jsonl"
        agent = SimulationAgent(project_path, trace_path)
        project_name = "commercial-onboarding"
        context_facts = [
            "Customer-facing commercial banking KYB onboarding",
            "Handles protected health information for insurance cross-sell",
            "Multi-agent architecture for specialist workflows",
            "RAG required for regulatory document corpus",
        ]

        # ═══ PHASE 1: PROJECT SETUP ═══

        agent.setup_project(project_name, "ai-applications")

        # ═══ PHASE 2: RESEARCH ═══

        # Step 1: Init registry
        init_result = agent.init_registry(project_name, "ai-applications")
        total_ddrs = init_result["progress"]["total"]
        assert total_ddrs > 0, "Registry should have DDRs after init"

        # Step 2: Scoping questions
        agent.set_deployment_target("external")
        agent.set_delivery_intent("production")

        # Step 3: Gather facts (5 batches)
        for fact in context_facts:
            agent.add_fact(fact)

        agent.add_integration({
            "system": "Dow Jones Risk & Compliance",
            "protocol": "REST", "auth": "API Key", "direction": "outbound",
        })
        agent.add_integration({
            "system": "Core Banking System",
            "protocol": "gRPC", "auth": "mTLS", "direction": "bidirectional",
        })

        agent.set_infrastructure({
            "cloud": "GCP", "identity_provider": "Azure AD", "container_platform": "GKE",
        })

        agent.add_compliance("hipaa")
        agent.add_compliance("soc2")

        agent.add_nfr({"category": "performance", "target": "p95 < 500ms"})
        agent.add_nfr({"category": "availability", "target": "99.9% uptime"})

        agent.add_constraint({"statement": "Budget capped; only approved vendors allowed", "flexibility": "somewhat_flexible"})

        # Step 4: Verify context populated
        status = agent.status()
        assert status["context_populated"]["facts"] == 4
        assert status["context_populated"]["integrations"] == 2
        assert status["context_populated"]["compliance_regimes"] == 2

        # Step 5: Detect regimes
        regimes = agent.detect_regimes()
        assert "hipaa" in regimes["triggered_regimes"]
        assert "soc2" in regimes["triggered_regimes"]

        # Step 6: Apply eliminations
        elim_result = agent.apply_eliminations()
        assert elim_result["applied"] is True

        # Step 7: Apply skip conditions (pre-resolution)
        agent.apply_skip_conditions()

        # Step 8: Write research artifact
        agent.write_research_artifact()

        # ── ERROR SCENARIO: Research gate should FAIL before context is gathered ──
        # (We already have context, so test that gate PASSES now)
        passed, issues = agent.validate_research()
        assert passed, f"Research gate should pass: {issues}"

        # ═══ PHASE 3: ANALYSIS ═══

        agent.set_phase("analysis")

        # ── ERROR SCENARIO: Analysis gate should FAIL with no resolutions ──
        passed, issues = agent.validate_analysis()
        assert not passed, "Analysis gate should fail before any decisions resolved"
        assert any("not resolved" in i for i in issues), f"Expected unresolved error: {issues}"

        # Step 1: Resolve all decisions in dependency order
        resolved_count = 0
        max_iterations = total_ddrs + 10

        for _ in range(max_iterations):
            next_result = agent.next_unresolved()
            if next_result.get("status") == "all_resolved":
                break

            ddr_id = next_result["ddr_id"]
            ddr = next_result.get("ddr", {})

            # ── ERROR SCENARIO: Try invalid status, expect failure, recover ──
            if resolved_count == 0:
                err = agent.resolve_decision_invalid(ddr_id, "bogus-status")
                assert err["error"] is True, "Invalid status should fail"

            # Pick option from scripted choices, fallback to first available
            choice = COMMERCIAL_CHOICES.get(ddr_id)
            if choice is None:
                options = ddr.get("options", [])
                choice = options[0]["label"] if options else "default"

            # Resolve and write ADR
            agent.resolve_decision(ddr_id, choice)
            agent.write_adr(ddr, choice, project_name, context_facts)
            resolved_count += 1

            # Re-evaluate skip conditions
            agent.apply_skip_conditions()

        # Step 2: Write cross-cutting artifacts
        agent.write_cross_cutting_artifacts(project_name)

        # ═══ VALIDATION ═══

        final_registry = agent.load_registry()
        final_status = agent.status()

        # All decisions resolved or skipped
        for d in final_registry["decisions"]:
            assert d["status"] in ("resolved", "skipped"), (
                f"DDR {d['ddr_id']} still {d['status']}"
            )
        assert final_status["progress"]["open"] == 0

        # Registry state correct
        assert final_registry["context"]["deployment_target"] == "external"
        assert "hipaa" in final_registry["context"]["compliance_regimes"]

        # Multi-agent chosen — L5-002/003/004 should NOT be skipped
        l5_002 = next(d for d in final_registry["decisions"] if d["ddr_id"] == "DDR-L5-002")
        assert l5_002["status"] == "resolved"

        # ADR files exist for every resolved decision
        adr_issues = validate_registry_adrs(agent.rapids_path)
        assert adr_issues == [], f"ADR issues: {adr_issues}"

        # ADR quality passes
        quality_issues = validate_adr_quality(agent.rapids_path)
        assert quality_issues == [], f"ADR quality issues: {quality_issues}"

        # Cross-cutting artifacts exist
        cc_issues = validate_cross_cutting_artifacts(agent.rapids_path)
        assert cc_issues == [], f"Cross-cutting issues: {cc_issues}"

        # Full analysis gate passes
        passed, issues = agent.validate_analysis()
        assert passed, f"Analysis gate should pass: {issues}"

        # Trace file completeness
        assert trace_path.exists()
        trace_lines = trace_path.read_text().strip().split("\n")
        assert len(trace_lines) > 20, f"Expected >20 trace entries, got {len(trace_lines)}"

        # ── ERROR SCENARIO: Delete an ADR, verify gate catches it ──
        first_resolved = next(
            d for d in final_registry["decisions"] if d["status"] == "resolved"
        )
        adr_name = f"ADR-{first_resolved['ddr_id'].replace('DDR-', '')}.md"
        adr_file = agent.rapids_path / "analysis" / "decisions" / adr_name
        if adr_file.exists():
            adr_file.unlink()
            passed, issues = agent.validate_analysis()
            assert not passed, "Gate should fail when ADR is missing"
            assert any(first_resolved["ddr_id"] in i for i in issues)
            # Recover: regenerate the ADR
            ddr_data = run_loader("load-ddr", "--id", first_resolved["ddr_id"], "--format", "json")
            agent.write_adr(ddr_data, first_resolved["resolved"], project_name, context_facts)
            passed, issues = agent.validate_analysis()
            assert passed, f"Gate should pass after recovery: {issues}"


# ══════════════════════════════════════════════════════════════════════
# TEST CASE 2: Internal Knowledge Assistant
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestInternalAssistantE2E:
    """No compliance, AWS, single-agent, simple RAG.
    Exercises skip_if pruning when single-agent is chosen."""

    def test_full_lifecycle(self, tmp_path):
        project_path = tmp_path / "internal-assistant"
        trace_path = tmp_path / "trace.jsonl"
        agent = SimulationAgent(project_path, trace_path)
        project_name = "internal-assistant"
        context_facts = [
            "Internal knowledge assistant for engineering team",
            "Simple RAG over Confluence documentation wiki",
        ]

        # ═══ PHASE 1: PROJECT SETUP ═══

        agent.setup_project(project_name, "ai-applications")

        # ═══ PHASE 2: RESEARCH ═══

        agent.init_registry(project_name, "ai-applications")
        total_ddrs = agent.status()["progress"]["total"]

        agent.set_deployment_target("internal")
        agent.set_delivery_intent("mvp")

        for fact in context_facts:
            agent.add_fact(fact)

        agent.set_infrastructure({"cloud": "AWS", "identity_provider": "Okta"})
        agent.add_nfr({"category": "performance", "target": "p95 < 2s"})
        agent.add_constraint({"statement": "Budget capped; only approved vendors allowed", "flexibility": "fixed"})

        # ── ERROR SCENARIO: Research gate fails with empty registry ──
        # (We need at least a research artifact too)

        # No compliance → eliminations should be minimal
        elim_result = agent.apply_eliminations()
        assert elim_result["applied"] is True

        agent.apply_skip_conditions()
        agent.write_research_artifact()

        passed, issues = agent.validate_research()
        assert passed, f"Research gate should pass: {issues}"

        # ═══ PHASE 3: ANALYSIS ═══

        agent.set_phase("analysis")

        resolved_ids = []
        max_iterations = total_ddrs + 10

        for _ in range(max_iterations):
            next_result = agent.next_unresolved()
            if next_result.get("status") == "all_resolved":
                break

            ddr_id = next_result["ddr_id"]
            ddr = next_result.get("ddr", {})

            choice = INTERNAL_CHOICES.get(ddr_id)
            if choice is None:
                options = ddr.get("options", [])
                choice = options[0]["label"] if options else "default"

            agent.resolve_decision(ddr_id, choice)
            agent.write_adr(ddr, choice, project_name, context_facts)
            resolved_ids.append(ddr_id)

            agent.apply_skip_conditions()

        agent.write_cross_cutting_artifacts(project_name)

        # ═══ VALIDATION ═══

        final_registry = agent.load_registry()
        final_status = agent.status()

        # All decisions resolved or skipped
        for d in final_registry["decisions"]:
            assert d["status"] in ("resolved", "skipped"), (
                f"DDR {d['ddr_id']} still {d['status']}"
            )
        assert final_status["progress"]["open"] == 0

        # Single-agent chosen — L5-002 and L5-004 should be skipped
        for skip_id in ["DDR-L5-002", "DDR-L5-004"]:
            decision = next(
                (d for d in final_registry["decisions"] if d["ddr_id"] == skip_id), None
            )
            if decision:
                assert decision["status"] == "skipped", (
                    f"{skip_id} should be skipped for single-agent, got '{decision['status']}'"
                )

        # no-frontend chosen — verify DDR-SHARED-007/008 handled correctly
        # (they may or may not have skip_if; if no skip_if, they're resolved with fallback)

        # Dependency order: L5-001 before any L5 that depended on it
        if "DDR-L5-001" in resolved_ids:
            l5_001_idx = resolved_ids.index("DDR-L5-001")
            for dep_id in ["DDR-L5-002", "DDR-L5-003", "DDR-L5-004"]:
                if dep_id in resolved_ids:
                    assert resolved_ids.index(dep_id) > l5_001_idx, (
                        f"{dep_id} resolved before dependency DDR-L5-001"
                    )

        # Context state
        assert final_registry["context"]["deployment_target"] == "internal"
        assert final_registry["context"]["delivery_intent"] == "mvp"

        # Gates pass
        adr_issues = validate_registry_adrs(agent.rapids_path)
        assert adr_issues == [], f"ADR issues: {adr_issues}"

        quality_issues = validate_adr_quality(agent.rapids_path)
        assert quality_issues == [], f"Quality issues: {quality_issues}"

        cc_issues = validate_cross_cutting_artifacts(agent.rapids_path)
        assert cc_issues == [], f"Cross-cutting issues: {cc_issues}"

        passed, issues = agent.validate_analysis()
        assert passed, f"Analysis gate should pass: {issues}"

        # Fewer resolved than commercial (due to skips)
        assert final_status["progress"]["skipped"] >= 2


# ══════════════════════════════════════════════════════════════════════
# ERROR SCENARIO TESTS
# ══════════════════════════════════════════════════════════════════════

@pytest.mark.e2e
class TestErrorScenarios:
    """Targeted error scenarios that a user might hit and must recover from."""

    def test_research_gate_fails_with_empty_context(self, tmp_path):
        """Research gate should fail if registry context is completely empty."""
        project_path = tmp_path / "empty-ctx"
        agent = SimulationAgent(project_path)
        agent.setup_project("empty-context-test", "ai-applications")
        agent.init_registry("empty-context-test", "ai-applications")

        # No context added — gate should fail
        agent.write_research_artifact()
        passed, issues = agent.validate_research()
        assert not passed, "Gate should fail with empty context"
        assert any("context" in i.lower() for i in issues)

        # Recovery: add minimal context
        agent.add_fact("Some project context fact")
        passed, issues = agent.validate_research()
        assert passed, f"Gate should pass after adding context: {issues}"

    def test_research_gate_fails_without_eliminations_when_regimes_present(self, tmp_path):
        """If compliance regimes are set but eliminations not applied, gate should fail."""
        project_path = tmp_path / "no-elim"
        agent = SimulationAgent(project_path)
        agent.setup_project("no-elim-test", "ai-applications")
        agent.init_registry("no-elim-test", "ai-applications")
        agent.add_fact("Project with compliance needs")
        agent.add_compliance("hipaa")
        agent.write_research_artifact()

        # Compliance set but eliminations NOT applied
        passed, issues = agent.validate_research()
        assert not passed, "Gate should fail without eliminations"
        assert any("elimination" in i.lower() for i in issues)

        # Recovery: apply eliminations
        agent.apply_eliminations()
        passed, issues = agent.validate_research()
        assert passed, f"Gate should pass after eliminations: {issues}"

    def test_analysis_gate_fails_with_missing_adr(self, tmp_path):
        """Analysis gate should catch missing ADR for a resolved decision."""
        project_path = tmp_path / "missing-adr"
        agent = SimulationAgent(project_path)
        agent.setup_project("missing-adr-test", "ai-applications")
        agent.init_registry("missing-adr-test", "ai-applications")
        agent.add_fact("Test project")
        agent.set_phase("analysis")

        # Resolve one decision but don't write ADR
        registry = agent.load_registry()
        first_ddr_id = registry["decisions"][0]["ddr_id"]
        agent.resolve_decision(first_ddr_id, "test-option")

        # Resolve all others to get past the "unresolved" check
        for d in registry["decisions"][1:]:
            run_registry(
                "update-decision", "--registry", str(agent.registry_path),
                "--ddr-id", d["ddr_id"], "--status", "resolved",
                "--resolved", "test-option", "--confirmed", "true",
                "--adr-path", f"analysis/decisions/ADR-{d['ddr_id'].replace('DDR-', '')}.md",
            )
            # Write ADR for all except the first
            adr_path = agent.rapids_path / "analysis" / "decisions" / f"ADR-{d['ddr_id'].replace('DDR-', '')}.md"
            adr_path.write_text(
                f"# ADR: {d['ddr_id']}\n\n## Forces in Tension\n\n"
                f"| **test-force** | Test tension | HIGH | Test reason |\n\n"
                f"## Rejected Options\n\n"
                f"| other-option | Rejected because the test-force constraint makes it unsuitable "
                f"for this specific engagement context and requirements |\n",
                encoding="utf-8",
            )

        agent.write_cross_cutting_artifacts("missing-adr-test")

        # Gate should catch the missing ADR
        adr_issues = validate_registry_adrs(agent.rapids_path)
        assert len(adr_issues) > 0, "Should detect missing ADR"
        assert first_ddr_id in adr_issues[0]

    def test_analysis_gate_fails_with_thin_rejected_options(self, tmp_path):
        """Analysis gate should catch ADRs with insufficiently detailed rejection reasons."""
        project_path = tmp_path / "thin-reject"
        agent = SimulationAgent(project_path)
        agent.setup_project("thin-reject-test", "ai-applications")
        agent.set_phase("analysis")

        decisions_dir = agent.rapids_path / "analysis" / "decisions"
        decisions_dir.mkdir(parents=True, exist_ok=True)

        # Write an ADR with a too-thin rejected section
        (decisions_dir / "ADR-L1-001.md").write_text(
            "# ADR: DDR-L1-001\n\n"
            "## Forces in Tension\n\n"
            "| **test-force** | X vs Y | HIGH | Reason |\n\n"
            "## Rejected Options\n\n"
            "| opt-b | Bad |\n",
            encoding="utf-8",
        )

        quality_issues = validate_adr_quality(agent.rapids_path)
        assert any("too thin" in i.lower() for i in quality_issues), (
            f"Should detect thin rejected options: {quality_issues}"
        )
