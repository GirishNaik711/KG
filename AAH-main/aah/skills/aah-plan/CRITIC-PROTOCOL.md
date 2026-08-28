# Plan Critic Protocol (the AI step of the Plan Gate)

The Plan Critic is the **single AI step** of the Plan Gate (Step 6). It validates the
authored features (one per module) after the cheap deterministic steps pass. It implements
the **maker-checker principle**: the agent that authors a feature must NOT be the same agent
that validates it.

**Dispatch is per-feature and parallel.** Spawn one `general-purpose` critic agent **per
feature, all in parallel in one message** — mirroring the per-module authoring fan-out. Each
agent evaluates exactly one feature and returns the per-feature verdict below. A single
whole-project critic is NOT used — it is the slow, non-parallel path this protocol replaces.

---

## Isolation Principle

The critic does NOT receive:
- The maker agents' reasoning or conversation history
- Any "context" about why decisions were made

This isolation is intentional — the critic evaluates each feature on its own merits.

---

## Dimensions Checked

Under **1 module = 1 feature** with a rich Description (behavioral expectations) and no
pre-specified test cases, the critic checks two dimensions. **Module sizing is NOT checked
here** — it is an architecture-phase concern and has been removed from the critic.

### C — Coverage
The module's single feature covers everything the module owns — **except** API operations,
which are owned by dimension K (so the API concern is never double-counted).

**Critical if:**
- A capability listed in the module-map entry's `layers` has no matching behavioral expectation
  in the `## Description`.
- A wireframe screen mapped to this module (UI modules) is not reflected in the behavioral
  expectations. **UX presence/coverage lives here, in the AI step — there is no deterministic
  UX gate.**
- A wireframe file the `## Description` **cites** (any `applications_wireframes/*.md` path) does
  **not exist on disk** — a dangling reference to a removed/renamed screen. Verify each cited
  wireframe path resolves against `.aah/architecture/applications_wireframes/`. This is the UX
  analogue of the dimension-K `operation_id`/`schema_file` resolution check — a stale citation is
  a concrete, verifiable error, not a discretionary nit, so it is **critical**, not `minor`.
- A data-model entity the module owns (per the design docs) is absent from the Description.

### K — Contract completeness
The feature is a usable spec for TDD, and its API contract is correct. K is the **single home**
for the API check.

**(a) Behavioral-expectations presence — binary / blocking.**
- **Critical if:** the `## Description` has no concrete **Behavioral expectations** list.
- **Vague wording never blocks.** Wording that reads vague ("performs well", "handles errors
  gracefully" instead of `given X, when Y, then Z`) is logged as a **`major`** finding — a
  non-blocking note, not a critical. `major` findings do not fail the gate.

**(b) API-contract correctness.**
- **Critical if:** the module produces/consumes endpoints but the feature omits the
  `## API Contracts` block, **or** the block's `operation_id` / `schema_file` tokens do not
  resolve against the module's schema file.

---

## Verdict Format

One verdict per feature (each parallel agent returns its own):

```yaml
checkpoint: plan-gate-ai
feature: F-MOD-002
verdict: FAIL

findings:
  - id: CF-001
    severity: critical
    dimension: contract_completeness
    location: F-MOD-002 ## Description
    issue: "No Behavioral expectations list — nothing for the build-phase TDD to test against"
    suggestion: "Add given/when/then bullets covering create + fetch-by-id + not-found"

  - id: CF-002
    severity: critical
    dimension: coverage
    location: F-MOD-002 (module-map layer 'search')
    issue: "module-map lists a 'search' capability with no matching behavioral expectation"
    suggestion: "Add a behavioral expectation for the search capability"

  - id: CF-003
    severity: critical
    dimension: contract_completeness
    location: F-MOD-002 ## API Contracts
    issue: "operation_id 'searchItems' does not resolve against MOD-002-api-schema.yaml"
    suggestion: "Correct the operation_id to match the schema file, or add the missing operation"

  - id: CF-004
    severity: major
    dimension: contract_completeness
    location: F-MOD-002 ## Description
    issue: "Expectation 'handles errors gracefully' is vague (non-blocking note)"
    suggestion: "Restate as given/when/then, e.g. 'given a malformed body, when POSTed, then 400'"

summary:
  critical: 3
  major: 1
  minor: 0
  pass_threshold: "0 critical findings"
```

---

## Pass/Fail Rules

**Deterministic threshold:** `critical == 0 → PASS`.

No AI decides pass/fail. Only `critical` severity blocks. `major`/`minor` findings (including
all vague-wording notes) are logged but do not block progression.

---

## What the Critic Does NOT Do

- It does NOT rewrite features (that's the maker's job).
- It does NOT split a module into multiple features, and it does NOT judge module size at all
  (there is one feature per module; sizing is an architecture-phase concern).
- It does NOT check dependency-DAG correctness (build_and_validate_dag owns that; the Plan Gate
  only confirms `dag.json` exists).
- It does NOT check runtime bootstrap or `.env` (deterministic gate concerns).
- It does NOT have more than ONE revision opportunity before escalation.
