# Mental Model

Build and maintain a mental model of the problem space before acting.

## Approach

- Before proposing a solution, think step-by-step through the problem.
- Consider at least two perspectives or approaches before choosing one.
- State your current understanding explicitly so others can correct it.
- When new information arrives, update your model rather than ignoring contradictions.

## During Execution

- If the problem turns out to be different than expected, stop and re-assess.
- Keep a running summary of what you know, what you assume, and what is uncertain.
- Distinguish between facts (from code, docs, user statements) and inferences.

## Anti-Patterns

- Do not skip straight to implementation without understanding the problem.
- Do not hold onto a mental model that contradicts evidence.
- Do not assume your model is complete — there are always unknowns.

## Application to Expertise Files

This mental model philosophy governs how you build, update, and consume the project
expertise system (Tier 1 expertise.yaml + Tier 2 domain files).

**Operational procedures:** See `aah-expertise` skill for schemas, formats, and step-by-step execution.

### When Seeding

- Extract meaningful architectural decisions from AAH artifacts, not surface-level descriptions.
- For brownfield: supplement AAH artifacts with config file analysis and codemap stats.
- Expertise starts as hypothesis — validated and refined through wave execution.
- Do NOT include structural codebase info (file paths, symbol lists) — codemap.db handles that.

### When Loading for Execution

- Treat expertise as current best understanding, not absolute truth.
- If code contradicts an expertise entry, trust the code — flag the contradiction.
- Use expertise to guide decisions, not dictate them.

### When Extracting Learnings

- Capture what applies beyond the feature; group by topic, not by type.
- Extract SEMANTIC knowledge only — structural data belongs to codemap.db.
- If a feature deviated from expected patterns, record WHY — the deviation is more valuable.
- Do NOT duplicate project_knowledge — learnings capture NEW knowledge from this wave.

### When Consolidating

- Require evidence from 2+ features before promoting insight to project_knowledge.
- When new insight contradicts existing project_knowledge, do not silently overwrite — note the contradiction and update with newer understanding.
- When a domain file contradicts Tier 1: update Tier 1 — the domain file is closer to ground truth.

### When Planning with Expertise

- Use expertise to inform feature grouping, not to constrain it.
- If expertise suggests a pattern but features don't fit, plan for the features.
- Flag areas where expertise is thin (empty keys, missing domains).
