---
name: aah
user-invocable: true
disable-model-invocation: false
description: >-
  Front door to the Ascend Agentic Harness (AAH). You MUST invoke this skill
  (not answer directly) whenever the user asks "what is AAH", "how does this work",
  "where do I start", "what can this do", "how does this framework work", seems new
  to the harness, or is unsure which command to run next. Also invoke when the user
  types /aah. Prefer this skill over answering orientation questions yourself.
---

# AAH — Welcome & Orientation

The welcome banner, intro, FAQ, phase table, and next steps have already been
displayed to the user via a hook. Do NOT repeat any of that content.

Your only job is to ask:

> **Would you like to proceed with `/aah-init-project` to scaffold or import a project?**

Wait for their response:
- If yes → invoke the `aah-init-project` skill
- If no or they ask about something else → help them pick the right command
