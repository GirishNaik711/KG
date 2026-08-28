# API / Interface Contract

**Project:** {project_name}
**Date:** {date}
**Tier:** {mvp | prod}

<!-- One doc covers all modules. Repeat sections §1–§5 below per module,
     under a `## {module_id} — {module_name}` heading for each. -->

---

## 1. Endpoint Inventory

| Endpoint | Method | Purpose | Auth | Status |
|----------|--------|---------|------|--------|
| | GET / POST / PUT / DELETE | | required / public | active / planned |

---

## 2. Request / Response Contracts

### {endpoint_name}

**Method:** {GET/POST/PUT/DELETE}
**Path:** {/api/v1/...}

#### Request

| Field | Type | Required | Validation |
|-------|------|----------|------------|
| | | | |

#### Response (success)

| Field | Type | Description |
|-------|------|-------------|
| | | |

#### Response (error)

```json
{
  "error": "{error_code}",
  "message": "{human-readable}",
  "details": {}
}
```

---


<!-- Auth details per endpoint. Derived from P9. -->

| Endpoint | Auth Method | Roles Allowed | Scopes |
|----------|-----------|---------------|--------|
| | | | |

---

## 4. Versioning & Rate Limits

| Aspect | Strategy |
|--------|----------|
| Versioning | {URL path / header / query param} |
| Current version | |
| Deprecation policy | |
| Rate limit (default) | |
| Rate limit (authenticated) | |
| Burst limit | |

---

## 5. SLA

<!-- Per-endpoint availability and latency targets. -->

| Endpoint | Availability | Latency (P50) | Latency (P99) | Timeout |
|----------|-------------|---------------|---------------|---------|
| | | | | |

---

## Provenance

<!-- Section-wise table: one row per major section of this doc. Origin = Inherited
     (carried forward from upstream inputs: PRD + decisions in .aah/discuss/,
     research-prd.md, project-intent.yaml, module-map.yaml) or Authored (newly created
     during the architecture phase, incl. user Q&A). Use "Inherited + Authored" for a
     mix. Name the specific source. Every section must appear. -->

| Section | Origin | Source |
|---------|--------|--------|
| Endpoint Inventory | {Inherited / Authored} | {e.g. module-map.yaml API layer} |
| Request / Response Contracts | {Inherited / Authored} | {source or arch-phase} |
| Per-Endpoint Auth | {Inherited / Authored} | {e.g. decision-registry slug} |
| Versioning & Rate Limits | {Inherited / Authored} | {source or arch-phase} |
| SLA | {Inherited / Authored} | {source or arch-phase} |
