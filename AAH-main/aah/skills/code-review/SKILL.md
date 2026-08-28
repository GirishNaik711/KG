---
name: code-review
description: Spec-axis code review — does the code faithfully implement what the feature spec asked for? Use after a module's tests are green to check the implementation against the feature's Description before returning. In the AAH build phase the feature-implementer invokes this itself, in-loop.
user-invocable: false
---

<!--
Adapted from https://github.com/mattpocock/skills/blob/main/skills/engineering/code-review/SKILL.md
AAH adaptations:
  - SPEC AXIS ONLY. The Standards axis (repo coding standards + Fowler smell baseline) is
    intentionally deferred and NOT run here.
  - SHA-FREE. There is no "pin a fixed point / git diff" step — the build works on a single shared
    worktree and reviews the module's CURRENT code, not a diff range.
  - The "spec" is the feature's `## Description` (its behavioral expectations) + `## API Contracts`.
  - Invoked in-loop by the feature-implementer (Skill tool). It is a self-review that runs after the
    module goes green and before the implementer returns — not a separate agent.
-->

# Code Review — Spec Axis

Check whether the module's code faithfully implements what its feature spec asked for. This is the
**Spec** axis only; repo coding-standards review is out of scope here.

## 1. Identify the spec

The spec is the feature file for the module under build: `.aah/plan/features/<feature-id>.md`.
The authoritative part is its `## Description` — specifically the **Behavioral expectations** it
lists — plus any `## API Contracts`. If no feature file is available, report "no spec available"
and stop.

## 2. Review the current code against the spec

Read the module's current implementation in the worktree (the code you just wrote/changed for this
feature) and compare it to the spec. Report, quoting the spec line for each finding:

- **(a) Missing or partial** — a behavioral expectation the spec asked for that the code does not
  implement, or implements only partially.
- **(b) Scope creep** — behavior in the code that the spec did not ask for.
- **(c) Wrong implementation** — a requirement that looks implemented but where the implementation
  does not match what the spec describes (wrong shape, wrong contract, wrong condition).

Pay particular attention to **integration**: an expectation that an endpoint or capability is
reachable on the running app means the route/handler must actually be wired into the composition
root — a handler that exists but is never registered is a **missing** finding.

## 3. Report

Return the findings as a short list under `## Spec`, each tagged (a)/(b)/(c) with the spec line
quoted and the file/location in the code. If there are no findings, say "Spec: no findings".

Keep it under ~400 words. Do not review coding style, naming, or refactoring opportunities — that is
the deferred Standards axis, not this review.
