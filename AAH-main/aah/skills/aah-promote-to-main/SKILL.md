---
name: aah-promote-to-main
description: Gated merge from develop to main — validates all criteria before allowing promotion
user-invocable: true
disable-model-invocation: true
---

# AAH Promote to Main

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.** Do NOT present questions as plain text. Call `AskUserQuestion` with properly structured `questions`.

Merge `develop` to `main` with explicit quality gates. NEVER automatic.

## Steps

### 0. Resolve Active Project

First, resolve the active project path. ALL file operations in this skill use this path:
```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
```
Use `$AAH_DIR` instead of `.aah/` and `$PROJECT_DIR` instead of `.` for all paths.


### 1. Validate Merge Readiness
```bash
aah run core.git_ops.validate_main_merge
```

This checks:
- All planned features have `passes: true`
- Cumulative regression suite passes on develop
- No features are in-progress
- No known issues remain

### 2. Present Merge Readiness Report
Show the user the full validation results:
- Feature completion status
- Regression results
- Any blocking issues

### 3. Get Explicit User Confirmation
Use `AskUserQuestion` to get explicit confirmation:
- "All checks passed. Merge develop to main?" with choices: ["Yes, merge to main", "No, abort — I need to review"]
Do NOT proceed without the user explicitly selecting "Yes".

### 4. Execute Merge
```bash
aah run core.common.git_utils checkout main --project-path "$PROJECT_DIR"
git merge --ff-only develop
```

If fast-forward fails, use `AskUserQuestion`:
- "Fast-forward merge failed (develop has diverged from main). How to proceed?" with choices: ["Create a merge commit", "Abort and investigate", "Rebase develop onto main"]

### 5. Update State
```bash
aah run core.common.manifest update-phase complete
aah run core.common.progress update --phase complete --next-steps "Project delivered to main"
```

### 5a. Checkpoint: Commit Final State on Main

```bash
cd "$PROJECT_DIR"
git add .aah/manifest.yaml .aah/claude-progress.json .aah/audit/
git commit -m "chore(aah): mark project complete on main"
```

**Tag the phase:**

```bash
MANIFEST_JSON=$(aah run core.common.manifest read)
PROJECT_NAME=$(echo "$MANIFEST_JSON" | uvx --from aah python -c "import sys,json; print(json.load(sys.stdin)['project_name'])")
ITER=$(echo "$MANIFEST_JSON" | uvx --from aah python -c "import sys,json; print(json.load(sys.stdin).get('current_iteration', 1))")
git -C "$PROJECT_DIR" tag -a "${PROJECT_NAME}/iter-${ITER}/aah-complete" -m "AAH project complete — promoted to main (iteration ${ITER})"
```

### 6. Final Summary
Show the user the completed state and any post-delivery recommendations.
