# Data Model Architecture

**Project:** {project_name}
**Date:** {date}
**Tier:** {poc | mvp | prod}
**Scope:** Hybrid (shared-core global + per-module extensions)

---

## 1. Shared Core (◄ P6 write-ownership)

<!-- Entities owned by no single module — shared across boundaries.
     Derived from P6 probe: "two flows writing one entity = shared core." -->

### Entity Catalog — Shared

| Entity | Owner Module | Consumers | Key Fields | Volume |
|--------|-------------|-----------|-----------|--------|
| | MOD-000 (or shared) | [MOD-001, MOD-002] | | |

### Relationships

<!-- Key entity relationships and cardinality for shared entities -->

---

## 2. Per-Module Extensions

<!-- Each module's private entities that only it writes. -->

### MOD-{001} — {module_name}

| Entity | Key Fields | Relationships | Volume |
|--------|-----------|---------------|--------|
| | | | |

### MOD-{002} — {module_name}

| Entity | Key Fields | Relationships | Volume |
|--------|-----------|---------------|--------|
| | | | |

---

## 3. ERD

<!-- Entity-Relationship Diagram — ASCII or Mermaid. Show shared core + module-owned entities. -->

```
{ERD diagram}
```

---

## 4. Storage & Access Patterns

<!-- How data is stored and accessed. Technology choices. -->

| Entity/Group | Storage Technology | Access Pattern | Indexing | Caching |
|-------------|-------------------|----------------|----------|---------|
| | | read-heavy / write-heavy / balanced | | |

---

## 5. Migrations & Retention

<!-- How schema changes are managed and data is retained/purged. -->

### Migration Strategy

| Aspect | Approach |
|--------|----------|
| Tool | {e.g. Prisma Migrate, Alembic, Flyway} |
| Versioning | {sequential / timestamp} |
| Rollback | {supported / forward-only} |
| Zero-downtime | {yes / no — strategy} |

### Data Retention

| Entity | Retention Period | Archive Strategy | Purge Trigger |
|--------|-----------------|------------------|---------------|
| | | | |

---

## 6. Schema Evolution

<!-- How breaking vs non-breaking changes are handled. -->

| Change Type | Policy | Communication |
|-------------|--------|---------------|
| Additive (new column/table) | | |
| Breaking (rename/remove) | | |
| Data backfill | | |

---

## Provenance

<!-- Section-wise table: one row per major section of this doc. Origin = Inherited
     (carried forward from upstream inputs: PRD + decisions in .aah/discuss/,
     research-prd.md, project-intent.yaml, module-map.yaml) or Authored (newly created
     during the architecture phase, incl. user Q&A). Use "Inherited + Authored" for a
     mix. Name the specific source. Every section must appear. -->

| Section | Origin | Source |
|---------|--------|--------|
| Shared Core | {Inherited / Authored} | {e.g. research-prd.md, module-map.yaml} |
| Per-Module Extensions | {Inherited / Authored} | {e.g. module-map.yaml data/storage layer} |
| ERD | {Inherited / Authored} | {source or arch-phase} |
| Storage & Access Patterns | {Inherited / Authored} | {e.g. decision-registry slug} |
| Migrations & Retention | {Inherited / Authored} | {source or arch-phase} |
| Schema Evolution | {Inherited / Authored} | {source or arch-phase} |
