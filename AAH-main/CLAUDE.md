# AAH (Ascend Agentic Harness)

This repository is the AAH delivery framework — the tooling that scaffolds and
drives AI-assisted software delivery projects. These rules govern how the
harness itself operates, independent of any specific delivery project. The
project-level delivery ruleset is stamped into each scaffolded project's own
`CLAUDE.md` (see `aah/core/scaffold/claude_md_template.py`).

## Core Rules

### Skill Invocation — MANDATORY
- Any general "what is AAH / what is this / how does this work / how do I use
  this / where do I start" question — in ANY phrasing — MUST invoke the `aah`
  skill via the Skill tool.
- NEVER answer these from memory, even if you know the answer — the skill
  renders the official banner. Answer directly ONLY for a specific technical
  question about one command, file, or behavior.

### State Management
- ALWAYS invoke Python modules from `aah.core` (via `aah run core.<module>`) for state management

### Project Protection — CRITICAL
- The folder is the project: the current folder holds `.aah/` and IS the
  AAH project. There is no workspace, no `workspace_root`, no
  active-project pointer.
- NEVER delete, remove, or `rm -rf` any directory in the project tree
- NEVER run `shutil.rmtree`, `rm -rf`, `os.remove`, or any destructive
  file operation on the project directory or any path outside the current
  git repo's `.claude/worktrees/` directory
- Before any destructive file operation, verify the target path is within
  a known safe scope (e.g., `.claude/worktrees/`, temp directories)
- If a cleanup script targets a path outside `.claude/worktrees/`, REFUSE
  and report the issue to the user

### Reasoning Philosophy
- ALWAYS read and follow the reasoning directives in `.claude/skills/aah-expertise/references/mental-model.md`
- This governs how Claude approaches problems, manages expertise, and handles contradictions

## Python Modules

All deterministic logic lives in `aah/core/`. Run via the `aah` CLI:
```
aah run core.<module> <subcommand> [args]
```
`aah run` ensures the correct Python environment (uv-managed) is used;
`core.<module>` is shorthand for the fully-qualified `aah.core.<module>`.

Key modules:
- `common.manifest` — Read/write manifest.yaml
- `common.progress` — Read/write claude-progress.json
- `common.feature_list` — Read/write feature-list.json (status only)
- `common.dag` — DAG construction, validation, topological sort
- `registry.registry` — Decision registry CRUD (init, update-context, update-decision, next-unresolved, apply-eliminations, apply-skip-conditions, status)
- `registry.ddr_loader` — DDR v2.0 playbook loader (list-ddrs, load-ddr, validate-ddr, resolution-order)
- `registry.regimes` — Compliance regime detection and option elimination (detect-regimes, get-eliminations)
- `scaffold.project` — Scaffold `.aah/` in the current folder (greenfield/brownfield auto-detected)
- `plan.build_feature_list` — Assemble feature-list.json from YAMLs
- `plan.build_dag` — Build and validate DAG
- `plan.compute_waves` — Compute execution waves
- `build.load_impl_context` — Session startup context
- `build.run_feature_tests` — Run per-feature tests
- `build.run_regression_suite` — Run cumulative regression
- `git_ops.merge_wave_to_integration` — Merge wave to integration branch
- `git_ops.promote_to_develop` — Promote integration to develop
- `git_ops.validate_main_merge` — Check main merge readiness
- `intel.build_traceability` — Build traceability matrix
