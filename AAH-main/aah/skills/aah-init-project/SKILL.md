---
name: aah-init-project
argument-hint: "[project-name] [--stack <tech-stack>]"
description: >-
  Onboard any project into the AAH delivery framework. Creates a new AAH project
  from scratch (greenfield) or imports an existing codebase (brownfield) —
  the single entry point for all project initialization and onboarding workflows.
---

# Initialize an AAH Project

**The current working directory (CWD) IS the project.** AAH never creates a
project subfolder — it scaffolds `.aah/` directly into CWD and keeps CWD as the
project root for every later phase. The user is expected to start Claude from
inside the folder they want to be the project.

Two mechanisms only:
- **New project** — empty CWD, scaffold from scratch (greenfield)
- **Import project** — everything else is brownfield (existing code, existing `.aah/`, existing `.rapids/`, iteration 2+, cloned repo)

## MANDATORY: Tool Usage for Questions

**Every time this skill says "Use `AskUserQuestion`" or "ask the user", you MUST invoke the `AskUserQuestion` tool.** Do NOT present questions as plain text.

## Steps

### 1. Inspect the Current Directory — CWD Is the Project Root

Before anything else, determine the state of CWD. List its children, ignoring
`.git` and `.rapids` (AAH-managed) — everything else counts as "content":

```bash
ls -A
```

Three cases decide the flow:

- **CWD is empty** (no content) → **greenfield in place**. Skip the intent
  question; go straight to **Path A**.
- **CWD has content AND the user wants to onboard that existing code** →
  **brownfield in place**. Go to **Path B**.
- **CWD has content BUT the user wants a *brand-new* project** → **STOP.** We
  will not scaffold a new project on top of existing files, and we never create
  a subfolder. Tell the user:

  > This folder already has files in it, and AAH treats the current folder as
  > the project root. To start a **new** project, create an empty folder and
  > run Claude from there:
  > ```bash
  > mkdir my-new-project
  > cd my-new-project
  > claude   # then run /aah-init-project again
  > ```
  > (If you meant to onboard the code that's already here, tell me and I'll
  > import it as a brownfield project instead.)

  Do not proceed until the user is in an empty folder (greenfield) or confirms
  brownfield import.

If CWD state is ambiguous (has content, and you don't yet know the user's
intent), use `AskUserQuestion`:
- **question**: "This folder already has files. What would you like to do?"
- **header**: "Project"
- **multiSelect**: false
- **options**:
  - label: `"Import this existing code"`, description: `"Onboard the code already in this folder into AAH (brownfield)"`
  - label: `"Start a brand-new project"`, description: `"I'll show you how to start fresh in an empty folder"`

Route: "Import this existing code" → **Path B**; "Start a brand-new project" →
show the STOP message above.

If CWD is empty, use `AskUserQuestion` to distinguish greenfield-from-scratch
vs. clone/import-into-this-empty-folder:
- **question**: "This folder is empty. How would you like to start?"
- **header**: "Project"
- **multiSelect**: false
- **options**:
  - label: `"Build a new project"`, description: `"Scaffold a fresh AAH project here from scratch"`
  - label: `"Import existing project"`, description: `"Clone a Git repo / point to code to onboard into this folder"`

Route: "Build a new project" → **Path A**; "Import existing project" → **Path B**.

---

## Path A — New Project (Greenfield, in place)

### A1. Gather Project Name & Stack

Use `AskUserQuestion`:
- **question**: "What is the project name?"
- **header**: "Name"
- **options**:
  - label: `"my-project"`, description: `"Use this placeholder — you can rename later"`
  - label: `"new-project"`, description: `"Use this placeholder — you can rename later"`

The user will likely type their own name via "Other". If arguments already contain a project name, skip this prompt. Project name defaults to the CWD folder name.

Optionally ask for tech stack (aids .gitignore generation):
```
--stack <tech-stack>
```

### A2. Scaffold .aah/ in place (Greenfield)

Scaffold directly into CWD — **no `--target-dir`, no subfolder**:
```bash
aah run core.scaffold.project create $PROJECT_NAME --stack $STACK
```

This will:
- Treat CWD as the project folder
- `git init` in CWD (if not already a repo)
- Create `.aah/` directory structure (manifest.yaml, claude-progress.json, expertise.yaml)
- Write `CLAUDE.md` with the delivery ruleset (project name + stack in the header)
- Generate stack-aware `.gitignore` + `.gitattributes`
- Initial commit → create `develop` branch → checkout `develop`

Capture `PROJECT_DIR` from the JSON output `path` field (it will be CWD).

### A3. Knowledge Base (Optional)

Follow the **Knowledge Base Procedure** (see below). On completion or skip → proceed to Convergence.

---

## Path B — Import Project (Brownfield, in place)

### B1. Get the Code Into CWD

The project must end up **in CWD** — never a subfolder.

- **CWD already contains the code** (has content / `.git`) → nothing to fetch,
  proceed to B2.
- **CWD is empty and the user wants to import** → use `AskUserQuestion`:
  - **question**: "Where is the project you want to import?"
  - **header**: "Location"
  - **multiSelect**: false
  - **options**:
    - label: `"Enter a Git URL"`, description: `"Clone from GitHub/GitLab/Bitbucket into this folder"`
    - label: `"Enter a local path"`, description: `"Point to an existing folder on disk"`

  **Routing:**
  - "Enter a Git URL" → ask for URL, then clone **into CWD** (note the trailing `.`):
    ```bash
    git clone $URL .
    ```
    CWD stays the project root — do not `cd` into a subfolder.
  - "Enter a local path" → the folder at that path is a *different* root. Since
    CWD must be the project root, tell the user to start Claude from that folder
    instead (`cd <path> && claude`, then re-run `/aah-init-project`), OR confirm
    they want its contents — do not scaffold a foreign path as the root.
  - "Other" (free-text) → parse: if it starts with `http`/`https`/`git@` or
    contains `.git`, treat as a Git URL and clone into CWD as above; otherwise
    treat as a local path per the rule above.

Project name defaults to the CWD folder name.

### B2. Scaffold .aah/ (Brownfield, in place)

```bash
aah run core.scaffold.project create $PROJECT_NAME --type brownfield
```

This will:
- Preserve existing repo, branch, history
- If prior `.aah/` exists: archive to `.aah/iterations/{N}/` before re-scaffolding
- If prior `.rapids/` exists: archive/migrate, then scaffold fresh `.aah/`
- Create `.aah/` directory structure (manifest.yaml, claude-progress.json, expertise.yaml)
- Write/update `CLAUDE.md`: append the delivery ruleset (wrapped in `<!-- AAH:BEGIN -->`/`<!-- AAH:END -->` markers) to any existing `CLAUDE.md`, or create it if absent — user content is never clobbered, and a re-scaffold replaces the block idempotently. Stack is read from the manifest.
- Merge-safe `.gitignore` update (never clobbers existing entries)
- Commit only `.aah/` changes on current branch

Capture `PROJECT_DIR` from the JSON output `path` field.

### B3. Knowledge Base (Optional)

Follow the **Knowledge Base Procedure** (see below). On completion or skip → proceed to B4.

### B4. Codebase Intelligence

Invoke the codebase profiler skill:
```
/aah-codebase-profile --mode import
```

This produces 9 intel files in `.aah/codebase-intel/`:
- `codebase-profile.json` (automated scan)
- `codebase-structure.md`, `dependency-map.md`, `tech-stack.md`
- `architecture-diagram.md`, `data-model-diagram.md`, `data-flow-diagram.md`, `dependency-graph.md`, `codebase-learning.md`

The skill handles its own commit of intel artifacts.

---

## Knowledge Base Procedure

Use `AskUserQuestion`:
- **question**: "Do you have supplementary documentation (requirements, architecture docs, API specs, meeting notes) to include?"
- **header**: "Knowledge"
- **options**:
  - label: `"Yes, I have docs to add"`, description: `"I'll create a knowledge folder for you to place them"`
  - label: `"No, skip this step"`, description: `"Proceed without supplementary docs"`

If yes:
```bash
aah run core.knowledge.main init --project-path "$PROJECT_DIR"
```
Guide user to place files in the `knowledge/` folder. Supported file types:
- **Documents:** `.pdf` `.doc` `.docx` `.docm` `.odt` `.rtf`
- **Presentations:** `.ppt` `.pptx` `.pptm` `.odp`
- **Spreadsheets:** `.xls` `.xlsx` `.xlsm` `.ods` `.csv` `.tsv`
- **Images:** `.jpg` `.jpeg` `.png` `.gif` `.bmp` `.tiff` `.webp`
- **Web:** `.html` `.htm`
- **Plain text:** `.md` `.txt` `.rst`
- **Structured text:** `.yaml` `.yml` `.json`


Auto-detects existing `docs/`, `knowledge-base/`, `project-documentation/` folders — no need to move files.

Then use `AskUserQuestion`:
- **question**: "Have you placed your documentation files in the knowledge/ folder?"
- **header**: "Confirm"
- **options**:
  - label: `"Yes, files are ready"`, description: `"Parse and index the documentation now"`
  - label: `"Skip for now"`, description: `"I'll add docs later — proceed without parsing"`

If "Yes, files are ready":
```bash
aah run core.knowledge.main parse --project-path "$PROJECT_DIR"
```

If "No" or "Skip for now": return to caller.

---

## Convergence — Both Paths

### Initialize the Port Registry (MANDATORY)

Before the summary, initialize the ports registry so the ports mechanism is
armed for the whole spine. This MUST run here — the registry must exist before
`/aah-discuss`, because discuss's own `before` bookend reads it to fire any
always-on (`default`) port activities (design §3). See the WIRING NOTE at the
top of `aah/core/ports/executor.py`.

```bash
aah run core.ports.executor --project-path "$PROJECT_DIR" init
```

- **Module/fn:** `aah.core.ports.executor` → `op_init(project_root)`
- **Reads:** the framework catalog `aah/_resources/_references/_ports/_registry.yaml`
- **Writes:** `$PROJECT_DIR/.aah/port-registry.yaml` — materializes every
  `default` port activity with `status: pending` (stubs → `deferred-stub`).
- **Idempotent** — re-running preserves runtime state and adds no duplicates.

Parse the JSON output and re-render it as markdown (Bash output is collapsed —
the user cannot see it otherwise):

```
### ═══ Port Registry Initialized ═══
| Field          | Value                          |
|----------------|--------------------------------|
| Registry       | .aah/port-registry.yaml        |
| Defaults added | <defaults_added from JSON>     |
```

If the JSON contains an `error` (e.g. no ports catalog in an older harness),
report it but do NOT block onboarding — `/aah-discuss` compile self-heals by
calling init lazily.

### Version Control — Track Features as GitHub Issues (Mandatory)

AAH syncs every feature to a GitHub Issue (one feature = one issue), keeping
`feature.md` as the source of truth. This step always runs — do NOT skip it.

1. **Check `gh` is installed and authenticated:**
   ```bash
   gh auth status
   ```

2. **If `gh` is NOT installed or NOT authenticated**, do NOT enable version control yet.
   Show the user these setup steps and stop until they confirm it's done:

   > **GitHub CLI setup required.** Issue sync uses the `gh` CLI so no tokens are stored in the repo.
   > 1. Install `gh` — see https://cli.github.com (macOS: `brew install gh`, Windows: `winget install GitHub.cli`, Linux: see the docs).
   > 2. Authenticate: run `gh auth login` and follow the prompts (choose GitHub.com → HTTPS → login with a browser).
   > 3. Verify: `gh auth status` should report you are logged in.
   >
   > Tell me once `gh auth status` succeeds and I'll enable issue sync.

   After the user confirms, re-run `gh auth status`. Only continue when it succeeds.

3. **Determine the repo** (`owner/name`). Prefer auto-detection from the project's git remote:
   ```bash
   gh repo view --json nameWithOwner -q .nameWithOwner
   ```
   If that fails (no remote / not a GitHub repo), use `AskUserQuestion` to ask for the
   repo slug (header: "Repo", e.g. `owner/name`), or offer to create one.

   **Repo creation policy — organization only, never personal.** AAH creates repos only
   under a GitHub organization. Personal-account repos are not supported here.

   When offering to create a repo, ask the user two `AskUserQuestion`s:
   1. **Organization name** — text input, default `Deloitte-US-Consulting`. The user may
      override with any organization they belong to.
   2. **Visibility** — choose `internal` (visible to all org members) or `private`
      (restricted). No `public` option.

   Then create the repo under the chosen org with the chosen visibility:
   ```bash
   gh repo create "<org>/<name>" --<visibility> --source=. --remote=origin
   ```
   Where `<visibility>` is `internal` or `private`.

   Suggest the name `aah-project-<proj-name>` where `<proj-name>` is derived from the
   project folder name or the project name in manifest.yaml (lowercased, spaces replaced
   with hyphens).

4. **Enable version control in the manifest:**
   ```bash
   aah run core.common.manifest set-version-control --enable --provider github --repo "$REPO" --auth gh-cli --path "$PROJECT_DIR"
   ```

5. **Initialize the sync ledger and validate auth:**
   ```bash
   aah run core.version_control.cli init --project-path "$PROJECT_DIR"
   ```
   This is non-blocking — it creates `.aah/version-control/` state and confirms `gh` auth.
   Feature issues are created later by `/aah-plan` (via `sync --from-plan`), not here.

6. **Provision the AAH labels in the repo:**
   ```bash
   aah run core.version_control.cli labels --project-path "$PROJECT_DIR"
   ```
   This creates every `aah:*` label (phases `discuss/architecture/plan/build/deploy`,
   kinds `feature/feedback`, all statuses, and layers) in the repo. Idempotent — safe
   to re-run. Requires the repo to already exist and `gh` to be authenticated.

### Verify & Display Summary

```bash
aah run core.common.manifest get-status
```

Display:
- Project name and path
- Type: greenfield or brownfield
- Git branches (greenfield: main + develop created; brownfield: existing preserved)
- `.aah/` directory structure
- Issue sync: enabled (provider + repo) or not configured
- `CLAUDE.md` — created (greenfield) or updated with the AAH delivery block (brownfield)
- For brownfield: list codebase-intel files generated

Tell the user: `.aah/` is git-tracked and portable across machines.

**Next step:** `/aah-discuss` — begin problem statement intake.
