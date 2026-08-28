# PR Release Summary Skill

A Claude Code skill that generates professional release summary documents from merged pull requests on a given branch within a configurable date range.

## What It Does

- Fetches merged PRs from GitHub using the `gh` CLI
- Groups changes into meaningful **themes** (capability-oriented, not code-structure)
- Produces a polished document with:
  1. **Executive Summary** — high-level overview for stakeholders
  2. **Changes by Theme** — grouped PRs with narrative descriptions
  3. **Complete PR List** — full table of all merged PRs with stats

## Output Formats

- **HTML** — styled, responsive, print-friendly
- **Word (.docx)** — professional formatting with Calibri typography and branded colors
- **Both** (default)

## Parameters

| Parameter    | Default              | Description                          |
|--------------|----------------------|--------------------------------------|
| `repo`       | Current directory    | Repository path or GitHub owner/repo |
| `branch`     | `develop`            | Target branch to scan                |
| `start_date` | 14 days ago          | Start of date range (YYYY-MM-DD)     |
| `end_date`   | Today                | End of date range (YYYY-MM-DD)       |
| `format`     | `both`               | `html`, `docx`, or `both`            |

## Example Prompts

```
Summarize the recent PRs on develop
Generate a release summary for main from 2026-01-01 to 2026-03-01 as HTML
What was merged in the last month? Give me a Word doc
```

## Requirements

- `gh` CLI (authenticated with repo access)
- Python 3.8+
- `python-docx` package (`pip install python-docx`)

## Files

```
pr-release-summary/
├── SKILL.md                    # Skill instructions (loaded by Claude Code)
├── README.md                   # This file
└── scripts/
    └── generate_document.py    # HTML and DOCX document generator
```
