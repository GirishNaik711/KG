---
name: api-conventions
description: Team API design patterns and conventions for consistent API development
user-invocable: false
---

# API Conventions

## REST API Design

- Use plural nouns for resources: `/users`, `/orders`, `/products`
- Use HTTP methods semantically: GET (read), POST (create), PUT (replace), PATCH (update), DELETE (remove)
- Return appropriate status codes: 200 (ok), 201 (created), 204 (no content), 400 (bad request), 401 (unauthorized), 403 (forbidden), 404 (not found), 409 (conflict), 422 (unprocessable), 500 (server error)
- Use consistent error response format:
  ```json
  {"error": {"code": "VALIDATION_ERROR", "message": "...", "details": [...]}}
  ```

## Naming

- URL paths: kebab-case (`/user-profiles`)
- Query params: snake_case (`?sort_by=created_at`)
- JSON fields: snake_case (`{"user_name": "..."}`)
- Headers: Title-Case (`X-Request-Id`)

## Pagination

- Use cursor-based pagination for large datasets
- Use offset/limit for small, stable datasets
- Always include `total`, `has_more` in paginated responses

## Versioning

- URL path versioning: `/api/v1/users`
- Major versions only — minor changes are backwards-compatible

## Authentication

- Bearer tokens in Authorization header
- Short-lived access tokens + refresh tokens
- API keys for service-to-service calls
