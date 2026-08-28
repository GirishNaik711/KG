---
name: pr-release-summary
description: >
  Generate a professional release summary document from merged pull requests in a Git repository.
  Use this skill whenever the user wants to summarize recent PRs, create a release report, review
  what changed on a branch over a time period, generate a changelog document, or produce a
  stakeholder-ready summary of development activity. Triggers on requests involving: merged PRs,
  release notes, branch activity summary, sprint recap, deployment summary, "what shipped",
  "what merged", changelog generation, or development progress reports. Supports HTML and Word
  (.docx) output formats.
---

# PR Release Summary

You are generating a professional release summary document that captures all merged pull requests
on a given branch within a date range, organized into thematic groups for easy consumption by
stakeholders and engineering teams.

## Workflow

### 1. Gather Parameters

Collect these from the user's request (use defaults where not specified):

| Parameter     | Default                        | Description                          |
|---------------|--------------------------------|--------------------------------------|
| `repo`        | Current working directory      | Repository path or GitHub owner/repo |
| `branch`      | `develop`                      | Target branch to scan                |
| `start_date`  | 14 days before `end_date`      | Start of date range (YYYY-MM-DD)     |
| `end_date`    | Today's date                   | End of date range (YYYY-MM-DD)       |
| `format`      | `both`                         | Output: `html`, `docx`, or `both`    |

### 2. Fetch Merged PRs

Use the `gh` CLI to pull merged PRs. Run this command (adjust dates and branch):

```bash
gh pr list \
  --repo <repo> \
  --base <branch> \
  --state merged \
  --search "merged:${start_date}..${end_date}" \
  --json number,title,author,body,mergedAt,labels,files,additions,deletions,url \
  --limit 200
```

If `gh` is not available or the repo is local-only, fall back to `git log`:

```bash
git log --merges --oneline --after="${start_date}" --before="${end_date}" <branch>
```

Then extract PR numbers from merge commit messages and fetch details individually.

### 3. Analyze and Theme the Changes

This is the most important step. Read through all PR titles, descriptions, and file changes,
then group them into **3-7 themes** based on functional areas. The themes should make sense to
someone who cares about *what the product can do now*, not how the code is structured.

Good themes describe capability areas:
- "Authentication & Security Enhancements"
- "Data Pipeline Performance Improvements"
- "New Reporting Dashboard Features"
- "API Stability & Bug Fixes"
- "Developer Experience & Tooling"

Bad themes mirror code structure (avoid these):
- "Frontend Changes"
- "Backend Changes"
- "Config Updates"

For each theme, write a **2-3 sentence narrative** explaining what changed and why it matters.
Each PR within a theme gets a one-line summary.

### 4. Write the Executive Summary

The executive summary goes at the top of the document. It should be 3-5 sentences covering:
- The time period and branch
- Total number of PRs merged and contributors involved
- The 2-3 most significant themes or changes
- Overall trajectory (new features? stabilization? infrastructure work?)

Write in a professional but accessible tone. Avoid jargon. A VP or product manager should be able
to read this paragraph and understand what happened.

### 5. Generate the Document

Run the generation script from this skill's directory:

```bash
uvx --from aah python "${SKILL_PATH}/scripts/generate_document.py" \
  --data '<JSON string of structured PR data>' \
  --format <html|docx|both> \
  --output-dir <output directory> \
  --title "Release Summary" \
  --branch <branch> \
  --start-date <start_date> \
  --end-date <end_date>
```

However, because the structured data (themed groups, executive summary, PR details) comes from
your analysis, you will need to prepare the JSON payload before calling the script. The JSON
structure expected by the script is:

```json
{
  "executive_summary": "Your executive summary text here.",
  "branch": "develop",
  "start_date": "2025-01-01",
  "end_date": "2025-01-14",
  "total_prs": 15,
  "contributors": ["alice", "bob"],
  "themes": [
    {
      "name": "Theme Name",
      "narrative": "2-3 sentence description of this theme.",
      "prs": [
        {
          "number": 42,
          "title": "PR title",
          "author": "username",
          "summary": "One-line summary of what this PR does.",
          "merged_at": "2025-01-10T14:30:00Z",
          "url": "https://github.com/owner/repo/pull/42",
          "additions": 150,
          "deletions": 30
        }
      ]
    }
  ]
}
```

Write this JSON to a temp file, then pass it via `--data-file` instead of inline:

```bash
uvx --from aah python "${SKILL_PATH}/scripts/generate_document.py" \
  --data-file /tmp/pr_summary_data.json \
  --format both \
  --output-dir .
```

### 6. Present Results

Tell the user where the files were saved and offer to open them. The HTML file can be opened
directly in a browser; the .docx file in Word or any compatible application.

## Important Notes

- If the repository has very few PRs (< 3), you may not need themes — just list them with
  individual summaries under the executive summary.
- If there are many PRs (> 30), focus themes on the most impactful changes and keep the
  PR list in the appendix concise.
- Always include the PR number and link in the PR list so readers can drill into details.
- The script handles all formatting, styling, and file generation. Your job is the analysis
  and content — the script handles presentation.
