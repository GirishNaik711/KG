# Architecture Overview

**Project:** {project_name}
**Date:** {date}
**Tier:** {poc | mvp | prod}
**Scope:** Global (one per project)

---

## 1. System Context (C4 Level 1)

<!-- High-level: what the system is, who uses it, what it connects to. -->

### Context Diagram

```
{ASCII or Mermaid C4 context diagram}
```

### System Boundaries

| Boundary | Inside | Outside |
|----------|--------|---------|
| | | |

---

## 2. Architecture Style & Key Decisions

<!-- Selected style (microservices, event-driven, layered, modular monolith, etc.) and rationale. -->

| Decision | Chosen | Rationale | Registry Slug |
|----------|--------|-----------|---------------|
| | | | |

### Design Principles

<!-- 3–5 principles guiding architectural choices -->

---

## 3. Backend / Frontend Split

<!-- How the system divides across BE and FE. Tech stack per side. -->

| Layer | Technology | Responsibility |
|-------|-----------|----------------|
| Frontend | | |
| Backend | | |
| Data | | |

---

## 4. Module Map & DAG

<!-- Embed or reference module-map.yaml. Show module dependency graph. -->

| Module | Name | Demo Criteria | Depends On |
|--------|------|---------------|------------|
| MOD-000 | | | |
| MOD-001 | | | |

### Dependency Graph

```
MOD-000 → MOD-001 → MOD-002 → ...
```

### Critical Path

<!-- Which module chain is longest / highest risk -->

---

## 5. Cross-Cutting Concerns

<!-- Concerns that span all modules — auth, logging, error handling, config. -->

| Concern | Strategy | Owner (module) |
|---------|----------|----------------|
| Authentication | | MOD-000 |
| Logging | | MOD-000 |
| Error handling | | MOD-000 |
| Configuration | | MOD-000 |

---

## 6. Non-Goals & Scope Boundaries

<!-- What this architecture explicitly does NOT address. Prevents scope creep. -->

| Non-Goal | Rationale |
|----------|-----------|
| | |

---

## 7. Folder Structure

<!-- Project directory layout. -->

```
{project root}/
├── src/
│   ├── ...
├── ...
```

---

## Provenance

<!-- Section-wise table: one row per major section of this doc. Origin = Inherited
     (carried forward from upstream inputs: PRD + decisions in .aah/discuss/,
     research-prd.md, project-intent.yaml, module-map.yaml) or Authored (newly created
     during the architecture phase, incl. user Q&A). Use "Inherited + Authored" for a
     mix. Name the specific source. Every section must appear. -->

| Section | Origin | Source |
|---------|--------|--------|
| System Context | {Inherited / Authored} | {e.g. research-prd.md, decision-registry slug} |
| Architecture Style & Key Decisions | {Inherited / Authored} | {e.g. decision-registry slug} |
| Backend / Frontend Split | {Inherited / Authored} | {e.g. decision-registry slug} |
| Module Map & DAG | {Inherited / Authored} | {e.g. module-map.yaml} |
| Cross-Cutting Concerns | {Inherited / Authored} | {source or arch-phase} |
| Non-Goals & Scope Boundaries | {Inherited / Authored} | {source or arch-phase} |
| Folder Structure | {Inherited / Authored} | {source or arch-phase} |
