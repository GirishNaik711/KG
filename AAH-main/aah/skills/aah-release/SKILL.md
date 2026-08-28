---
name: aah-release
description: Certify an AAH framework release — runs security scans on the framework itself, generates certification report, and stores audit artifacts in releases/
user-invocable: true
disable-model-invocation: true
---

# AAH Release Certification

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.** Do NOT present questions as plain text.

## Steps

### 1. Determine Version

If the user provided a version as an argument (e.g., `/aah-release 1.0.0`), use it directly.

If no version was provided, use `AskUserQuestion`:
- "What version should this release be?"
  - Provide a text input (semver format: `1.0.0`, `0.2.0-rc.1`)

Check for existing releases:
```bash
aah run core.security.release_certify list
```

If the version already exists, inform the user and ask for a different version.

### 2. Confirm Release Scope

Use `AskUserQuestion`:
- "Ready to certify AAH Framework v{version}? This will:"
  - "1. Run a full security scan on the framework code (SAST, secrets, deps, licenses)"
  - "2. Generate certification report and detailed scan reports"
  - "3. Store all reports in releases/{version}/"
  - "4. Commit and tag as v{version}"
  - Choices: ["Certify now", "Certify with --allow-warnings (accept medium/low)", "Cancel"]

### 3. Run Certification

Based on user's choice:

**Standard certification (blocks on any critical/high findings):**
```bash
aah run core.security.release_certify certify --version {version}
```

**Allow warnings (blocks only on critical/high, accepts medium/low):**
```bash
aah run core.security.release_certify certify --version {version} --allow-warnings
```

### 4. Display Results

If certification **PASSED**:
- Show the certification summary (version, date, commit, finding counts)
- Show the "Fix These First" priorities if any medium/low warnings exist
- Confirm the reports location: `releases/{version}/`
- Confirm the git tag: `v{version}`

If certification **FAILED**:
- Show the blocking findings (critical/high)
- Use `AskUserQuestion`: "Certification failed with {N} blocking findings. What would you like to do?"
  - Choices: ["Review blocking findings", "Fix findings now", "Retry with --allow-warnings", "Cancel release"]

### 5. Show Generated Reports

List the reports generated:
```bash
ls releases/{version}/
```

Offer to display any report:
- Use `AskUserQuestion`: "Certification complete. Which report would you like to review?"
  - Choices: ["Certification summary", "SAST report", "Dependency report", "License report", "Done"]

### 6. Push Reminder

After successful certification, remind the user:
- "Release v{version} is committed and tagged locally. To publish:"
  - `git push origin feature/ctu-updates` (or current branch)
  - `git push origin v{version}` (push the tag)
