---
name: data-modeling-patterns
description: Data architecture conventions and modeling patterns
user-invocable: false
---

# Data Modeling Patterns

## General Principles

- Every table has a primary key (prefer UUID over auto-increment for distributed systems)
- Every table has `created_at` and `updated_at` timestamps
- Use soft deletes (`deleted_at`) unless hard delete is explicitly required
- Foreign keys are always indexed
- Use database-level constraints (NOT NULL, UNIQUE, CHECK) — don't rely on application-level only

## Naming Conventions

- Tables: plural snake_case (`user_accounts`, `order_items`)
- Columns: singular snake_case (`user_id`, `created_at`)
- Indexes: `idx_<table>_<columns>` (`idx_users_email`)
- Foreign keys: `fk_<table>_<ref_table>` (`fk_orders_users`)

## Relationships

- One-to-many: FK on the "many" side
- Many-to-many: join table named `<table1>_<table2>` alphabetically
- Self-referential: explicit column name (`parent_id`, `manager_id`)

## Migration Strategy

- Every schema change is a migration file
- Migrations are forward-only in production
- Always test migrations against a copy of production data
- Include both `up` and `down` migrations for development

## Data Validation

- Validate at the boundary (API input)
- Enforce at the database (constraints)
- Trust internal data between services
