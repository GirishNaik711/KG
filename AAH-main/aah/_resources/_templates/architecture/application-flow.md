# Application Flow

**Project:** {project_name}
**Date:** {date}
**Tier:** {poc | mvp | prod}

<!-- One doc covers all modules. Repeat sections §1–§8 below per module,
     under a `## {module_id} — {module_name}` heading for each. -->

---

## 1. Flow Summary

<!-- One paragraph: what this flow does end-to-end, who triggers it, what the outcome is. -->

---

## 2. Sequence Diagram — Happy Path

<!-- ASCII or Mermaid sequence diagram showing trigger → outcome.
     Must include: actor, all layers touched (UI → API → DB → external), response path. -->

```
{actor} → {UI component} → {API endpoint} → {DB/service} → response
```

---

## 3. Trigger & Entry Point

| Attribute | Value |
|-----------|-------|
| Trigger type | {user action / scheduled / event / API call} |
| Entry point | {UI page / API endpoint / event listener} |
| Actor | {user role / system / external service} |
| Preconditions | {auth required, prior module complete, etc.} |

---

## 4. Step-by-Step Flow

| Step | Layer | Action | Input | Output | Error Case |
|------|-------|--------|-------|--------|------------|
| 1 | UI | | | | |
| 2 | API | | | | |
| 3 | DB | | | | |
| ... | | | | | |

---

## 5. Data Touched

| Entity | Operation | Layer | Ownership |
|--------|-----------|-------|-----------|
| | read / write / create / delete | db / api / external | this module / shared (MOD-xxx) |

---

## 6. Integration Points

| Target | Direction | Protocol | Contract Reference |
|--------|-----------|----------|--------------------|
| | inbound / outbound | REST / gRPC / event / direct call | |

---

## 7. Error & Edge Cases

<!-- Document error states, empty states, edge cases. -->

| Scenario | Detection | Recovery | User Feedback |
|----------|-----------|----------|---------------|
| | | | |

---

## 8. State & Idempotency

<!-- Document state transitions, idempotency guarantees, retry behavior. -->

---

## Provenance

<!-- Section-wise table: one row per major section of this doc. Origin = Inherited
     (carried forward from upstream inputs: PRD + decisions in .aah/discuss/,
     research-prd.md, project-intent.yaml, module-map.yaml) or Authored (newly created
     during the architecture phase, incl. user Q&A). Use "Inherited + Authored" for a
     mix. Name the specific source. Every section must appear. -->

| Section | Origin | Source |
|---------|--------|--------|
| Flow Summary | {Inherited / Authored} | {e.g. research-prd.md} |
| Sequence Diagram — Happy Path | {Inherited / Authored} | {source or arch-phase} |
| Trigger & Entry Point | {Inherited / Authored} | {e.g. decision-registry slug} |
| Step-by-Step Flow | {Inherited / Authored} | {source or arch-phase} |
| Data Touched | {Inherited / Authored} | {e.g. module-map.yaml, data-model} |
| Integration Points | {Inherited / Authored} | {source or arch-phase} |
| Error & Edge Cases | {Inherited / Authored} | {source or arch-phase} |
| State & Idempotency | {Inherited / Authored} | {source or arch-phase} |
