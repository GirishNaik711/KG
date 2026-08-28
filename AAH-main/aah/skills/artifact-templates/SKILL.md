---
name: artifact-templates
description: Standardized document templates for all AAH artifact types
user-invocable: false
---

# Artifact Templates

## Research Artifact Template

```markdown
# [Title]

## Problem Framing
What question are we trying to answer? What context is relevant?

## Findings
Structured findings with evidence and sources.

## Trade-offs
| Option | Pros | Cons | Risk |
|--------|------|------|------|
| ... | ... | ... | ... |

## Recommendation
Clear recommendation with rationale.
```

## Architecture Decision Record (ADR) Template

```markdown
# ADR-NNN: [Title]

## Context
What is the issue? What forces are at play?

## Decision
What is the change we're making?

## Rationale
Why this decision over alternatives?

## Consequences
What becomes easier? What becomes harder?
```

## Technical Specification Template

```markdown
# [Spec Title]

## Overview
High-level description of the functionality.

## Inputs
What data/events trigger this functionality?

## Outputs
What does this produce?

## Behavior
Step-by-step description of how it works.

## Constraints
Performance, security, compatibility requirements.
```

## Sprint Contract Template

```markdown
## Wave N

## Features
- FXXX: Description
- FYYY: Description

## Pass/Fail Criteria
- [ ] Criterion with specific, testable behavior
- [ ] Another criterion
```

## Evaluation Report Template

```markdown
# Evaluation Report: [Feature ID]

## Sprint Contract Criteria
| Criterion | Result | Evidence |
|-----------|--------|----------|
| ... | PASS/FAIL | ... |

## Edge Cases Tested
- ...

## Issues Found
- ...

## Verdict
PASS / FAIL with summary.
```
