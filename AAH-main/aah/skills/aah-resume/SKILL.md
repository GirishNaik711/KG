---
name: aah-resume
description: Resume the AAH project in the current folder mid-session (coming soon)
user-invocable: true
disable-model-invocation: true
---

# Resume an AAH Project — Coming Soon

This skill is being reworked for the `.aah/` project model and is **not yet
available**.

The previous implementation is preserved in `SKILL.md.bak` in this folder for
reference and will be revamped in a future update.

## What to do in the meantime

- To see where a project stands, inspect its state directly:

  ```bash
  aah run core.build.load_impl_context --project-path .
  ```

- Or open the project folder (the one containing `.aah/`) and re-run the
  relevant phase command from the AAH banner (`/aah-build`, `/aah-plan`, etc.).

Tell the user this skill is coming soon and point them to the options above.
