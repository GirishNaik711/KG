---
name: testing-standards
description: Testing philosophy and patterns for the AAH framework
user-invocable: false
---

# Testing Standards

## Core Philosophy

Prefer functional tests (real services, real I/O). Mocks are permitted as
fallback when functional testing is not feasible.

## Approach by Category

- **Database** — real test DB, transactions that roll back; mocks if DB unavailable
- **API** — start real server, make real HTTP requests; mocks if server unavailable
- **File system** — use temp directories; mocks if filesystem access not feasible
- **External services** — real sandbox when available; mocks if not
- **UI / Frontend** — e2e (Playwright/Cypress) preferred; `@testing-library` or mock frameworks (`jest.mock`, `vi.mock`, `msw`) permitted when e2e not feasible

## Test Structure

```python
def test_user_can_login(running_app, test_db):
    # Arrange: real user in real database
    create_user(test_db, email="test@example.com", password="secret")

    # Act: real HTTP request to real server
    response = running_app.post("/auth/login", json={
        "email": "test@example.com",
        "password": "secret"
    })

    # Assert: real response
    assert response.status_code == 200
    assert "token" in response.json()
```

## Writing Tests — Good vs Bad Tests

Tests are written via TDD in the build phase (red→green→refactor) — see the `tdd` skill, whose
`tests.md` is the canonical good-vs-bad-tests reference: test observable behavior through the public
interface, assert against independent known values (never recompute the expected value the way the
code does), and avoid coupling to internal structure or asserting on call counts/order.
