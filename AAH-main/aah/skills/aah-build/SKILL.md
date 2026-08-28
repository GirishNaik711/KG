---
name: aah-build
description: Execute the AAH build phase — a lean, sequential TDD driver that builds one module (one feature) at a time
user-invocable: true
disable-model-invocation: false
---

# AAH Build Phase — Sequential TDD Driver

This skill is a **hardcoded sequential driver**, not an orchestrator. It reads the plan's DAG order,
continues on the current branch, and builds each module **one at a time** by dispatching the
`aah-feature-implementer` (which does vanilla TDD + a self code-review).
After each module the app is **booted with its own start commands, with `.env` supplied by
`python-dotenv`** — you are asked before it launches, and asked again whether to close it and move on.
That review **is** the gate. When all modules are done, you run the **whole-codebase standards gate
and the regression suite** once each, promote, and finish.

All work happens in `$PROJECT_DIR` on the current branch. You never write code, run raw tests, or fix
failures yourself — the implementer does that, and `Skill("aah-fix")` handles fixes.

## Delegation Rules (apply throughout)

- **You never write code, run raw `pytest`/`npm test`, run `git merge`, or edit `feature-list.json`/
  `claude-progress.json` by hand.** Dispatch the implementer, run `aah run core.*` commands where a
  step says to, ask the user via `AskUserQuestion`, and route fixes through `Skill("aah-fix")`.
- **If a framework tool (`aah run core.*`) fails:** fix only the git precondition (commit dirty
  state / checkout the right branch) and retry once; if it still fails, surface to the user. Never
  work around it with raw shell.
- **`AskUserQuestion` is mandatory for every user question** — never present choices as plain text.

## Ports — this is the `build` spine node

The `port-executor-agent` only **fetches** an ordered plan; this skill's main loop **runs** each
port (so port-skills can use `AskUserQuestion`). The plan lists ports to run now (`fire`),
within-ports to run later at their anchors (`within_plan`), and ports to only report
(`hand_back`, `blocked`).

**How to run a port** (moves `pending → in-progress → completed`):
1. Announce it (`ℹ️ Auto-triggering port <id> (<ref>)`), then mark started:
   `aah run core.ports.executor --project-path "$PROJECT_DIR" update --activity <id> --status in-progress`
2. Invoke by `type`: `skill`→Skill · `agent`→Agent · `workflow`→Workflow.
3. The instant it returns, mark done (or `--status failed`):
   `aah run core.ports.executor --project-path "$PROJECT_DIR" update --activity <id> --status completed --artifact <produces>`

**Within-ports** carry an `anchor_location` — run each exactly at the point it names (never earlier,
later, or batched). Any un-run within-port at the close = phase FAILED (the Step 10 completeness gate
catches it).

---

## START

### 1. Phase transition + context
```bash
PROJECT_DIR=$(aah run core.common.config project-path)
AAH_DIR="$PROJECT_DIR/.aah"
aah run core.common.phase_transition start build
```

### 2. START ports bookend — `build / before`
```
Agent(port-executor-agent, { node: "build", position: "before" }, run_in_background: false)
```
Run each `fire` port per "how to run a port"; **retain `within_plan`** (each runs later at its
`anchor_location`).

### 3. Issue Brief — always runs
Invoke `aah-issue-reporter`: "Brief me on **build** issues." Act on the brief with phase context
(absorb → re-entry, note status, or surface untriaged). If version_control is enabled
(`aah run core.version_control.cli status`) and a GitHub issue triggered this run, comment the
decision back (`aah run core.version_control.cli comment --issue <N> --body "..."`); route fixes to
`Skill("aah-fix", args: "<feedback> (originating_issue: <N>)")` and close only when fully resolved.

### 4. Resume sync — if version_control enabled
`aah run core.version_control.cli status`; if enabled, invoke `aah-issue-syncer`: "Full resync on
build resume — reconcile all offline edits." Read its report; surface structural inbound changes /
orphan issues / conflicts to the user before proceeding.

### 5. Brownfield intel freshness (non-blocking)
If `.aah/codebase-intel/*.md` exist and are stale, refresh once:
```bash
aah run core.intel.check_intel_update staleness-check --project-path "$PROJECT_DIR"
# if stale:
/aah-codebase-profile --mode refresh
aah run core.intel.check_intel_update reset --project-path "$PROJECT_DIR"
```
Never block the build on intel.

---

## BUILD — sequential

### 6. Determine order + retain the current branch
Read the topological order from the plan's DAG (one feature per module):
```bash
cat "$AAH_DIR/plan/dag.json"   # nodes in dependency order — this is the build order
```
Continue on the current branch — every module is built on it, in sequence:
```bash
BUILD_BRANCH=$(git -C "$PROJECT_DIR" branch --show-current)
test -n "$BUILD_BRANCH" || { echo "Detached HEAD — stop before build"; exit 1; }
```
Do not create, reset, or switch branches. If the tree is dirty, commit the pending artifacts
before dispatching an implementer; never stash or discard them.

### 7. Per-module loop (SEQUENTIAL — one at a time)
For each feature id in DAG topological order, with `<i>` its 0-based position in that order:

1. `aah run core.common.feature_list update-lifecycle <id> implementing`
2. **Dispatch the implementer** (foreground — build is sequential, one module at a time):
   ```
   Agent(aah-feature-implementer, {
     feature-id: <id>,
     SUBJECT_BRANCH: "<BUILD_BRANCH>",
     ATTEMPT_ID: "<id>-attempt-1",
     PROJECT_DIR: "<PROJECT_DIR>"
   }, run_in_background: false)
   ```
   The implementer starts in `$PROJECT_DIR` on the build branch. It reconstitutes context (deps'
   code, API Contracts, CLAUDE.md), does red→green TDD from the `## Description` behavioral
   expectations, self-reviews via the `code-review` skill,
   commits, and produces official per-feature test evidence. You do NOT re-run its tests.
3. **Boot + user review — THE GATE.** Runs for every module except `<i> == 0`, unless module 0 is the
   only module (module 0 is scaffolding with nothing assembled to boot). No runtime validator, no
   Docker, no Playwright — you start the project's own declared surfaces and let the user look.

   **3a. Ask before launching.** `AskUserQuestion`: "Module `<id>` implemented (tests
   `<pass/fail>`). Launch the app for review?"
   - **Launch it** → 3b · **Skip the boot** → item 4 (approved without a look)
   - **Request changes** → `Skill("aah-fix", args: "<what to change> (feature: <id>)")`, then redo 3a
   - **Stop the build** → halt, leave the tree committed

   **3b. Boot it** — run **§7A** with `<i>`, leaving the services running. A failed boot does not stop
   the build: carry it into 3c with the real error from its log and let the user decide. Never retry
   silently more than once.

   **3c. Hand over, then ask before closing.** Give the real URLs, preferring `localhost` over
   `127.0.0.1` (it must match `CORS_ORIGIN` and whatever origin the frontend hardcodes), plus **the
   hand-over warning**: nothing automated is touching the app now, so anything they do writes real
   data to the real store. Then `AskUserQuestion`: "Finished with the app — close it and proceed to
   the next module?"
   - **Close it and proceed** → §7A step 5, then item 4
   - **Keep it running and proceed** → item 4, warning them the next module's boot needs these ports
     and will stop these processes first (or fail with the port in use)
   - **Request changes** → §7A step 5, then `Skill("aah-fix", args: "<what to change> (feature:
     <id>)")`, then redo 3a — the fix moved the code you just booted
   - **Stop the build** → §7A step 5, halt, leave the tree committed
4. On proceed: `aah run core.common.feature_list update-lifecycle <id> done` ·
   `aah run core.common.progress update`.
5. Move to the next module.

Commits accumulate on the one build branch.

### 7A. Boot procedure
Called from step 7.3b (per module) and step 13 (end-of-build review), with `<i>` the module index.

`.env` is loaded by the **child process, never by the shell** — `uv run --with python-dotenv` makes
the package importable ephemerally (so `pyproject.toml` needs no new dependency) and the framework
reads `.env` itself.

**1. Ensure the service declaration exists** — `$AAH_DIR/build/app-services.yaml`.

**Required keys per service:** `name` (short id, also the log filename) · `cwd` (relative to
`$PROJECT_DIR`; `.` is the repo root) · `command` · `health` (a URL). Order in the file **is** the
start order: dependencies first, backend before frontend. A file missing a key, or with a service you
cannot map to a real process, → stop and report. Never guess a port.

**Present** → read it and go to step 2:
```yaml
services:
  - name: backend
    cwd: .
    command: uv run --with python-dotenv python -m flask --app backend.app run --port 5000 --no-reload
    health: http://127.0.0.1:5000/health
  - name: frontend
    cwd: frontend
    command: npm run dev
    health: http://127.0.0.1:5173
```

**Absent** → **create it now.** This happens once, at the first boot. Start from the template at
`aah/_resources/_templates/build/app-services.yaml`, then:

  **a. Inventory the surfaces** — one entry per *independently startable process*. A backend and a
  frontend are two entries; a worker or a second API is another. Anything started by another service
  (a forked worker, a bundler inside `npm run dev`) is NOT its own entry.

  **b. Resolve each port and health path from the code that already fixes them** — `vite.config.js`,
  `app.run(port=…)`, a hardcoded API base in the frontend — **never a convention default**. If the
  frontend hardcodes an API origin, the backend MUST bind that exact port or the app is broken. Pick a
  health path that exists: a dedicated `/health` if the code defines one, otherwise the served root.

  **c. Compose each command** from the table below, matched to what the code actually is (an app
  factory is not a module-level `app`).

  **d. Confirm the whole set with ONE `AskUserQuestion`** — list every service with its command, port
  and health URL — then `Write` the file and commit it. (This is the skill's own state, not a
  verification artifact, so a plain `Write` is correct.) Do not boot anything before it is confirmed.

| Stack | command |
|---|---|
| Flask factory (`create_app`) | `uv run --with python-dotenv python -m flask --app <mod> run --port <p> --no-reload` |
| FastAPI / ASGI | `uv run --with python-dotenv python -m uvicorn <mod>:app --port <p> --env-file .env` |
| Django | `uv run --with python-dotenv python manage.py runserver <p>` — only if `settings.py` calls `load_dotenv()`; if it does not, say so instead of booting a process that will die on missing config |
| Node / Vite | `npm run dev` — loads `.env` natively, add nothing |

`--no-reload` is required: the reloader double-starts and orphans a process you cannot kill by PID.

**2. Start each service**, in declaration order. `exec` is required — without it `$!` is a throwaway
subshell instead of the launcher:
```bash
mkdir -p "$AAH_DIR/build/boot-logs"
( cd "$PROJECT_DIR/<cwd>" && exec <command> ) > "$AAH_DIR/build/boot-logs/<name>.log" 2>&1 &
echo $!    # the LAUNCHER's pid — useful for logs, but NOT the listener; see step 5
```
`$!` is the launcher (`uv`, `npm`), which then **forks** the real server (`python`, `node`). So the
recorded PID is never sufficient to stop the service — step 5 resolves the listener by port.

**3. Health-probe each** — up to 60s:
```bash
for _ in $(seq 30); do
  CODE=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 5 "<health>" 2>/dev/null)
  [ "$CODE" != "000" ] && [ "$CODE" -lt 500 ] 2>/dev/null && break
  sleep 2
done
echo "$CODE"
```
Any code **< 500 (404 included)** means it is serving; `000` is curl failing to connect, never a pass.
If it never turns healthy, read that service's log and report the real error verbatim. **Never claim a
service is healthy without an HTTP code.**

**4. Record the result** at `$AAH_DIR/build/boot-results/module-<i>-boot.json` — plain JSON, written
directly. **Advisory and unattested**; no gate reads it:
```json
{"module": 1, "all_healthy": true, "logs": ".aah/build/boot-logs/",
 "services": [{"name": "backend", "url": "http://127.0.0.1:5000/health", "http_code": 200, "healthy": true, "pid": 12345}]}
```

**5. Stop when told to — resolve the listener by PORT, not by the recorded PID.** The launcher forked
the server (step 2), and on Windows git-bash `$!` is a bash PID while `netstat` reports Windows PIDs —
the two are different namespaces, so killing `$!` alone leaves the listener running:
```bash
for PORT in <port1> <port2>; do
  for P in $(netstat -ano | grep LISTENING | grep ":$PORT " | awk '{print $NF}' | sort -u); do
    taskkill //PID "$P" //F 2>/dev/null || kill -9 "$P" 2>/dev/null
  done
done
kill <launcher-pid> 2>/dev/null            # reap the launcher too, if it survived its child
netstat -ano | grep LISTENING | grep -E ":<port1>|:<port2>"    # MUST print nothing
```
If that last line still prints, it is an orphan — kill it before you report the app closed. Never
leave a process the user asked you to close. Never commit `boot-logs/` (transient).

---

## END — after ALL modules

Order is fixed: **standards → regression → promote.** Standards runs before regression because a lint
repair changes the tree, and regression evidence is SHA-bound — running regression first guarantees a
stale artifact.

### 8. Standards gate (once, whole codebase)
```bash
aah run core.build.quality_checks run-project --project-path "$PROJECT_DIR" --subject-branch "$BUILD_BRANCH"
```
Lint + static analysis over **every** package root (`backend/`, `frontend/`, …); evidence lands at
`.aah/build/quality-results/standards-latest.json`. Read `scopes` in it to confirm which packages were
examined.
- **exit 0** → step 9.
- **exit 2 with `status: "fail"`** → `Skill("aah-fix", args: "standards failures: <summary>")`, then
  re-run this step. It converges in at most one extra iteration: the repair commit moves the subject,
  so the fresh run is the current verdict.
- **`status: "no_signal"`** → a check never rendered a verdict, so the codebase is **unchecked, not
  clean**. Do **NOT** route to `aah-fix` — there are no findings to act on and it would invent work.
  Recover with **one bounded retry** of this same step.
  - pass/fail on the retry → normal routing above.
  - `no_signal` again → **STOP**. Report `details.blocking_reason` and the message, naming what the
    user must install or which package root has no lint config. Do not install anything yourself and
    do not re-run further: this is an environment problem, and a module that shipped a package root
    without its `## Lint Config` on disk is a spec violation the user needs to see.

### 9. Regression (once)
Run the full suite once over the whole build:
```bash
aah run core.build.run_regression_suite --project-path "$PROJECT_DIR" --subject-branch "$BUILD_BRANCH"
```
- **Fail** → `Skill("aah-fix", args: "regression failures: <summary>")` re-enters the offending
  module, then re-run **step 8 and this step** (the fix moved the subject both are bound to).
  Do NOT promote on a red suite.
- **Pass** → continue.

### 10. Ports END + completeness gate
```
Agent(port-executor-agent, { node: "build", position: "after" }, run_in_background: false)
```
```bash
aah run core.ports.executor --project-path "$PROJECT_DIR" reconcile --node build
aah run core.gates.validate_port_artifacts --node build --project-path "$PROJECT_DIR"
```
Non-zero = a within-port was missed → STOP and report; do not promote.

### 11. Promote to develop
```bash
aah run core.git_ops.promote_to_develop --project-path "$PROJECT_DIR" --build-branch "$BUILD_BRANCH" --cleanup-branches
```
`promote_to_develop` verifies the regression evidence — attested and bound to the build-branch tip —
and performs the branch transition (never run a raw `git merge`). It does **not** re-check standards:
step 8 enforces that itself via its exit code. On conflict, dispatch `aah-merge-resolver` to
reconcile, then let `promote_to_develop` re-verify. On a non-evidence failure (Git/dirty-tree/system
error), STOP and report — do not retry blindly.

### 12. Final GitHub sync + commit + finish
`promote_to_develop` restores the branch that invoked it. Switch to the configured develop branch
before writing final state:
```bash
DEVELOP_BRANCH=$(aah run core.common.manifest read | uvx --from aah python -c "import sys,json; print(json.load(sys.stdin).get('branching_config', {}).get('develop_branch', 'develop'))")
aah run core.common.git_utils checkout "$DEVELOP_BRANCH" --project-path "$PROJECT_DIR"
```
If version_control enabled, invoke `aah-issue-syncer`: "Final build-complete sync — reconcile drift."
Then transition the phase, commit all final AAH state on develop, and tag that commit:
```bash
aah run core.common.phase_transition end build
cd "$PROJECT_DIR"
git add .aah/manifest.yaml .aah/claude-progress.json .aah/audit/ .aah/build/test-results/ .aah/build/quality-results/ .aah/build/boot-results/ .aah/build/app-services.yaml .aah/version-control/
git commit -m "chore: commit final AAH build state"
git push origin "$DEVELOP_BRANCH"
MANIFEST_JSON=$(aah run core.common.manifest read)
PROJECT_NAME=$(echo "$MANIFEST_JSON" | uvx --from aah python -c "import sys,json; print(json.load(sys.stdin)['project_name'])")
ITER=$(echo "$MANIFEST_JSON" | uvx --from aah python -c "import sys,json; print(json.load(sys.stdin).get('current_iteration', 1))")
git -C "$PROJECT_DIR" tag -a "${PROJECT_NAME}/iter-${ITER}/aah-build" -m "AAH build phase complete (iteration ${ITER})"
git push --tags origin
```
Show where artifacts live (`.aah/build/test-results/`, `.aah/build/quality-results/`,
`.aah/build/boot-results/`, plus any port
artifacts), then `AskUserQuestion`: "Build complete — N/N modules done, promoted to develop. What's
next?" with:
- **Review the running app** — boot it and hand it over for hands-on testing (step 13).
- **Review final state** — stay here and look at code, tests, or artifacts.

**Closing line** — once they are done reviewing (step 13 returns here, or they finish looking at final
state): the phase is over and state is committed, tagged and pushed, so tell them to run `/aah-deploy`
or `/aah-promote-to-main` when ready. Nothing in this skill runs after that.

### 13. Hands-on review — only if the user asks for it
Reached from step 12, with `<i>` = the last module's index. Tell the user the app is starting and to
wait for the URLs. Run **§7A**, leaving the services running, then hand over the URLs with **the
hand-over warning** (7.3c).

Commit what the boot recorded, before handing over:
```bash
git -C "$PROJECT_DIR" add .aah/build/boot-results/ .aah/build/app-services.yaml
if ! git -C "$PROJECT_DIR" diff --cached --quiet; then
  git -C "$PROJECT_DIR" commit -m "chore: hands-on review boot record for module <i>"
  git -C "$PROJECT_DIR" push origin HEAD
fi
```

Then `AskUserQuestion`: "Finished with the app?" with:
- **Done — shut it down** → §7A step 5, then give step 12's closing line.
- **Leave it running** → hand over the PIDs and the ports they hold so the user can stop them
  whenever they finish, then give step 12's closing line. Do NOT re-ask this question.

## Rules — MANDATORY
- NEVER implement features in the main conversation — dispatch `aah-feature-implementer`.
- Build modules **sequentially**, one at a time, in `$PROJECT_DIR` on the current branch.
- **Step 7.3 is the only gate: ask before launching, ask before closing.** Both prompts always fire
  via `AskUserQuestion` — never auto-approve, and never start or stop the app underneath the user.
- Boot runs **per module from module 1 on** (module 0 is scaffolding — unless it is the only module)
  and is **advisory**: report it honestly with the real error, never invent an HTTP code, and let the
  user decide.
- **`.env` belongs to the child process.** Never shell-source it (`source .env`, `. .env`,
  `export $(cat .env)` — blocked by the secrets guard and leaks secrets), never echo an env value, and
  keep one interpreter per command: no nested `dotenv run -- python ...`, no `--python <version>` pin
  (both strand the process on an interpreter without the project's deps).
- The **standards gate then regression** run once at the end, in that order, then promote. Never
  promote on a red suite or an unchecked (`no_signal`) codebase — and never route `no_signal` to
  `aah-fix`; retry the gate once, then stop and report.
- NEVER run a raw `git merge`/`git checkout` that moves a verified subject — use
  `promote_to_develop` and `aah-merge-resolver`.
- Never leave uncommitted AAH state at the end.
