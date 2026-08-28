## Id
- F-MOD-000

## Title
- Task CRUD API Endpoints

## Module Ref
- MOD-000

## Description
PostgreSQL-backed Task CRUD API built with FastAPI, realizing the **Data Model** and **API
Contracts** sections of `architecture-overview.md`. Persists tasks in Postgres (connection from
`DATABASE_URL`), exposes create/read endpoints, and wires them into the app's composition root so
they are served by the running application.

**Behavioral expectations** (the acceptance intent — this is the spec the build-phase TDD works
against; there is no separate Test Cases section):
- Creating a task with a title persists it and returns the created record with a generated id.
- Fetching a task by id returns the matching record, or a not-found response when it is absent.
- The endpoints are reachable on the running app (routes registered in the composition root).

## Layers
- api

## Dependencies
- F-MOD-001

## API Contracts
```yaml
api_contracts:
  produces:
    - operation_id: {operation_id}
      schema_file: schema/{MOD-NNN-api-schema.yaml}
      request_schema: {RequestSchemaName}
      response_schema: {ResponseSchemaName}
  consumes:
    - operation_id: {operation_id}
      schema_file: schema/{MOD-NNN-api-schema.yaml}
      request_schema: {RequestSchemaName}
      response_schema: {ResponseSchemaName}
```

## Test Config

## Lint Config
- {package-root} — {default | path/to/supplied-config}

## Constraints

## Required Env Variables
- DATABASE_URL — Postgres connection string the data layer reads at startup

---
<!-- REFERENCE: Field rules below. Do not copy into feature files. -->

## Field Rules

| Field | Type | Rule |
|-------|------|------|
| `Id` | string | `F-{MODULE_ID}` — one feature per module (no `-NN` sequence). Module ID from module-map. |
| `Title` | string | Short label (~6-7 words) summarizing the module's purpose |
| `Module Ref` | string | Module `id` from module-map.yaml (e.g., MOD-000), NOT module name |
| `Description` | string | **The spec.** Rich and actionable: WHAT the module does, WITH which stack/framework, and HOW — by **referencing the authoritative design docs by path** (e.g. wireframes, agent-topology, `architecture-overview.md` sections). MUST end with a **Behavioral expectations** list — the acceptance intent in prose — because the build-phase implementer derives its TDD tests from these (there is no Test Cases section). |
| `Layers` | list | Layer keys from module-map `layers` field. Always bulleted (`- layer`) |
| `Dependencies` | list | Feature IDs only (`- F-MOD-001`). With 1 module = 1 feature, these are the module's upstream modules. Leave heading present but empty if none |
| `API Contracts` | yaml block | OMIT this heading entirely for features with no endpoint touch (pure UI, helpers, contract models, prompt builders, LLM clients, etc.). **Empty heading with no yaml body is a spec violation — either fill it or delete it.** Otherwise: fenced yaml block with `produces:` (implements an operation) and/or `consumes:` (calls one). Each entry: `operation_id`, `schema_file`, `request_schema`, `response_schema` — copied verbatim from `.aah/architecture/schema/MOD-NNN-api-schema.yaml`. **`schema_file:` MUST include the `schema/` prefix.** |
| `Test Config` | mapping | Leave empty during planning. The feature implementer adds the portable `command` and actual `test_paths` after writing its TDD tests |
| `Lint Config` | list | One line per package root THIS feature scaffolds: `- <root> — <source>`, where `source` is `default` (the framework's own template) or a repo-relative path to a config the architecture docs or knowledge base supplied. The authoring agent decides provided-vs-default per root and writes the creation instruction above the list — the implementer follows this section and nothing else, so a root omitted here gets no config and the build's standards gate cannot check that package. Keep the heading with nothing below it when the feature scaffolds no package |
| `Constraints` | list | Hard constraints from design docs/standards. Leave heading present but empty if none |
| `Required Env Variables` | list | Env var NAMES this module's code reads at startup or test time, drawn from the prompt's `## Required Env Catalog`. One line per var: `- VAR_NAME — purpose`. **Names only, NEVER values** — values live in the gitignored root `.env`; `.env.example` is the checked-in catalog of names and safe placeholders. Keep the heading with nothing below it when the feature's code reads no env vars. Parsed into the `required_env` contract key that gates the test run |

## Section Rules

- ALL `## ` headings above the `---` line MUST be present in every feature file, even if empty —
  EXCEPT `## API Contracts`, which is omitted entirely for features with no endpoint touch
- Heading names are EXACT — no renaming (`## Layers` not `## Layer`, `## Description` not `## Summary`)
- List fields MUST use `- ` bullet prefix, never bare text
- `## Dependencies` entries must be feature IDs (`F-{MODULE_ID}`), never module IDs
- `## Description` MUST include a **Behavioral expectations** list — this is the definition-of-done
  intent the build-phase TDD works against. There is NO `## Test Cases`, `## Acceptance Criteria`,
  `## File Scope`, or `## Knowledge Used` section — those were removed in v2. All references the
  implementer needs (design docs, wireframes, topology) are named inline in the Description.
- `## API Contracts` values (`operation_id`, `schema_file`, `request_schema`, `response_schema`)
  must match tokens from `.aah/architecture/schema/*-api-schema.yaml` verbatim — never invent, never
  rename. `## API Contracts` heading + empty yaml body is FORBIDDEN. `schema_file:` MUST include the
  `schema/` prefix.
- `## Required Env Variables` entries are NAMES ONLY (`- VAR_NAME — purpose`). A `: value` form is
  FORBIDDEN — it would commit a secret. Every name listed here must also appear in `.env.example`
- `## Lint Config` carries the creation instruction plus one line per package root this feature
  scaffolds (`- <root> — <source>`). The implementer creates exactly what this names and nothing
  more — there is no fallback
