# E2E Test Portfolio

Comprehensive end-to-end tests for the RAPIDS delivery framework. Tests are split into two categories:

1. **Agent SDK tests** — spawn real Claude Code sessions via `claude_agent_sdk`, consume API tokens, 30-120s each
2. **Script-level tests** — call RAPIDS Python scripts and CLI commands directly, no Claude sessions needed

**Total: 305 E2E tests (114 Agent SDK + 191 script-level)**

## Prerequisites

- `claude-agent-sdk` Python package installed (`uv pip install claude-agent-sdk`)
- One of: `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_AUTH_TOKEN`, or `CLAUDE_CODE_USE_BEDROCK` set
- `claude` CLI available on PATH
- `rapids-run` on PATH (`uv tool install ./scripts`)

## Running

```bash
# All E2E tests (305 tests, includes both Agent SDK and script-level)
pytest tests/e2e/ -v

# Agent SDK tests only (114 tests — spawns real Claude sessions)
pytest tests/e2e/ -m e2e -v

# Script-level tests only (191 tests — no Claude sessions)
pytest tests/e2e/ -m "not e2e" -v

# By file
pytest tests/e2e/test_agent_e2e.py -m e2e -v           # Core framework (14)
pytest tests/e2e/test_activity_system_e2e.py -m e2e -v  # Activity system (16)
pytest tests/e2e/test_agent_sdk_hardening.py -m e2e -v  # Hardening (19)
pytest tests/e2e/test_smart_intake_e2e.py -m e2e -v     # Smart intake (21)
pytest tests/e2e/test_comprehensive_agent_e2e.py -m e2e -v  # Comprehensive (41)

# A specific group
pytest tests/e2e/ -m e2e -v -k "GroupH"
pytest tests/e2e/ -m e2e -v -k "GroupL"
```

---

## Test Files Overview

| File | Agent SDK | Script-level | Total | Coverage Area |
|------|-----------|-------------|-------|---------------|
| `test_agent_e2e.py` | 14 | 0 | 14 | Core skills, orchestrator, hooks, planning, intake |
| `test_activity_system_e2e.py` | 16 | 44 | 60 | Activity library, fast-path, recommendation, iteration CLI |
| `test_agent_sdk_hardening.py` | 19 | 0 | 19 | Context layers, domain intel, summaries, knowledge graph, demo, coverage, hooks |
| `test_smart_intake_e2e.py` | 21 | 0 | 21 | Smart intake, /rapids-start, /rapids-fix, research/analysis context |
| `test_comprehensive_agent_e2e.py` | 41 | 0 | 41 | Scenarios, iterations, profiler, phases, registry, errors, workflows |
| `test_greenfield_lifecycle.py` | 3 | 48 | 51 | Scaffold, status skill, workspace init, orchestrator, full lifecycle |
| `test_hooks.py` | 0 | 61 | 61 | Hook enforcement via CLI (no-mocks, validate-path, artifact templates) |
| `test_archetype_flows_e2e.py` | 0 | 13 | 13 | Archetype classification, activity loading, DAG validation |
| `test_phase_guards_e2e.py` | 0 | 13 | 13 | Phase gate validation (research, analysis, plan gates) |
| `test_brownfield_lifecycle.py` | 0 | 7 | 7 | Brownfield scaffold, profiler, manifest |
| `test_context_layers_e2e.py` | 0 | 5 | 5 | 3-layer context system integration |
| **Total** | **114** | **191** | **305** | |

---

## Agent SDK Test Groups (A-Z)

### `test_agent_e2e.py` — Core Framework (Groups A, B, C, E, G)

#### Group A — Core Skills (3 tests)

Tests user-invocable RAPIDS skills through real Claude sessions.

| # | Test | What it does |
|---|------|-------------|
| A1 | `test_rapids_status_shows_phase_and_feature_info` | Runs /rapids-status and verifies it returns phase and feature count. |
| A2 | `test_rapids_fix_creates_trivial_intake` | Runs /rapids-fix and verifies it creates intake.json with trivial tier. |
| A3 | `test_rapids_usage_returns_token_data` | Runs /rapids-usage and verifies it returns token usage information. |

#### Group B — Orchestrator Behaviour (3 tests)

Tests orchestrator commands produce correct output through Claude sessions.

| # | Test | What it does |
|---|------|-------------|
| B1 | `test_frontier_command_shows_blocked_features` | Agent runs frontier and reports available vs blocked features with reasons. |
| B2 | `test_wave_context_returns_resolved_paths` | Agent runs wave-context and returns feature descriptions with resolved YAML paths. |
| B3 | `test_next_action_dispatch_with_features_available` | Agent runs next-action and returns dispatch action when features are pending. |

#### Group C — Hook Enforcement (3 tests)

Tests PreToolUse hooks intercept and block disallowed actions in live Claude sessions.

| # | Test | What it does |
|---|------|-------------|
| C1 | `test_no_mocks_guard_blocks_pytest_mock_install` | Hook blocks `pip install pytest-mock` during a live session. |
| C2 | `test_no_mocks_guard_allows_normal_install` | Hook allows non-mock pip installs to proceed. |
| C3 | `test_validate_rapids_path_blocks_wrong_phase_write` | Hook blocks writing to .rapids/research/ during implement phase. |

#### Group E — Planning Pipeline (2 tests)

Tests planning pipeline CLI commands orchestrated through Claude sessions.

| # | Test | What it does |
|---|------|-------------|
| E1 | `test_build_dag_and_waves_via_agent` | Agent builds feature list, DAG, and waves from scratch in sequence. |
| E2 | `test_wave_context_output_has_yaml_paths` | Agent runs wave-context and verifies features have yaml_path and contract_path. |

#### Group G — Intake Classification (3 tests)

Tests intake and classification system through Claude sessions.

| # | Test | What it does |
|---|------|-------------|
| G1 | `test_trivial_bug_fix_classified_correctly` | Bug fix returns trivial tier with research not required. |
| G2 | `test_complex_system_classified_as_significant_or_complex` | Complex system gets significant or complex tier. |
| G3 | `test_questionnaire_initial_returns_four_questions` | Questionnaires initial returns 4 structured questions. |

---

### `test_activity_system_e2e.py` — Activity System (Groups H, I, J, K)

#### Group H — Activity Library Operations (4 tests)

Tests that the agent can operate the activity library CLI to list, validate, and inspect activities.

| # | Test | What it does |
|---|------|-------------|
| H1 | `test_list_activities_for_ai_agents` | Asks the agent to run `loader list --project-type ai-agents --format ids` and report which IDs start with R-AI vs R-SHARED. Verifies the response mentions both shared and AI-specific activity IDs, and references analysis-phase activities. |
| H2 | `test_validate_all_activities_pass` | Asks the agent to run `loader validate` and report how many activities are valid vs invalid. Verifies the response includes "39" (total count) and confirms all pass with no errors. |
| H3 | `test_activity_dag_shows_waves` | Asks the agent to run `loader dag --project-type ai-agents` and describe the execution waves and dependencies. Verifies the response mentions "wave" and includes activity IDs. |
| H4 | `test_activity_context_for_llm` | Asks the agent to run `loader context --project-type data-pipelines` and list the included activities. Verifies the response references data-specific and shared activities. |

#### Group I — Fast-Path Detection (4 tests)

Tests that the agent correctly classifies problem statements as bug fixes, enhancements, or full projects, and recommends appropriate phase skipping.

| # | Test | What it does |
|---|------|-------------|
| I1 | `test_bug_fix_classified_and_phases_skipped` | Gives the agent a bug-fix problem statement ("Fix the authentication bug in login page that crashes on null email") and asks it to run `fast_path detect`. Verifies the response identifies it as a bug and indicates research/analysis phases should be skipped. |
| I2 | `test_full_project_classified_with_all_phases` | Gives the agent a complex project statement ("Design and build a comprehensive multi-agent customer support platform") and asks for classification. Verifies it's classified as "full" with all phases required. |
| I3 | `test_enhancement_classified_correctly` | Gives the agent a simple enhancement ("Add a logout button to the settings page") and asks for classification and confidence. Verifies it's classified as enhancement or bug_fix with a confidence score. |
| I4 | `test_start_recommendation_fast_path` | Asks the agent to start a recommendation with a bug-fix problem statement. Verifies the workflow type returned is "fast" (fast_path). |

#### Group J — Recommendation Workflow (6 tests)

Tests the full recommendation lifecycle: start (load candidates) -> finalize (accept/reject) -> audit (check completion).

| # | Test | What it does |
|---|------|-------------|
| J1 | `test_start_recommendation_full_project` | Starts a recommendation for ai-agents with a complex problem statement. Verifies the workflow is "full", candidate activities are loaded (23 for ai-agents), and AI-specific activity IDs appear in the output. |
| J2 | `test_start_recommendation_combined_types` | Starts a recommendation with two project types (ai-agents + llm-ops). Verifies the output includes both R-AI and R-MLOPS activities, confirming union-deduplication works. |
| J3 | `test_start_recommendation_with_prior_activities` | Starts a recommendation for iteration 2 with prior activities from iteration 1 (R-SHARED-prior-art, R-AI-frameworks, A-AI-architecture). Verifies the output references iteration 2 and acknowledges the prior activities. |
| J4 | `test_finalize_creates_plan_and_feedback` | Runs finalize with 3 recommended and 2 accepted activities, each with depth assignments. Verifies the response reports an acceptance rate (~67%) and confirms an activity plan was created. |
| J5 | `test_audit_completion_reports_status` | Runs audit on a pre-built activity plan containing 1 completed, 1 in-progress, and 1 pending activity. Verifies the response reports the counts and indicates the plan is not ready for the next phase. |
| J6 | `test_feedback_metrics_via_agent` | Asks the agent to retrieve feedback metrics. Verifies the response reports metrics or acknowledges zero sessions exist (for a fresh environment). |

#### Group K — /rapids-activities Skill (2 tests)

Tests that the `/rapids-activities` skill triggers correctly when the agent is given natural-language requests about activities.

| # | Test | What it does |
|---|------|-------------|
| K1 | `test_skill_lists_activities_on_request` | Asks the agent "Show me all the activities available for ai-agents projects" (natural language, no CLI command). Verifies the response includes specific activity IDs like R-AI-frameworks, R-SHARED-prior-art, or A-AI-architecture. The agent has access to Bash, Read, Glob, Grep, and Skill tools. |
| K2 | `test_skill_shows_activity_details` | Asks the agent to show details of activity R-AI-frameworks including tasks and depth levels. Verifies the response mentions the framework/landscape/evaluation context, depth levels (light/standard/deep), and task information. |

---

### `test_agent_sdk_hardening.py` — Hardening Features (Groups L, L2, M, N, N2, O, P, P2)

Tests for the WS1-WS7 hardening workstreams: 3-layer context management, domain intelligence, enhanced summaries, knowledge graph, demo system, activity coverage, hook integration, and intake resilience.

#### Group L — Context Management, WS1 (4 tests)

Tests the 3-layer context system: context dashboard, codebase intelligence staleness detection, session intelligence re-injection, and knowledge graph summary injection.

| # | Test | What it does |
|---|------|-------------|
| L1 | `test_context_dashboard_shows_all_layers` | Agent runs `context_dashboard` with all 3 layers populated (intake Q&A, codebase intel, session history). Verifies output references Layer 1 (knowledge/documents), Layer 2 (codebase intelligence), and Layer 3 (session progress). |
| L2 | `test_staleness_check_reports_status` | Agent runs `check_intel_update staleness-check` on a project with codebase intelligence. Verifies the JSON result includes stale/fresh status and pending change count. |
| L3 | `test_session_intelligence_visible_in_context` | Agent runs `load_impl_context` on a project with session history containing decisions ("JWT middleware") and patterns ("Docker for Postgres"). Verifies these appear in the context output. |
| L4 | `test_load_impl_context_includes_knowledge_graph_summary` | Agent runs `load_impl_context` on a project with a CODEBASE_GRAPH_REPORT.md. Verifies the output includes knowledge graph data (hub modules, communities, betweenness). |

#### Group L2 — Knowledge Graph, WS1.4 (2 tests)

Tests knowledge graph generation from codebase profiles and community detection.

| # | Test | What it does |
|---|------|-------------|
| L2-1 | `test_knowledge_graph_build_from_profile` | Agent builds a NetworkX graph from a minimal codebase-profile.json (3 files, 2 edges) and generates a report. Verifies node and edge counts in the output. |
| L2-2 | `test_knowledge_graph_report_contains_communities` | Agent builds a graph from a 10-file profile with 2 clusters connected by a bridge edge. Runs community detection and verifies communities are identified in the report. |

#### Group M — Domain Intelligence, WS3 (2 tests)

Tests domain detection from intake answers and context injection into implementation sessions.

| # | Test | What it does |
|---|------|-------------|
| M1 | `test_domain_context_detected_in_session` | Agent runs `load_impl_context` on a project with intake answers mentioning "agent orchestration" and "tool use". Verifies domain intelligence (agentic-ai patterns) appears in the context output. |
| M2 | `test_domain_loader_cli_output` | Agent runs `build_domain_context()` directly via Python snippet. Verifies the function detects the agentic-ai domain from intake keywords and produces non-empty context. |

#### Group N — Enhanced Summaries, WS4 (2 tests)

Tests wave and phase summary generation with Q&A sections from intake.

| # | Test | What it does |
|---|------|-------------|
| N1 | `test_wave_summary_includes_qa_section` | Agent generates wave 0 summary for a project with intake Q&A (architecture, data store questions). Reads the saved `.rapids/implement/wave-summaries/wave-0-summary.md` and verifies it contains a "Questions & Decisions" section. |
| N2 | `test_phase_summary_generates_artifacts` | Agent generates implement phase summary. Verifies `.rapids/audit/phase-implement-summary.md` is created and contains phase-relevant content. |

#### Group N2 — Intake Resilience, WS6 (2 tests)

Tests intake Q&A carryover with many entries and graceful handling of malformed JSON.

| # | Test | What it does |
|---|------|-------------|
| N2-1 | `test_intake_summary_with_many_qa_entries` | Agent runs `load_impl_context` on a project with 30 Q&A entries across 3 phases (research, analysis, plan). Verifies Q&A content is visible and not truncated. |
| N2-2 | `test_malformed_intake_handled_gracefully` | Agent runs `load_impl_context` on a project with `{invalid json content!!!` in intake.json. Verifies the script produces output without crashing (no traceback). |

#### Group O — Demo System, WS7 (3 tests)

Tests the demo worktree system: phase tag listing, worktree creation, and resume-from-phase state validation.

| # | Test | What it does |
|---|------|-------------|
| O1 | `test_demo_list_phases_shows_tags` | Agent runs `phase_checkout list-phases` on a project with annotated tags for research, analysis, and plan phases. Verifies all three phases appear in the output. |
| O2 | `test_demo_setup_creates_worktrees` | Agent runs `phase_checkout setup --phases research,analysis,plan` to create git worktrees at phase tags. Verifies the output mentions worktrees or phase names. Cleans up worktrees after. |
| O3 | `test_resume_from_phase_validates_state` | Agent runs `resume_from_phase` on the project path. Verifies the output includes phase, manifest, and progress state information. |

#### Group P — Activity Coverage, WS2 (2 tests)

Tests activity coverage gap analysis per project type archetype.

| # | Test | What it does |
|---|------|-------------|
| P1 | `test_coverage_report_for_ai_agents` | Agent runs `coverage_report --project-type ai-agents`. Verifies the output lists phases (research, analysis, plan, implement, deploy) with activity counts and identifies gaps. |
| P2 | `test_coverage_report_for_ai_platforms` | Agent runs `coverage_report --project-type ai-platforms`. Verifies the output shows platform-specific phase coverage. |

#### Group P2 — Hook Integration (2 tests)

Tests PostToolUse hooks and subagent learnings capture in live agent sessions.

| # | Test | What it does |
|---|------|-------------|
| P2-1 | `test_check_intel_update_increments_counter` | Agent pipes PostToolUse hook JSON (with a significant file path) to `check_intel_update`. Verifies `pending-changes.json` counter increments to >= 1, confirming the staleness tracking works. |
| P2-2 | `test_subagent_learnings_capture_format` | Agent calls `get_accumulated_learnings()` via Python snippet. Verifies the return is a dict with keys: patterns, decisions, conventions. |

---

### `test_smart_intake_e2e.py` — Smart Intake (Groups A-E, namespaced as SmartIntake)

#### SmartIntake Group A — /rapids-start Skill (5 tests)

Tests the full /rapids-start intake flow including problem statement capture, complexity classification, and Q&A rounds.

#### SmartIntake Group B — /rapids-fix Skill (4 tests)

Tests the /rapids-fix bug-fix intake flow including trivial classification and fast-path routing.

#### SmartIntake Group C — Research Context-Aware (4 tests)

Tests that research phase activities receive and use context from the start/intake phase.

#### SmartIntake Group D — Analysis Context-Aware (5 tests)

Tests that analysis phase activities receive context from both start and research phases.

#### SmartIntake Group E — Cross-Phase Context (3 tests)

Tests end-to-end context preservation across all phases (start -> research -> analysis).

---

### `test_comprehensive_agent_e2e.py` — Comprehensive Scenarios (Groups Q-Z)

#### Group Q — Scenario Detection Engine (5 tests)

Tests the Scenario Detection Engine through agent sessions. Verifies the agent can assess project state, classify problems, and build phase trajectories.

| # | Test | What it does |
|---|------|-------------|
| Q1 | `test_assess_greenfield_project` | Agent runs `scenario assess` on an empty directory and confirms it's classified as greenfield. |
| Q2 | `test_assess_brownfield_project` | Agent runs `scenario assess` on a directory containing Flask source files, requirements.txt, and Dockerfile. Verifies it's recognized as brownfield/existing codebase. |
| Q3 | `test_detect_full_scenario_pipeline` | Agent runs `scenario detect` with a complex problem statement on a brownfield project. Verifies a scenario ID (S1-S9) is returned and phases are mentioned. |
| Q4 | `test_trajectory_shows_phase_depths` | Agent runs `scenario trajectory --scenario S1` and reports phase depth levels. Verifies research, implement, and depth indicators appear. |
| Q5 | `test_bug_fix_trajectory_is_minimal` | Agent runs `scenario trajectory --scenario S7` and confirms minimal phases (plan+implement) with heavy phases skipped. |

#### Group R — Iteration Lifecycle (5 tests)

Tests the Iteration Lifecycle Manager through agent sessions. Verifies start, complete, recap, and synthesis operations.

| # | Test | What it does |
|---|------|-------------|
| R1 | `test_start_iteration_via_agent` | Agent starts iteration 1 with scenario S4 and scope "Add user dashboard with analytics". Verifies iteration number 1 and in_progress status. |
| R2 | `test_complete_iteration_via_agent` | Agent completes an in-progress iteration with 3 features (F001-F003) and 2 ADRs. Pre-starts the iteration via CLI, then has the agent complete it. Verifies 3 features and completion date. |
| R3 | `test_recap_for_returning_user` | Agent generates re-entry recap on a project with one completed iteration (3 features, 2 ADRs, 2 artifacts). Verifies recap references iteration 1, feature count, and key decisions. |
| R4 | `test_synthesize_prior_intelligence` | Agent synthesizes intelligence from prior iteration artifacts (framework-eval.md, architecture.md). Verifies research and analysis artifacts are found and reported. |
| R5 | `test_iteration_history_after_multiple` | Agent reports history after: start iter 1 -> complete iter 1 -> start iter 2. Verifies 2 iterations with completed and in_progress statuses. |

#### Group S — Codebase Profiler (3 tests)

Tests the codebase profiler script through agent sessions. Verifies file inventory, language detection, and architecture analysis.

| # | Test | What it does |
|---|------|-------------|
| S1 | `test_profiler_reports_file_inventory` | Agent runs the profiler on a brownfield directory (Flask app with 5 files) and reads the JSON output. Verifies Python files detected and Flask framework identified. |
| S2 | `test_profiler_on_rapids_reports_architecture` | Agent profiles the RAPIDS scripts/ directory itself. Uses python3 to extract summary stats (file count, symbol count, languages). Verifies Python is primary language and symbols are reported. |
| S3 | `test_profiler_detects_roles_and_coupling` | Agent runs profiler and reads role classifications (entry_points, config_files, data_model_files). Verifies main.py, requirements.txt, and Dockerfile are properly classified. |

#### Group T — Cross-Phase Navigation (4 tests)

Tests cross-phase navigation and gate validation. Verifies phase plans, depth levels, and questionnaire systems.

| # | Test | What it does |
|---|------|-------------|
| T1 | `test_phase_plan_for_trivial_tier` | Agent gets phase plan for trivial complexity tier. Verifies research/analysis are skipped while plan/implement are required. |
| T2 | `test_phase_plan_for_complex_tier` | Agent gets phase plan for complex tier. Verifies all phases required with deep/standard depth levels. |
| T3 | `test_scenario_trajectory_matches_phase_plan` | Agent runs both `scenario trajectory --scenario S1` and `classifier phase-plan --tier complex`, then compares them for consistency. Verifies both require research and analysis. |
| T4 | `test_questionnaire_initial_returns_questions` | Agent retrieves initial intake questions and reports the number and topics. |

#### Group U — Multi-Project-Type & Registry (5 tests)

Tests multi-project-type handling, union/deduplication, phase filtering, and registry consistency.

| # | Test | What it does |
|---|------|-------------|
| U1 | `test_three_project_types_union` | Agent lists activities for ai-agents (23), data-pipelines (23), and all three combined (39). Verifies the combined set is larger and shows correct deduplication. |
| U2 | `test_registry_activity_count_matches_yaml_files` | Agent runs `loader validate` and counts YAML files on disk with `find`. Verifies the registry count matches the actual file count. |
| U3 | `test_phase_filtering_research_only` | Agent lists research-only activities for ai-agents. Verifies only R- prefixed IDs appear and no A- (analysis) activities leak through. |
| U4 | `test_phase_filtering_analysis_only` | Agent lists analysis-only activities for data-pipelines. Verifies A- prefixed activities and data-specific ones appear. |
| U5 | `test_all_project_types_listed_in_registry` | Agent reads _registry.yaml and confirms all 8 project types (ai-infra-platforms, ai-infra-platforms, data-pipelines, data-modernization, llm-ops, ml-ops, cloud-modernization, security-updates) are defined. |

#### Group V — Error Handling & Edge Cases (4 tests)

Tests graceful handling of invalid inputs, missing data, and boundary conditions.

| # | Test | What it does |
|---|------|-------------|
| V1 | `test_unknown_project_type_returns_shared_only` | Agent lists activities for "blockchain" (nonexistent type). Verifies only shared activities (15) are returned. |
| V2 | `test_duplicate_iteration_start_rejected` | Agent tries to start a second iteration while one is already in_progress. Verifies error message about existing iteration. |
| V3 | `test_complete_without_in_progress_rejected` | Agent tries to complete an iteration when none is in_progress. Verifies appropriate error message. |
| V4 | `test_empty_problem_statement_classified` | Agent runs fast-path detection with an empty problem statement. Verifies it returns a result (either classification or error) without crashing. |

#### Group W — Deploy & Sustain Phase Activities (4 tests)

Tests the newer deploy and sustain phase activities are properly registered and filterable.

| # | Test | What it does |
|---|------|-------------|
| W1 | `test_deploy_activities_for_ai_agents` | Agent lists activities for ai-agents and checks that D-AI-model-serving is included while D-DATA-pipeline-deploy is excluded (wrong project type). |
| W2 | `test_sustain_activities_for_llm_ops` | Agent lists sustain activities for llm-ops and verifies S-MLOPS-model-drift is included. |
| W3 | `test_brownfield_activities_in_dag` | Agent builds the activity DAG for ai-agents and verifies brownfield (B-) activities appear with their wave assignments. |
| W4 | `test_deploy_sustain_dependency_chain` | Agent reads the cicd-pipeline.yaml activity and verifies it depends on D-SHARED-infra-design, confirming the dependency chain. |

#### Group X — Intake & Classification Workflow (4 tests)

Tests the intake initialization, complexity classification, and phase plan generation.

| # | Test | What it does |
|---|------|-------------|
| X1 | `test_classify_trivial_bug_fix` | Agent classifies an intake with problem "Fix null pointer in login handler" and project_type "bug_fix". Verifies trivial tier assignment. |
| X2 | `test_phase_plan_moderate_with_project_type` | Agent gets phase plan for moderate tier with ai-agents type. Verifies research and analysis are required with appropriate depth. |
| X3 | `test_is_trivial_check` | Agent runs `is-trivial` quick check on a bug-fix intake ("Fix typo in error message"). Verifies it returns trivial/true. |
| X4 | `test_followup_questions_for_topic` | Agent retrieves followup questions for "architecture" topic. Verifies architecture-related questions are returned. |

#### Group Y — Activity Depth & Dependency Analysis (4 tests)

Tests activity depth level inspection, dependency chain tracing, and DAG validity.

| # | Test | What it does |
|---|------|-------------|
| Y1 | `test_activity_depth_levels_comparison` | Agent reads framework-landscape.yaml and compares light vs deep task counts. Verifies deep has more tasks than light. |
| Y2 | `test_activity_dependency_chain` | Agent traces the full dependency chain for A-AI-tool-integration back through A-AI-architecture, A-SHARED-integration-design, and R-AI-frameworks. |
| Y3 | `test_activity_task_actions_variety` | Agent reads prior-art-analysis.yaml and reports the variety of task action types (web_search, analyze, write_artifact, etc.) used. |
| Y4 | `test_dag_topological_order_valid` | Agent builds the DAG for ai-agents and verifies no activity appears in a wave before its dependencies. Confirms valid topological ordering. |

#### Group Z — Full Workflow Integration (3 tests)

Tests multi-step end-to-end workflows that chain multiple RAPIDS commands.

| # | Test | What it does |
|---|------|-------------|
| Z1 | `test_greenfield_intake_to_activities` | Agent runs a 3-step sequence: scenario detect (on empty dir) -> trajectory for detected scenario -> recommend start for ai-agents. Verifies scenario ID, trajectory phases, and candidate activity count. |
| Z2 | `test_brownfield_assess_to_iteration` | Agent runs a 3-step sequence: scenario assess (brownfield dir) -> iteration start (S3, "Modernize legacy Flask app") -> iteration history. Verifies project state assessment, iteration 1 creation, and S3 scenario. |
| Z3 | `test_iteration_complete_to_recap` | Agent runs a 3-step sequence: iteration complete (3 features, 2 decisions) -> iteration recap -> iteration synthesize. Verifies feature count, recap summary, and artifact discovery during synthesis. |

---

## Script-Level Test Files (no Agent SDK)

These files test RAPIDS Python scripts directly without spawning Claude sessions. They run fast and don't consume API tokens.

| File | Tests | Coverage Area |
|------|-------|---------------|
| `test_hooks.py` | 61 | Hook enforcement via `run_hook()` helper — no-mocks guard, validate-rapids-path, artifact templates, feature-list protection |
| `test_greenfield_lifecycle.py` | 48 | Full greenfield lifecycle: scaffold, plan, DAG, waves, orchestrator, feature tests, regression, merge, promote |
| `test_activity_system_e2e.py` | 44 | Activity loader CLI, recommendation CLI, iteration CLI, fast-path CLI |
| `test_archetype_flows_e2e.py` | 13 | Archetype classification for ai-agents/ai-infra-platforms, activity loading, DAG validation per archetype |
| `test_phase_guards_e2e.py` | 13 | Phase gate validation (research/analysis/plan gates), block vs pass scenarios |
| `test_brownfield_lifecycle.py` | 7 | Brownfield scaffold with pre-existing git, manifest, profiler T0 inventory |
| `test_context_layers_e2e.py` | 5 | 3-layer context integration: greenfield, brownfield, staleness detection |
