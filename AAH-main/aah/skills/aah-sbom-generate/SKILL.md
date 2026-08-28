---
name: aah-sbom-generate
description: Generate SBOM (Software Bill of Materials) and scan for supply chain vulnerabilities
user-invocable: true
disable-model-invocation: true
---

# AAH SBOM Generator

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

SBOM generation requires:
- **Syft** — for generating CycloneDX/SPDX SBOMs
- **Grype** — for scanning SBOM against vulnerability databases
- **cosign** (optional) — for signing artifacts

If Syft is not available, the skill cannot proceed. If Grype is missing, SBOM generation will succeed but vulnerability scanning will be skipped.

### 2. Parse Arguments

- `/aah-sbom-generate` — generate CycloneDX SBOM + Grype scan
- `/aah-sbom-generate --format spdx` — generate SPDX format
- `/aah-sbom-generate --format both` — generate both formats
- `/aah-sbom-generate --sign` — also sign with cosign

### 3. Generate SBOM and Scan

```bash
aah run core.security.run_sbom_scan run \
  --format {format} \
  --project-path "$PROJECT_DIR"
```

If `--sign` was specified:
```bash
aah run core.security.run_sbom_scan run \
  --format {format} \
  --sign \
  --project-path "$PROJECT_DIR"
```

### 4. Display Results

```bash
cat "$AAH_DIR/security/reports/sbom-report.md"
```

Present to the user:
- Component count (direct + transitive dependencies)
- Vulnerability findings from Grype (by severity)
- License distribution highlights (flag copyleft licenses)
- Signing status

### 5. Offer Next Steps

Use `AskUserQuestion` based on results:

**If vulnerabilities found:**
- "SBOM scan found {N} vulnerabilities in your dependencies."
  - Choices: ["Review vulnerable packages", "Update dependencies to fix", "View full SBOM", "Continue"]

**If clean:**
- "SBOM generated successfully with no known vulnerabilities."
  - Choices: ["View component inventory", "Upload to Dependency-Track", "Continue to next phase"]
