---
name: aah-dast-scan
description: Run DAST scans (OWASP ZAP + Nuclei) against a target URL for runtime vulnerability discovery
user-invocable: true
disable-model-invocation: true
---

# AAH DAST Scan

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.** Do NOT present questions as plain text.

## Steps

### 0. Resolve Active Project

```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
```

### 1. Check Tool Availability

```bash
aah run core.security.tool_check
```

DAST requires:
- **Docker** — for running OWASP ZAP (containerized)
- **Nuclei** — for template-based vulnerability scanning

If Docker is not available, ZAP scans will be skipped. If Nuclei is not available, template scans will be skipped. Inform the user which tools are missing.

### 2. Validate Target URL

Check if a target URL was provided as an argument or configured in policy.yaml:

```bash
cat "$AAH_DIR/security/policy.yaml" 2>/dev/null | grep target_url
```

If no URL is configured and none was passed as argument, use `AskUserQuestion` to ask:
- "No target URL configured. What URL should be scanned?"
  - Provide a text input for the URL

### 3. Parse Arguments

- `/aah-dast-scan` — run baseline mode against configured URL
- `/aah-dast-scan baseline --target http://localhost:8080` — baseline scan
- `/aah-dast-scan api --target http://localhost:8080/api/v1` — API scan (OpenAPI)
- `/aah-dast-scan full --target http://staging.example.com` — full active scan (staging only)

### 4. Run DAST Scan

```bash
aah run core.security.run_dast_scan run \
  --mode {mode} \
  --target {url} \
  --project-path "$PROJECT_DIR"
```

### 5. Display Results

```bash
cat "$AAH_DIR/security/reports/dast-report.md"
```

Present results to the user. Highlight:
- Combined finding count (ZAP + Nuclei)
- Critical/High severity findings with URLs
- Which tools ran successfully vs. which were skipped

### 6. Offer Next Steps

Use `AskUserQuestion` based on results:

**If findings found:**
- "DAST scan found {N} findings. What would you like to do?"
  - Choices: ["Review findings in detail", "Fix findings now", "Suppress false positives", "Re-scan after fixes"]

**If no findings:**
- "DAST scan completed with no findings."
  - Choices: ["Run a deeper scan (full mode)", "Continue to next phase", "View raw reports"]
