---
name: playwright
purpose: Browser testing & UI verification
phases: [build]
action_hint: "`browser_navigate` + `browser_take_screenshot` to verify UI"
tools:
  - browser_navigate
  - browser_snapshot
  - browser_click
  - browser_fill_form
  - browser_take_screenshot
  - browser_console_messages
  - browser_network_requests
  - browser_evaluate
  - browser_wait_for
  - browser_press_key
  - browser_select_option
---
# Playwright MCP Server

## When to Use
- To verify UI renders correctly after a feature is built
- To validate frontend functionality (forms, navigation, interactions)
- To check browser console for errors
- During the runtime-validation step of the build phase (UI render check)
- To debug UI behavior by inspecting network requests

## When NOT to Use
- For API-only testing (use curl/httpx directly)
- For unit tests (use the project's test framework)
- During Discuss/Architect/Plan phases (no UI to test yet)

## Workflow

### Basic page verification
```
1. browser_navigate(url="http://localhost:3000")
2. browser_take_screenshot()  → visual verification (DEFAULT — capture the rendered image)
3. browser_snapshot()  → only if you also need the accessibility tree for element structure/refs
```

### Form testing
```
1. browser_navigate(url="http://localhost:3000/login")
2. browser_fill_form(selector="#email", value="test@example.com")
3. browser_fill_form(selector="#password", value="password123")
4. browser_click(selector="button[type=submit]")
5. browser_wait_for(selector=".dashboard")
6. browser_take_screenshot()  → visual proof of the result (snapshot only if you need element refs to act further)
```

### Debug console errors
```
1. browser_navigate(url="http://localhost:3000")
2. browser_console_messages()  → check for JS errors
3. browser_network_requests()  → check for failed API calls
```

## Context Management
- DEFAULT to `browser_take_screenshot` for UI verification — capture the actual rendered image so the user can see the page.
- Use `browser_snapshot` (accessibility tree) only when you need element structure/refs (e.g. to locate a target before a click) — NOT as a substitute for the visual screenshot.
- Cost note: screenshots are ~10k+ tokens vs snapshots at 1-3k. Accept the cost — visual proof is the priority.

## Key Rules
- Always `browser_navigate` before other actions
- Use `browser_take_screenshot` as the DEFAULT for verifying the UI (visual proof of the rendered page)
- Use `browser_snapshot` (accessibility tree) only for element structure/refs when you need to act on a specific element
- Check `browser_console_messages` after navigation to catch JS errors
- For testing, ensure the local dev server is running first
- When saving a screenshot, pass an explicit `filename` under a known dir (e.g. `.aah/build/screenshots/wave-<N>.png`) so the file is easy to find — a bare filename lands in the project root
