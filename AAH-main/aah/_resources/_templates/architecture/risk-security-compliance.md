# Risk · Security · Compliance

**Project:** {project_name}
**Date:** {date}
**Tier:** {poc | mvp | prod}
**Scope:** Global, module-annotated

---

## 1. Trust Boundaries

<!-- Derived from P9 (actor/authority) probe. Where trust levels change in the system. -->

| Boundary | Inside (trusted) | Outside (untrusted) | Enforcement Mechanism |
|----------|-------------------|---------------------|----------------------|
| | | | |

### Boundary Diagram

```
┌─────────────────────────────────────────────────┐
│  TRUST ZONE: {name}                             │
│  ┌─────────┐    ┌─────────┐    ┌─────────┐     │
│  │ {comp1} │───►│ {comp2} │───►│ {comp3} │     │
│  └─────────┘    └─────────┘    └─────────┘     │
└───────────────────────┬─────────────────────────┘
                        │ ← BOUNDARY
                        ▼
              ┌─────────────────┐
              │  External / User │
              └─────────────────┘
```

### Module Annotations

| Module | Trust Zone | Crosses Boundary? | Notes |
|--------|-----------|-------------------|-------|
| MOD-000 | | | |
| MOD-001 | | | |

---

## 2. Auth & Secrets

| Aspect | Decision |
|--------|----------|
| Auth method | {JWT / OAuth2 / session / API key} |
| Identity provider | {internal / external IDP} |
| Session management | {stateless / server-side / hybrid} |
| Authorization model | {RBAC / ABAC / policy-based} |
| Secret storage | {env vars / vault / cloud KMS} |
| Credential rotation | {policy} |

### Role Matrix

| Role | Modules Accessible | Write Access | Admin |
|------|-------------------|--------------|-------|
| | | | |

---

## 3. Threat Model

| Threat | Category | Likelihood | Impact | Risk Score | Mitigation |
|--------|----------|------------|--------|------------|------------|
| | | H/M/L | H/M/L | | |

---

## 4. Compliance Matrix

<!-- Map regulations to implementation. -->

| Regulation / Standard | Requirement | Implementation | Verification |
|-----------------------|-------------|----------------|--------------|
| | | | |

---

## 5. NFR Matrix

<!-- Key non-functional targets. -->

| Category | Metric | Target | Measurement |
|----------|--------|--------|-------------|
| Performance | Response time (P50) | | |
| Performance | Response time (P99) | | |
| Performance | Throughput | | |
| Scalability | Concurrent users | | |
| Scalability | Data volume | | |
| Availability | Uptime SLA | | |
| Availability | RTO | | |
| Availability | RPO | | |

---

## Provenance

<!-- Section-wise table: one row per major section of this doc. Origin = Inherited
     (carried forward from upstream inputs: PRD + decisions in .aah/discuss/,
     research-prd.md, project-intent.yaml, module-map.yaml) or Authored (newly created
     during the architecture phase, incl. user Q&A). Use "Inherited + Authored" for a
     mix. Name the specific source. Every section must appear. -->

| Section | Origin | Source |
|---------|--------|--------|
| Trust Boundaries | {Inherited / Authored} | {source or arch-phase} |
| Auth & Secrets | {Inherited / Authored} | {e.g. decision-registry slug} |
| Threat Model | {Inherited / Authored} | {source or arch-phase} |
| Compliance Matrix | {Inherited / Authored} | {e.g. discuss-phase constraints} |
| NFR Matrix | {Inherited / Authored} | {e.g. discuss-phase requirements} |
