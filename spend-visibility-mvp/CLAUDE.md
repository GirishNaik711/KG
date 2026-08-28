<!-- AAH:BEGIN -->
# AAH Delivery Project — spend-visibility-mvp

This project (spend-visibility-mvp, stack: python) uses the AAH (Ascend Agentic
Harness) delivery framework for standardized AI-assisted software delivery.

## Core Rules

### State Management
- ALWAYS read `manifest.yaml` and `claude-progress.json` before starting any work
- ALWAYS read `decision-registry.yaml` for decision state (replaces phase-plan.yaml)
- ALWAYS update `claude-progress.json` at the end of every feature or session

### Testing — NO MOCKS
- NEVER use mock frameworks (jest.mock, unittest.mock, sinon, pytest-mock,
  nock, testdouble, vitest mock, proxyquire) in test code
- ALL tests must be functional — executing against the real running system
- ALL tests must be executable locally
- Do not generate mocks, stubs, or test doubles unless the feature spec
  explicitly defines a local test double

### Environment Configuration — NON-NEGOTIABLE
- `.env.example` is the SINGLE SOURCE OF TRUTH for what configuration this
  application needs. It is COMMITTED and holds variable NAMES with safe
  placeholders — NEVER a real secret, key, password, or endpoint
- `.env` holds the real values, is gitignored, and is the ONLY place values live.
  Never read a value from `.env.example`; never write a value into it
- The startup env checker (`config/env_check.py` or `config/env-check.cjs`,
  depending on stack) validates every required variable at boot and fails with
  `ERR_CDR_78_EX_CONFIG`, naming every missing variable at once and pointing the
  user at `cp .env.example .env`. Call it from the application entrypoint BEFORE
  any module reads configuration
- EVERY module that reads a new environment variable does THREE things in the
  same change: (1) lists the name in its feature file's
  `## Required Env Variables` section, (2) adds it to `.env.example` with a safe
  placeholder, (3) registers it in the checker's required list. Doing one or two
  of the three is what produces a runtime failure nobody can diagnose
- If the project's stack ships no checker (Go, Rust, Java, …), the scaffold-first
  feature implements the SAME contract in the project's own language: same error
  code, same message shape, same `.env.example` guidance
- NEVER work around a missing variable by hardcoding a value, inlining a default
  for a secret, or catching the config error — fix the environment

### Feature List Protection
- It is UNACCEPTABLE to remove or edit features in feature-list.json
- ONLY status changes (passes: true/false) are allowed
- Structural changes will be blocked by hooks

### Artifacts
- ALWAYS write artifacts to the correct `.aah/` subdirectory
- ALWAYS follow artifact templates when generating documents

### Git Discipline
- ALWAYS commit progress to git with descriptive messages after meaningful changes
- NEVER leave uncommitted work at the end of a feature or session
- Leave the environment in a clean, working state

### Orchestrator Loop — MANDATORY During Implement Phase
- ALWAYS use the orchestrator to determine what to do next:
  ```
  aah run core.build.orchestrator next-action
  ```
- NEVER run build-phase scripts directly without orchestrator guidance
- The orchestrator is the SINGLE SOURCE OF TRUTH for execution flow
- After every action completes, call `next-action` again to get the next step
- The orchestrator loop is:
  1. Call `orchestrator next-action` → receive ONE action
  2. Execute exactly that action (the command/script it tells you to run)
  3. Call `orchestrator next-action` again
  4. Repeat until `action: "complete"`
- NEVER skip steps, reorder steps, or improvise your own flow
- NEVER run quality_checks, validate_checkpoint, runtime_validation,
  run_regression_suite, or merge scripts unless the orchestrator told you to
- If the orchestrator returns an error, fix the underlying issue and re-call it
- NEVER judge a gate as "unnecessary" — every wave goes through every gate
  regardless of complexity. "Scaffold only" or "trivial" is NOT a reason to skip
- NEVER advance `current_wave` yourself — only the orchestrator's `merge` action
  advances waves after ALL gates pass
- NEVER write expertise/checkpoint markers without completing the full procedure
  — the orchestrator validates artifacts and will re-fire skipped gates
- When the orchestrator action includes a `skill` field, you MUST invoke
  that skill via the Skill tool — NEVER execute the steps manually

### Feedback Routing — NON-NEGOTIABLE
- ANY user message describing a bug, broken behavior, or change request
  MUST be routed through `Skill("aah-fix")` — NEVER act on it directly
- Bypass ONLY when the user explicitly says "do not update files" or
  "just tell me"

### Build Phase Constraints
- NEVER run raw test commands (`pytest`, `npm test`, `jest`) directly —
  use `aah run core.build.run_feature_tests` or `aah run core.build.run_regression_suite`
- ALL failures route through `Skill("aah-fix")` which dispatches the appropriate agent
- If a framework tool fails: fix only the git precondition (commit/checkout),
  retry. Everything else goes through `Skill("aah-fix")`

### Session Compaction Recovery
- If the session context is compacted, immediately reinvoke the skill that was
  active at the time of compaction using the Skill tool. This ensures full skill
  instructions are reloaded and no steps are missed.

### Orchestrator CLI Display — NON-NEGOTIABLE
- Bash tool output is COLLAPSED in the CLI (user must press Ctrl+O to see it)
- You MUST parse orchestrator JSON responses and re-render them as DIRECT
  markdown text in your response — NEVER rely on the Bash output being visible
- This applies to ALL orchestrator commands: `next-action`, `qa-report`,
  `wave-summary`, and any script that outputs checkpoint/gate banners
- After EVERY Bash call to an orchestrator or checkpoint script, immediately
  output a markdown heading + table with the key fields from the JSON response
- Example — after `next-action` returns `{"action": "run_qa", "wave": 3, "features": ["F005"]}`:

  ### ═══ QA GATE — Wave 3 ═══
  | Field    | Value                             |
  |----------|-----------------------------------|
  | Features | F005                              |
  | Action   | Run QA evaluator for each feature |

- This rule has the SAME priority as "NO MOCKS" — violating it means the user
  cannot see what is happening without manual intervention

### Work Increments
- Work on ONE feature at a time — complete it fully before starting the next
- Follow the DAG execution order defined in waves.json
- Get user confirmation between waves

### Branching Strategy
- `main` — production-ready code only, merge requires explicit criteria validation
- `develop` — integration branch, receives promoted code from integration branches
- `integration/wave-N` — temporary branches for cumulative testing after wave merges
- worktrees — isolated feature development branches off develop

## Directory Structure

- `.aah/discuss/` — Discuss phase artifacts
- `.aah/architecture/` — Architecture phase artifacts
- `.aah/plan/specs/` — Technical specifications
- `.aah/plan/features/` — Feature YAML definitions
- `.aah/plan/sprint-contracts/` — Sprint contracts per wave
- `.aah/build/` — Implementation state, test results
- `.aah/build/test-results/` — Per-feature and regression test results
- `.aah/deploy/` — Deployment configs and IaC
- `.aah/deploy/infra/` — Infrastructure provisioning templates
- `.aah/codebase-intel/` — Codebase intelligence artifacts (unified for greenfield and brownfield)
- `.aah/audit/` — Phase logs, traceability matrix
<!-- AAH:END -->
