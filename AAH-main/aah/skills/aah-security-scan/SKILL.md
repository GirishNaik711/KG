---
name: aah-security-scan
description: Run security scans (SAST, secrets, dependency, IaC) against the active AAH project and display results
user-invocable: true
disable-model-invocation: true
---

# AAH Security Scan

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.** Do NOT present questions as plain text. Call `AskUserQuestion` with properly structured `questions`.

## Steps

### 0. Resolve Active Project

First, resolve the active project path. ALL file operations in this skill use this path:
```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
```
Use `$AAH_DIR` instead of `.aah/` and `$PROJECT_DIR` instead of `.` for all paths.

### 1. Check Tool Availability

```bash
aah run core.security.tool_check
```

If no tools are available, inform the user and provide installation instructions. Do NOT proceed without at least one tool installed.

### 2. Parse Arguments

The user may provide arguments after the skill name:
- `/aah-security-scan` — run all enabled scan types
- `/aah-security-scan sast` — run only SAST scan
- `/aah-security-scan secrets` — run only secrets scan
- `/aah-security-scan deps` — run only dependency scan
- `/aah-security-scan all` — run all scan types
- `/aah-security-scan all --severity critical` — only block on critical findings

Map the first argument to `--scan-types` and `--severity` to the severity flag.

### 3. Run Security Scan

```bash
aah run core.security.run_security_scan run \
  --scan-types {scan_types} \
  --project-path "$PROJECT_DIR"
```

If `--severity` was specified:
```bash
aah run core.security.run_security_scan run \
  --scan-types {scan_types} \
  --severity {severity} \
  --project-path "$PROJECT_DIR"
```

### 4. Display Results

Read the generated report:
```bash
cat "$AAH_DIR/security/reports/security-scan-report.md"
```

Present the results to the user. Highlight:
- Gate result (PASSED/FAILED)
- Blocking findings (Critical/High) with file locations
- Warning findings count
- Suppressed findings count
- Any tool errors

### 5. Offer Next Steps

Use `AskUserQuestion` to present options based on results:

**If gate PASSED:**
- "Security scan passed. No blocking findings."
  - Choices: ["Continue working", "View full report", "Run scan again with different types"]

**If gate FAILED due to findings:**
- "Security scan found {N} blocking findings. What would you like to do?"
  - Choices: ["Auto-fix findings (/aah-security-fix)", "Review findings in detail", "Suppress a finding (with justification)", "Run scan again after fixes"]

### 5a. Handle Auto-Fix Choice

If the user chose "Auto-fix findings (/aah-security-fix)", invoke the `/aah-security-fix` skill to run the three-tier auto-fix pipeline.

**If gate FAILED due to errors:**
- "Security scan encountered errors. Some tools may have failed."
  - Choices: ["Re-run the scan", "View error details", "Continue without scan"]

### 6. Handle Suppression Requests

If the user chooses to suppress a finding:

1. Read the scan results to get the finding details
2. Use `AskUserQuestion` to ask:
   - Which finding to suppress (by ID)
   - Justification for suppression
   - Their name (for audit trail)

3. Write the suppression to `$AAH_DIR/security/suppressions.yaml` using the format:
```yaml
suppressions:
  - finding_id: "{tool}:{rule_id}"
    file: "{file_path}"
    justification: "{user_provided_justification}"
    suppressed_by: "{user_name}"
    suppressed_at: "{ISO_timestamp}"
    review_date: "{90_days_from_now}"
```

4. Re-run the scan to verify the suppression took effect.
