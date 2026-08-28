---
name: tdd
description: Test-driven development — the red→green→refactor loop. Use when building a feature or fixing a bug test-first, when the user mentions "red-green-refactor", or when writing integration tests. In the AAH build phase the feature-implementer follows this skill on every module.
user-invocable: false
---

<!--
AAH adaptations (kept minimal, so the loop discipline is faithful):
  - The "spec" is the feature's `## Description` behavioral expectations + `## API Contracts`
    (there is no separate Test Cases section — tests are emergent, written here).
  - The pre-agreed "seams" are the module's public interface named in that spec (endpoints,
    exported functions). The AAH build phase is unsupervised, so treat the spec's behavioral
    expectations AS the agreed seams — you do not pause to ask a human.
  - "CONTEXT.md" ≙ the project `CLAUDE.md` (conventions/decisions), loaded every run.
  - The `code-review` skill referenced below is AAH's Spec-axis reviewer, which the implementer
    invokes after going green.
-->

# Test-Driven Development

TDD is the red → green loop. This skill is the reference that makes that loop produce tests worth keeping: what a good test is, where tests go, the anti-patterns, and the rules of the loop. Every section applies on every cycle — consult them before and during the loop, not after.

When exploring the codebase, read the project `CLAUDE.md` (if it exists) so test names and interface vocabulary match the project's domain language, and respect the conventions and decisions recorded there.

## What a good test is

Tests verify behavior through public interfaces, not implementation details. Code can change entirely; tests shouldn't. A good test reads like a specification — "user can checkout with valid cart" tells you exactly what capability exists — and survives refactors because it doesn't care about internal structure.

See [tests.md](tests.md) for examples and [mocking.md](mocking.md) for mocking guidelines.

## Seams — where tests go

A **seam** is the public boundary you test at: the interface where you observe behavior without reaching inside. Tests live at seams, never against internals.

**Test at the seams the spec names.** The feature's `## Description` behavioral expectations and `## API Contracts` define the module's public interface — its endpoints and exported functions. Those are your agreed seams. Testing effort lands on those critical paths and the complex logic behind them, not on every internal edge case.

Ask: "What's the public interface the spec describes, and which seams should I test?"

## Anti-patterns

- **Implementation-coupled** — mocks internal collaborators, tests private methods, or verifies through a side channel (querying the database instead of using the interface). The tell: the test breaks when you refactor but behavior hasn't changed.
- **Tautological** — the assertion recomputes the expected value the way the code does (`expect(add(a, b)).toBe(a + b)`, a snapshot derived by hand the same way, a constant asserted equal to itself), so it passes by construction and can never disagree with the code. Expected values must come from an independent source of truth — a known-good literal, a worked example, the spec.
- **Horizontal slicing** — writing all tests first, then all implementation. Bulk tests verify _imagined_ behavior: you test the _shape_ of things rather than user-facing behavior, the tests go insensitive to real changes, and you commit to test structure before understanding the implementation. Work in **vertical slices** instead — one test → one implementation → repeat, each test a **tracer bullet** that responds to what the last cycle taught you.

## Rules of the loop

- **Red before green.** Write the failing test first, then only enough code to pass it. Don't anticipate future tests or add speculative features.
- **One slice at a time.** One seam, one test, one minimal implementation per cycle.
- **Refactoring is not part of the loop.** It belongs to a separate review stage, not the red → green implementation cycle.

## After green — spec review

When the behavioral expectations are covered and the suite is green, invoke the `code-review` skill (Spec axis) to check the module's code against the feature's `## Description`. Fix any missing/partial requirements or scope creep it finds, then re-run the loop. Only return once the tests are green and the spec review is clean.
