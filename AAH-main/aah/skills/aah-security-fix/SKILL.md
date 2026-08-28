---
name: aah-security-fix
description: Auto-fix security scan findings using a three-tier pipeline (deterministic fixes, Semgrep autofix, Claude-assisted remediation). Applies all fixes in one shot.
user-invocable: true
disable-model-invocation: true
---

# AAH Security Auto-Fix

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.** Do NOT present questions as plain text.

## Prerequisites

This skill requires scan results from a prior `/aah-security-scan` run. The file `.aah/security/scan-results-latest.json` must exist.

## Philosophy

This skill applies ALL fixable findings automatically without per-finding prompts. The user already reviewed findings via `/aah-security-scan` reports. Invoking this skill means "fix everything you can." Only ask the user a question at the very end if blocking findings remain.

## Steps

### 0. Resolve Active Project

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
```

### 1. Verify Scan Results Exist

```bash
test -f "$AAH_DIR/security/scan-results-latest.json" && echo "OK" || echo "MISSING"
```

If MISSING, tell the user: "No scan results found. Run `/aah-security-scan` first." and stop.

### 2. Classify and Report

```bash
aah run core.security.auto_fix classify --project-path "$PROJECT_DIR"
```

Parse the JSON output and present the classification summary (informational only, do NOT ask a question):

```
## Fix Classification

| Tier | Category | Count | Method |
|------|----------|------:|--------|
| 1 | Dependencies | N | Upgrade vulnerable packages in requirements.txt |
| 1 | Secrets | N | Externalize hardcoded credentials to .env |
| 2 | SAST | N | Semgrep rule-based autofix |
| 3 | Claude-Assisted | N | AI-guided code remediation |
| — | Skipped | N | Suppressed or license-only (no code fix) |
| | **Total** | **N** | |
```

If all fixable counts (tier1 + tier2 + tier3) are 0, inform the user: "No fixable findings. All findings are either suppressed or informational." and stop.

### 3. Run Tiers 1 and 2

Run the automated pipeline immediately — no confirmation needed:

```bash
aah run core.security.auto_fix run-pipeline \
  --tiers 1,2 \
  --project-path "$PROJECT_DIR"
```

Parse the JSON output and report what changed:

**Tier 1 — Dependencies:**
- List each upgraded package: `{old} -> {new} (fixes {CVEs})`
- Or "No fixable dependency vulnerabilities"

**Tier 1 — Secrets:**
- List each externalized variable: `{file}:{line} — {var_name} moved to .env`
- Or "No hardcoded secrets to fix"

**Tier 2 — Semgrep Autofix:**
- List each modified file
- Or "No Semgrep autofix rules matched"

### 4. Run Tier 3 — Claude-Assisted Fixes

If there are `tier3_remaining` findings from the pipeline output OR if there are Semgrep findings that Tier 2 couldn't autofix (Semgrep reports `applied: 0` but findings exist), read the scan results and fix them directly.

Also check: if the classification showed `tier2_semgrep > 0` but `tier2_result.applied == 0`, those Semgrep findings still need fixing — treat them as Tier 3.

**Process — fix all automatically, no per-finding questions:**

1. Collect all Tier 3 findings. Also collect any remaining Semgrep findings that weren't autofixed.

2. Group findings by `rule_id`. Sort groups by severity (critical first), then by occurrence count (most first).

3. For each group:
   a. Read the source file(s) containing the findings
   b. Read the finding's `fix_description` and `fix_example` from the scan results for remediation guidance
   c. Apply the fix to ALL occurrences in the group using Claude Code's `Edit` tool
   d. Follow the `fix_description` and `fix_example` as guidance — adapt to the specific code context
   e. Do NOT blindly paste the example — understand what the code does and apply the fix correctly
   f. Briefly state what was changed (one line per group, e.g. "Fixed 5 SQL injection findings in app/main.py — converted f-string queries to parameterized queries")

4. Track how many findings were fixed by Tier 3.

### 5. Re-run Scan for Verification

After all fixes are applied:

```bash
aah run core.security.run_security_scan run \
  --scan-types all \
  --project-path "$PROJECT_DIR"
```

### 6. Report Results

Read the new scan results and compare with the original classification:

```
## Fix Results

| Metric | Count |
|--------|------:|
| Findings before fix | N |
| Tier 1 fixed (deps) | N |
| Tier 1 fixed (secrets) | N |
| Tier 2 fixed (Semgrep) | N |
| Tier 3 fixed (Claude) | N |
| Findings after fix | N |
| Remaining | N |
```

If the gate now passes: "Security gate now **PASSES**."

If still failing, show remaining blocking findings and use `AskUserQuestion`:
- "Fixes applied but {N} blocking findings remain. What next?"
- Choices: ["Continue fixing remaining", "Suppress remaining with justification", "Done for now"]
