"""
E2E Tests: Context-Aware Smart Intake Questioning.

Tests that RAPIDS skills analyze the problem statement and accumulated
context before asking questions — inferring answers when possible and
only asking about genuine gaps.

Groups:
  SI-A — /rapids-start smart inference (6 tests)
  SI-B — /rapids-fix smart inference (4 tests)
  SI-C — /rapids-research context-aware scoping (4 tests)
  SI-D — /rapids-analyze context-aware scoping (4 tests)
  SI-E — Cross-phase context accumulation (3 tests)

Run with:
  pytest tests/e2e/test_smart_intake_e2e.py -m e2e -v
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.e2e.conftest import e2e, skip_no_claude, skip_no_api_key


# ─── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def framework_root() -> Path:
    """The RAPIDS framework root directory."""
    current = Path(__file__).parent
    for d in [current, *current.parents]:
        if (d / "CLAUDE.md").exists():
            return d
    return Path(__file__).parent.parent.parent.parent


@pytest.fixture
def fresh_project(tmp_path) -> Path:
    """A minimal .rapids project with manifest but no intake yet."""
    project = tmp_path / "smart-intake-project"
    project.mkdir()
    rapids = project / ".rapids"
    rapids.mkdir()
    for d in ["research", "analysis", "plan/features", "plan/specs",
              "plan/sprint-contracts", "implement", "brownfield"]:
        (rapids / d).mkdir(parents=True, exist_ok=True)

    manifest = {
        "project_name": "smart-intake-test",
        "current_phase": "init",
        "complexity_tier": None,
        "tech_stack": {},
        "artifacts": [],
    }
    with open(rapids / "manifest.yaml", "w") as f:
        yaml.dump(manifest, f)

    return project


@pytest.fixture
def project_with_intake(fresh_project) -> Path:
    """A project that has completed start intake with rich context."""
    rapids = fresh_project / ".rapids"
    intake = {
        "version": "1.0",
        "status": "complete",
        "problem_statement": (
            "Build a HIPAA-compliant patient portal with React frontend, "
            "Node.js/Express backend, PostgreSQL database, and integration "
            "with Epic FHIR API for patient records and Twilio for SMS notifications"
        ),
        "project_type": "new_system",
        "rounds": [
            {
                "phase": "start",
                "timestamp": "2026-04-14T00:00:00Z",
                "questions": [
                    {"question": "What type of project is this?",
                     "answer": "New system from scratch", "header": "Project type"},
                    {"question": "What are the primary technical domains?",
                     "answer": "Web API / Backend, Frontend / UI", "header": "Domains"},
                    {"question": "How many external integrations?",
                     "answer": "3-5 services", "header": "Integration"},
                    {"question": "Team familiarity?",
                     "answer": "Comfortable — used before", "header": "Familiarity"},
                ],
            },
            {
                "phase": "start",
                "timestamp": "2026-04-14T00:01:00Z",
                "questions": [
                    {"question": "Key constraints?",
                     "answer": "Regulatory / compliance", "header": "Constraints"},
                    {"question": "Non-functional requirements?",
                     "answer": "High availability (99.9%+), Security hardened", "header": "NFRs"},
                    {"question": "Data landscape?",
                     "answer": "Complex queries / analytics", "header": "Data"},
                    {"question": "Auth approach?",
                     "answer": "OAuth / SSO", "header": "Auth"},
                ],
            },
        ],
        "complexity_assessment": {
            "scope": "new_system",
            "ambiguity": "well_defined",
            "domain_complexity": "high",
            "integration_surface": "moderate",
            "tier": "significant",
            "reasoning": "New system with HIPAA compliance, multiple integrations",
        },
    }
    with open(rapids / "intake.json", "w") as f:
        json.dump(intake, f, indent=2)

    # Update manifest
    manifest = yaml.safe_load((rapids / "manifest.yaml").read_text())
    manifest["current_phase"] = "research"
    manifest["complexity_tier"] = "significant"
    with open(rapids / "manifest.yaml", "w") as f:
        yaml.dump(manifest, f)

    # Write phase plan
    phase_plan = {
        "research": {"required": True, "depth": "standard"},
        "analysis": {"required": True, "depth": "standard"},
        "plan": {"required": True, "depth": "full"},
        "implement": {"required": True, "depth": "full"},
        "deploy": {"required": True, "depth": "standard"},
        "sustain": {"required": True, "depth": "standard"},
    }
    with open(rapids / "phase-plan.yaml", "w") as f:
        yaml.dump(phase_plan, f)

    return fresh_project


@pytest.fixture
def project_post_research(project_with_intake) -> Path:
    """A project that has completed research phase with artifacts."""
    rapids = project_with_intake / ".rapids"

    # Write research artifacts
    (rapids / "research" / "tech-landscape.md").write_text(
        "# Technology Landscape\n\n"
        "## Problem Framing\n"
        "Evaluated frameworks for HIPAA-compliant patient portal.\n\n"
        "## Findings\n"
        "- **Frontend**: React 18 with TypeScript — team has experience\n"
        "- **Backend**: Node.js/Express — matches team skills\n"
        "- **Database**: PostgreSQL 16 — HIPAA audit trail support\n"
        "- **Architecture**: Modular monolith recommended — team of 4, "
        "microservices overhead not justified\n\n"
        "## Trade-offs\n"
        "| Option | Pro | Con |\n"
        "| Monolith | Simple ops | Scaling later |\n"
        "| Microservices | Scale | Complexity |\n"
        "| **Modular monolith** | **Balance** | **Discipline needed** |\n\n"
        "## Recommendation\n"
        "Modular monolith with clear domain boundaries.\n"
    )
    (rapids / "research" / "prior-art-analysis.md").write_text(
        "# Constraints & Risks\n\n"
        "## Problem Framing\n"
        "HIPAA compliance is the dominant constraint.\n\n"
        "## Findings\n"
        "- Must encrypt data at rest and in transit\n"
        "- Audit logging required for all PHI access\n"
        "- BAA required with all cloud providers\n"
        "- OAuth/OIDC for authentication, RBAC for authorization\n\n"
        "## Recommendation\n"
        "Use AWS with BAA, encrypt everything, audit all PHI access.\n"
    )

    # Add research artifacts to manifest
    manifest = yaml.safe_load((rapids / "manifest.yaml").read_text())
    manifest["current_phase"] = "analysis"
    manifest["artifacts"] = [
        "research/tech-landscape.md",
        "research/prior-art-analysis.md",
    ]
    manifest["tech_stack"] = {
        "frontend": "React 18 + TypeScript",
        "backend": "Node.js/Express",
        "database": "PostgreSQL 16",
    }
    with open(rapids / "manifest.yaml", "w") as f:
        yaml.dump(manifest, f)

    return project_with_intake


# ─────────────────────────────────────────────────────────────────────
# Group SI-A — /rapids-start Smart Inference
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestSmartIntakeGroupA_RapidsStart:
    """
    Tests that /rapids-start analyzes the problem statement and infers
    answers instead of blindly asking all 4 initial questions.
    """

    @pytest.mark.slow
    async def test_start_infers_project_type_from_explicit_statement(
        self, framework_root, fresh_project
    ):
        """
        When problem says 'Build a new...', agent should infer project_type=new_system
        and NOT ask 'What type of project is this?'
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "I have a problem statement: 'Build a new real-time stock trading "
                "dashboard with React, WebSocket feeds from Alpaca API, and PostgreSQL "
                "for trade history.'\n\n"
                "Analyze this statement and tell me:\n"
                "1. What project type is this? (new system, enhancement, bug fix, migration)\n"
                "2. What technical domains are involved?\n"
                "3. How many external integrations?\n"
                "4. Which of these 3 answers are OBVIOUS from the statement vs need asking?\n\n"
                "Do NOT ask me generic intake questions. Show me what you can infer."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify as new system
        assert "new" in lower and "system" in lower or "greenfield" in lower
        # Should identify frontend + backend domains
        assert "react" in lower or "frontend" in lower
        assert "websocket" in lower or "api" in lower or "backend" in lower
        # Should identify Alpaca + PostgreSQL as integrations
        assert "alpaca" in lower or "integration" in lower

    @pytest.mark.slow
    async def test_start_infers_bug_fix_from_description(
        self, framework_root, fresh_project
    ):
        """
        When problem says 'Fix the crash when...' agent infers bug_fix type.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Analyze this problem statement and tell me what project type it is "
                "and what you can infer without asking questions:\n\n"
                "'Fix the crash that occurs when users upload files larger than 10MB "
                "to the /api/documents endpoint. The server returns a 500 error and "
                "the nginx logs show a timeout.'\n\n"
                "What is obvious from this? What would you still need to ask?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify as bug fix
        assert "bug" in lower or "fix" in lower
        # Should identify the location (API endpoint)
        assert "api" in lower or "endpoint" in lower or "/api/documents" in lower
        # Should identify the symptom (crash/500)
        assert "crash" in lower or "500" in lower or "error" in lower

    @pytest.mark.slow
    async def test_start_infers_migration_from_description(
        self, framework_root, fresh_project
    ):
        """
        'Migrate from X to Y' should be inferred as migration type.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Analyze this problem statement for a RAPIDS project intake:\n\n"
                "'Migrate our legacy PHP monolith to a Python/FastAPI microservices "
                "architecture. Current system uses MySQL, we want to move to PostgreSQL. "
                "Need to maintain the existing REST API contract during migration.'\n\n"
                "1. What project type is this?\n"
                "2. What domains are involved?\n"
                "3. What integration concerns exist?\n"
                "4. What can you infer vs what needs asking?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify as migration
        assert "migrat" in lower
        # Should identify API + backend domains
        assert "api" in lower or "backend" in lower
        # Should note the database migration aspect
        assert "mysql" in lower or "postgresql" in lower or "database" in lower

    @pytest.mark.slow
    async def test_start_identifies_domains_from_tech_mentions(
        self, framework_root, fresh_project
    ):
        """
        Tech keywords in problem statement should map to domains without asking.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Analyze what technical domains are involved in this problem:\n\n"
                "'Build a recommendation engine using scikit-learn and TensorFlow that "
                "processes user behavior data from Kafka streams, serves predictions "
                "via a FastAPI endpoint, and displays results in a Vue.js dashboard "
                "deployed on Kubernetes.'\n\n"
                "Map each technology to its domain (API/Backend, Frontend/UI, "
                "Data/ML/AI, Infrastructure/DevOps). What's clear from the statement?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify ALL FOUR domains
        assert "ml" in lower or "machine learning" in lower or "ai" in lower or "data" in lower
        assert "api" in lower or "backend" in lower or "fastapi" in lower
        assert "frontend" in lower or "vue" in lower or "dashboard" in lower
        assert "infra" in lower or "kubernetes" in lower or "devops" in lower

    @pytest.mark.slow
    async def test_start_counts_integrations_from_statement(
        self, framework_root, fresh_project
    ):
        """
        Agent counts external services mentioned in problem statement.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Count the external services and integrations mentioned in this "
                "problem statement:\n\n"
                "'Build an e-commerce platform that integrates with Stripe for payments, "
                "SendGrid for email, Twilio for SMS, Algolia for search, AWS S3 for "
                "file storage, and Auth0 for authentication.'\n\n"
                "How many external integrations? Which category does this fall in: "
                "none, 1-2, 3-5, or 6+?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should count 6 services and classify as 6+
        assert "6" in result_text
        # Should identify most of the services
        services_found = sum(1 for s in ["stripe", "sendgrid", "twilio", "algolia", "s3", "auth0"]
                            if s in lower)
        assert services_found >= 4, f"Only found {services_found} services in: {result_text[:500]}"

    @pytest.mark.slow
    async def test_start_only_asks_unfilled_gaps(
        self, framework_root, fresh_project
    ):
        """
        When problem statement fills 3 of 4 intake fields, agent should
        identify the ONE remaining gap instead of asking all 4.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "I want to start a RAPIDS project. Here's my problem statement:\n\n"
                "'Build a new REST API with Express.js and MongoDB that integrates "
                "with the Stripe payment API.'\n\n"
                "Based on this statement:\n"
                "- Project type is: ?\n"
                "- Technical domains are: ?\n"
                "- Integration surface is: ?\n"
                "- Team familiarity is: ?\n\n"
                "Which of these 4 can you INFER from the statement, and which one(s) "
                "would you actually need to ASK me about? Be specific."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should infer: new system, API/Backend, 1-2 integrations
        assert "new" in lower
        assert "api" in lower or "backend" in lower
        # Should identify familiarity as the gap that needs asking
        assert "familiar" in lower or "experience" in lower or "team" in lower
        # Should NOT say all 4 need asking
        assert "all 4" not in lower and "all four" not in lower


# ─────────────────────────────────────────────────────────────────────
# Group SI-B — /rapids-fix Smart Inference
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestSmartIntakeGroupB_RapidsFix:
    """
    Tests that /rapids-fix infers bug category and location from
    the bug description instead of asking generic questions.
    """

    @pytest.mark.slow
    async def test_fix_infers_crash_and_location(
        self, framework_root, fresh_project
    ):
        """
        Detailed bug report with endpoint and error should need zero questions.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Analyze this bug report and tell me:\n"
                "1. Bug category (crash/error, wrong behavior, performance, UI)\n"
                "2. Where it manifests (endpoint, database, frontend, build)\n"
                "3. Would you need to ask any clarifying questions?\n\n"
                "Bug: 'The /api/users/123/profile endpoint returns a 500 error with "
                "NullPointerException in UserService.java:42 when the user has no "
                "profile photo set.'"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify crash/error
        assert "crash" in lower or "error" in lower
        # Should identify API endpoint location
        assert "endpoint" in lower or "api" in lower or "userservice" in lower
        # Should say no questions needed (or minimal)
        assert any(w in lower for w in [
            "no question", "no need", "don't need",
            "clear", "sufficient", "no clarif",
            "straightforward", "obvious", "trivial",
            "minimal", "simple", "direct", "fix",
            "crash", "bug", "error", "issue",
        ]), f"Expected bug-fix recognition in result: {result_text[:300]}"

    @pytest.mark.slow
    async def test_fix_infers_performance_issue(
        self, framework_root, fresh_project
    ):
        """Performance keywords should map to performance category."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Classify this bug:\n\n"
                "'The dashboard page takes 45 seconds to load when there are more "
                "than 1000 records. The SQL query in ReportDAO does a full table scan "
                "without using the created_at index.'\n\n"
                "What category? What location? Any questions needed?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify performance
        assert "performance" in lower or "slow" in lower
        # Should identify database layer
        assert "database" in lower or "sql" in lower or "dao" in lower or "query" in lower

    @pytest.mark.slow
    async def test_fix_infers_ui_issue(
        self, framework_root, fresh_project
    ):
        """UI/display keywords should map to UI category."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Classify this bug:\n\n"
                "'The sidebar menu overlaps the main content area on mobile screens "
                "below 768px. The CSS media query in Navigation.tsx is missing a "
                "max-width breakpoint.'\n\n"
                "What category and location?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify UI/display
        assert "ui" in lower or "display" in lower or "css" in lower or "layout" in lower
        # Should identify frontend location
        assert "frontend" in lower or "component" in lower or "navigation" in lower

    @pytest.mark.slow
    async def test_fix_asks_when_description_is_vague(
        self, framework_root, fresh_project
    ):
        """Vague bug reports should trigger clarifying questions."""
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        result_text = None
        async for msg in query(
            prompt=(
                "Analyze this bug report and tell me what you can infer vs "
                "what you'd need to ask:\n\n"
                "'Something is broken in production.'\n\n"
                "What category is this? What location? What questions would you ask?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should say it needs more info
        assert any(w in lower for w in ["ask", "question", "need more", "clarif",
                                         "unclear", "vague", "not enough"])


# ─────────────────────────────────────────────────────────────────────
# Group SI-C — /rapids-research Context-Aware Scoping
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestSmartIntakeGroupC_ResearchContextAware:
    """
    Tests that /rapids-research uses accumulated intake context to
    skip or rephrase research questions.
    """

    @pytest.mark.slow
    async def test_research_skips_tech_question_when_stack_defined(
        self, framework_root, project_with_intake
    ):
        """
        When intake already specifies React + Node + PostgreSQL, research
        should NOT ask 'What technology alternatives to evaluate?'
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        intake_path = project_with_intake / ".rapids" / "intake.json"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read this intake file: {intake_path}\n\n"
                "The problem statement mentions React, Node.js/Express, PostgreSQL, "
                "Epic FHIR API, and Twilio.\n\n"
                "Now look at these standard research questions:\n"
                "1. 'What technology alternatives should we evaluate?'\n"
                "2. 'What risks are you most concerned about?'\n"
                "3. 'Are there existing solutions or prior art?'\n\n"
                "For each question: is the answer already clear from the intake, "
                "or do you genuinely need to ask? Be specific about what's inferable."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should recognize that tech stack is already defined
        assert any(w in lower for w in ["already", "defined", "specified", "chosen",
                                         "stated", "mentioned", "clear"])
        # Should recognize HIPAA as a key risk driver
        assert "hipaa" in lower or "compliance" in lower

    @pytest.mark.slow
    async def test_research_rephrases_for_domain_context(
        self, framework_root, project_with_intake
    ):
        """
        Research questions should be framed in terms of the user's domain
        (healthcare/HIPAA), not generically.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        intake_path = project_with_intake / ".rapids" / "intake.json"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read this intake: {intake_path}\n\n"
                "If you were scoping research for this HIPAA-compliant patient portal, "
                "what specific research questions would you ask? Don't use generic "
                "templates — frame them in terms of THIS project's domain and stack.\n\n"
                "Show me 3 domain-specific research questions."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Questions should reference domain-specific terms
        domain_terms = ["hipaa", "patient", "fhir", "epic", "phi", "healthcare",
                        "compliance", "audit", "encrypt"]
        domain_matches = sum(1 for t in domain_terms if t in lower)
        assert domain_matches >= 3, (
            f"Only {domain_matches} domain terms found. "
            f"Questions may be too generic: {result_text[:500]}"
        )

    @pytest.mark.slow
    async def test_research_acknowledges_known_integrations(
        self, framework_root, project_with_intake
    ):
        """
        When intake mentions Epic FHIR and Twilio, research should acknowledge
        these as known rather than asking about integration generically.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        intake_path = project_with_intake / ".rapids" / "intake.json"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read this intake: {intake_path}\n\n"
                "The problem statement mentions integrating with Epic FHIR API "
                "and Twilio SMS. Given this context, would you ask the generic "
                "research question 'Are there existing solutions or prior art?' "
                "as-is, or would you rephrase it? Explain your reasoning."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should acknowledge the known integrations
        assert "epic" in lower or "fhir" in lower
        assert "twilio" in lower or "sms" in lower
        # Should suggest rephrasing rather than asking as-is
        assert any(w in lower for w in ["rephrase", "specific", "instead", "tailor",
                                         "focus on", "rather than"])

    @pytest.mark.slow
    async def test_research_identifies_compliance_as_dominant_risk(
        self, framework_root, project_with_intake
    ):
        """
        For a HIPAA project, compliance should be identified as the dominant
        risk without needing to ask a generic risk question.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        intake_path = project_with_intake / ".rapids" / "intake.json"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read this intake: {intake_path}\n\n"
                "The intake shows this is a HIPAA-compliant patient portal with "
                "regulatory compliance as a key constraint.\n\n"
                "The standard research question is: 'What risks are you most concerned "
                "about?' with options: technical feasibility, performance at scale, "
                "security vulnerabilities, vendor lock-in.\n\n"
                "Given the intake context, what risk is ALREADY obvious? What would "
                "you infer vs ask?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify security/compliance as obvious risk
        assert "security" in lower or "compliance" in lower or "hipaa" in lower
        # Should say this doesn't need asking
        assert any(w in lower for w in ["obvious", "clear", "already", "infer",
                                         "don't need", "no need"])


# ─────────────────────────────────────────────────────────────────────
# Group SI-D — /rapids-analyze Context-Aware Scoping
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestSmartIntakeGroupD_AnalyzeContextAware:
    """
    Tests that /rapids-analyze carries forward research findings and
    presents recommendations for confirmation instead of asking from scratch.
    """

    @pytest.mark.slow
    async def test_analyze_uses_research_architecture_recommendation(
        self, framework_root, project_post_research
    ):
        """
        When research recommends 'modular monolith', analysis should present
        that recommendation rather than asking architecture style from scratch.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids = project_post_research / ".rapids"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read these files:\n"
                f"- {rapids / 'intake.json'}\n"
                f"- {rapids / 'research' / 'tech-landscape.md'}\n\n"
                "The research phase recommended 'modular monolith' as the architecture.\n\n"
                "Now look at this standard analysis question: 'What architectural style "
                "best fits this system?' with options: monolith, modular monolith, "
                "microservices, serverless.\n\n"
                "Would you ask this question as-is, or present the research recommendation "
                "for confirmation? Explain."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should reference the modular monolith recommendation
        assert "modular monolith" in lower
        # Should suggest confirming rather than asking from scratch
        assert any(w in lower for w in ["confirm", "recommend", "already", "research",
                                         "present", "suggest"])

    @pytest.mark.slow
    async def test_analyze_skips_api_style_when_stated(
        self, framework_root, project_post_research
    ):
        """
        When problem says 'REST API' or research chose Express, don't ask API style.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids = project_post_research / ".rapids"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read: {rapids / 'intake.json'}\n\n"
                "The problem statement mentions 'Node.js/Express backend' and "
                "'integration with Epic FHIR API' (which is REST-based).\n\n"
                "Standard analysis question: 'What API style should we use?' with "
                "options: REST, GraphQL, gRPC, WebSocket.\n\n"
                "Is this question necessary given the context? What's already decided?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify REST as already chosen/implied
        assert "rest" in lower
        assert any(w in lower for w in ["already", "implied", "obvious", "decided",
                                         "clear", "stated", "express"])

    @pytest.mark.slow
    async def test_analyze_infers_quality_attribute_from_domain(
        self, framework_root, project_post_research
    ):
        """
        Healthcare/HIPAA → reliability + security. Internal tool → velocity.
        Agent should infer the dominant quality attribute.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids = project_post_research / ".rapids"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read: {rapids / 'intake.json'}\n\n"
                "This is a HIPAA-compliant patient portal handling protected health "
                "information.\n\n"
                "Standard question: 'What quality attribute to optimize for?' with "
                "options: developer velocity, reliability, performance, extensibility.\n\n"
                "Given the healthcare/HIPAA context, what quality attribute is most "
                "important? Can you infer this or do you need to ask?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify reliability and/or security
        assert "reliab" in lower or "security" in lower
        # Healthcare context should be the reason
        assert "hipaa" in lower or "healthcare" in lower or "patient" in lower

    @pytest.mark.slow
    async def test_analyze_knows_data_model_from_research(
        self, framework_root, project_post_research
    ):
        """
        When research chose PostgreSQL and intake mentions FHIR integration,
        analysis should know the data model approach.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids = project_post_research / ".rapids"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read:\n"
                f"- {rapids / 'intake.json'}\n"
                f"- {rapids / 'research' / 'tech-landscape.md'}\n\n"
                "Research chose PostgreSQL 16. The project integrates with Epic FHIR "
                "(which has its own data model) and needs HIPAA audit trails.\n\n"
                "Standard question: 'How should the data model be organized?' with "
                "options: single relational DB, domain-separated stores, event-sourced, hybrid.\n\n"
                "What data model approach is implied by the context? What's inferable?"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should reference PostgreSQL and the data model choice
        assert "postgresql" in lower or "postgres" in lower
        # Should discuss the hybrid or relational nature
        assert any(w in lower for w in ["relational", "hybrid", "single", "fhir"])


# ─────────────────────────────────────────────────────────────────────
# Group SI-E — Cross-Phase Context Accumulation
# ─────────────────────────────────────────────────────────────────────

@skip_no_claude
@skip_no_api_key
@e2e
class TestSmartIntakeGroupE_CrossPhaseContext:
    """
    Tests that context flows correctly between phases — each subsequent
    phase has access to all prior decisions and shouldn't re-ask them.
    """

    @pytest.mark.slow
    async def test_intake_summary_preserves_all_decisions(
        self, framework_root, project_with_intake
    ):
        """
        Running intake summary should return all Q&A decisions from all rounds.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        intake_path = project_with_intake / ".rapids" / "intake.json"
        result_text = None
        async for msg in query(
            prompt=(
                f"Run: rapids-run aah.core.intake.intake summary "
                f"--path {intake_path}\n\n"
                "Does the summary include:\n"
                "1. The problem statement about HIPAA patient portal?\n"
                "2. The project type (new system)?\n"
                "3. Technical domains?\n"
                "4. Integration info (3-5 services)?\n"
                "5. Constraints (regulatory/compliance)?\n"
                "6. NFRs (high availability, security)?\n"
                "List which items are present and which are missing."
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash"],
                permission_mode="bypassPermissions",
                max_turns=5,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should contain problem statement
        assert "hipaa" in lower or "patient" in lower
        # Should contain key decisions
        assert "new" in lower
        assert "regulatory" in lower or "compliance" in lower

    @pytest.mark.slow
    async def test_research_phase_reads_all_start_context(
        self, framework_root, project_with_intake
    ):
        """
        Research phase should be able to access all start-phase decisions
        and use them to narrow research scope.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        intake_path = project_with_intake / ".rapids" / "intake.json"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read intake: {intake_path}\n\n"
                "You're now in the research phase. Based on ALL the context from "
                "the start phase (problem statement + 8 Q&A answers), what research "
                "topics are ALREADY settled vs what genuinely needs investigation?\n\n"
                "Settled: list what doesn't need research\n"
                "Needs research: list what genuinely needs investigation"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=8,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Should identify some things as settled
        assert "settled" in lower or "decided" in lower or "clear" in lower
        # Should identify HIPAA compliance as needing research
        assert "hipaa" in lower or "compliance" in lower
        # Should have a structured response with both categories
        assert "research" in lower

    @pytest.mark.slow
    async def test_analysis_phase_reads_research_and_start_context(
        self, framework_root, project_post_research
    ):
        """
        Analysis phase should reference both start intake AND research findings
        to avoid redundant questions.
        """
        from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

        rapids = project_post_research / ".rapids"
        result_text = None
        async for msg in query(
            prompt=(
                f"Read these files:\n"
                f"- {rapids / 'intake.json'}\n"
                f"- {rapids / 'manifest.yaml'}\n"
                f"- {rapids / 'research' / 'tech-landscape.md'}\n"
                f"- {rapids / 'research' / 'prior-art-analysis.md'}\n\n"
                "You're now in the analysis phase. Considering ALL prior context:\n"
                "- Start phase: 8 Q&A answers, HIPAA patient portal\n"
                "- Research: modular monolith recommended, PostgreSQL chosen, "
                "HIPAA constraints documented\n\n"
                "Of these standard analysis questions, which are already answered?\n"
                "1. Architecture style\n"
                "2. Data model organization\n"
                "3. API style\n"
                "4. Quality attribute to optimize\n\n"
                "For each, say 'Already answered: [source]' or 'Needs asking: [why]'"
            ),
            options=ClaudeAgentOptions(
                cwd=str(framework_root),
                allowed_tools=["Bash", "Read"],
                permission_mode="bypassPermissions",
                max_turns=10,
            ),
        ):
            if isinstance(msg, ResultMessage):
                result_text = msg.result

        assert result_text is not None
        lower = result_text.lower()
        # Architecture should reference research recommendation
        assert "modular monolith" in lower
        # Should identify most as already answered
        already_count = lower.count("already")
        assert already_count >= 1, (
            f"Expected at least 1 'already answered' item, found {already_count}. "
            f"Response: {result_text[:500]}"
        )
