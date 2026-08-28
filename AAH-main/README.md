# Ascend Agentic Harness (AAH)

AAH is a structured AI-assisted software delivery framework and tool built on
Claude Code. It takes an ambiguous problem statement and produces shipped,
tested software by enforcing a disciplined phase sequence, with autonomous
subagents doing the work under deterministic quality gates.

**For detailed install and usage instructions, see the full guide:**
[AAH Documentation](https://dev.agenticexchange.deloitte.com/guides/ascend-agentic-harness-guide.html).  
**Need help?** Install the [AAH-L1-Support](https://agenticexchange.deloitte.com/artifacts/77899930-93e7-4b9d-b5e4-32bc437ef525) skill from Agentic Exchange — it can answer questions about installing and using Ascend Agentic Harness.

---

## Lifecycle

AAH runs on **three phases** — Orchestrate, Build, and Validate (OBV) — based on
Deloitte's AI product delivery methodology. Inside those phases you run nine
skills, each of which invokes the agents, scripts, tools, and context that AAH
has codified behind it:

```
Init -> Discuss -> Access* -> Architecture -> UX* -> Plan -> Build -> Fix -> Deploy
```  

**\* Access and UX are conditional** - AAH runs them only when your project needs them.

| Phase     | What happens | Skills |
|-----------|---|---|
| **Orchestrate** | Capture intent, confirm access, decide architecture, and sequence the work into features. | `/aah-init-project`, `/aah-discuss`, `/aah-access`*, `/aah-arch`, `/aah-ux`*, `/aah-plan` |
| **Build** | Modules are built one at a time in dependency order, behind quality gates, with continuous testing. | `/aah-build`, `/aah-fix` |
| **Validate** | Verify the build against its quality gates, then provision and ship it. Deploy runs inside this phase. | `/aah-deploy` |

---

## Installation

AAH is designed to be used from a terminal or an IDE (for example, VS Code or
PyCharm) for optimal performance. Installers are provided for Windows and Mac
and set up every prerequisite for you.

### Prerequisites

Before running the installer, make sure you have:

- **Administrator access** — required on both Windows and Mac.
- **Claude Code access** — request it through the Deloitte AI Tool Access
  request form. Allow about 3–5 days for the request to be completed. For WBS
  guidance, refer to the "Now live: AI Tool Access!" email from
  `usoplaie@deloitte.com`.

The installer automatically installs everything else it needs: Git, UV,
Python 3.12, Node.js, Claude Code, GitHub CLI, and AWS CLI.

### Download the Harness

1. Go to the Agentic Exchange portal: [https://agenticexchange.deloitte.com](https://agenticexchange.deloitte.com) 
2. Search for "Ascend Agentic Harness".
3. Open the result and download the ZIP archive.
4. Extract the downloaded ZIP — you will find an inner ZIP containing the source
   code.
5. Extract the inner ZIP to reveal the folder structure:

```
ascend-agentic-harness-main/
├── installers/
│   ├── AAH-Dependency-Installer.ps1    (Windows)
│   └── AAH-Dependency-Installer.sh     (Mac)
├── aah/
├── pyproject.toml
└── ...
```

### Windows

1. In File Explorer, open the extracted folder, right-click the `installers`
   folder, and choose **Open in Terminal**.
2. Run the installer:

   ```powershell
   powershell -ExecutionPolicy Bypass -File ".\AAH-Dependency-Installer.ps1"
   ```

   If your execution policy already allows scripts, you can simply run
   `.\AAH-Dependency-Installer.ps1`.
3. The script displays "Attempting to restart as Administrator..." and shows a
   UAC prompt. Click **Yes** — the installer continues in the elevated window.
   You do not need to open PowerShell as Administrator yourself; the script
   handles elevation.

The installer then runs automatically through these steps: install plan and
detection, installed-software check, a single yes/no confirmation, pre-flight
checks, the AAH harness install, and an installation summary.

**Windows troubleshooting**

- *"Running scripts is disabled on this system"* — run with the execution-policy
  bypass shown above.
- *winget blocked by Group Policy* — the installer falls back to direct
  downloads automatically. No action needed.
- *`aah` not found after install* — open a **new** terminal window. The PATH is
  updated for new sessions only.
- *Python version not satisfied* — run `uv python install 3.12`, then re-run the
  installer.

### Mac

1. Grant yourself **administrator privilege** (for example, via the Privileges
   app) before running the installer.
2. In Finder, open the extracted folder, right-click (or Control-click) the
   `installers` folder, and choose **New Terminal at Folder**.
3. Make the script executable and run it:

   ```bash
   chmod +x ./AAH-Dependency-Installer.sh
   ./AAH-Dependency-Installer.sh
   ```

The installer runs automatically through these steps: install plan and
detection, dependency installation via Homebrew (Homebrew itself if missing,
then Git, `uv`, Python 3.12, Node.js, Claude Code, GitHub CLI, and AWS CLI), the
AAH harness install, and verification.

**Mac troubleshooting**

- *`aah` not found after install* — your current shell does not have
  `~/.local/bin` on the PATH yet. First run `source ~/.zshrc`, then `aah status`.
  If `aah` still does not work, add the path manually: open `~/.zshrc`
  (`nano ~/.zshrc`), add `export PATH="$HOME/.local/bin:$PATH"` at the bottom,
  save, then run `source ~/.zshrc`.
- *Python version not satisfied* — run `uv python install 3.12`, then re-run the
  installer.
- *Homebrew not installing (corporate network)* — install it manually:
  `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"`
- *Permission denied on the script* — run `chmod +x ./AAH-Dependency-Installer.sh`.

### First Run

1. Open a new terminal window so environment changes take effect
   (on Mac you can instead run `source ~/.zshrc` or `source ~/.bashrc`).
2. Verify AAH is installed:

   ```bash
   aah status
   ```

3. Start Claude Code once to authenticate:

   ```bash
   claude
   ```

   Claude Code prompts for authentication on first launch — follow the
   on-screen instructions. Authentication is handled by Claude Code itself; the
   installer does not configure it.
4. **Trust the folder.** Starting Claude Code in a new project folder triggers a
   safety check. Choose **"Yes, I trust this folder"** to let Claude Code read,
   edit, and execute files in it. If the project has MCP servers configured,
   Claude Code also asks whether to enable them — approve to enable them.

### GitHub Organization Access

Access to the AAH repository is **not required to install** AAH, but it **is
required to run** the harness (for example, to push work to GitHub). You can
complete the installation above while your request is pending.

1. Confirm you have access to the **Deloitte-US-Consulting** GitHub
   organization. If you do not, request access at
   [https://engineeringplatforms.deloitte.com/github-enterprise](https://engineeringplatforms.deloitte.com/github-enterprise).
2. Access may take a few hours to a day. Once it is granted, log in to GitHub
   with your Deloitte credentials in the format `[Deloitte username]-deloitte`.

### Reinstall / Update

Updating to a newer version is identical to a fresh install:

1. Download the new harness ZIP from Agentic Exchange and extract it.
2. Open a terminal in the `installers` folder.
3. Run the same installation command for your operating system.

The installer detects the existing install, tears it down, installs the new
version, and re-links skills and agents automatically. No manual uninstall is
needed.

### Start Your First Project

The folder you open Claude in becomes the project. AAH scaffolds its `.aah/`
state into that folder and never creates a subfolder.

```bash
mkdir myapp && cd myapp   # or: git clone <your-repo> myapp && cd myapp
claude                    # open Claude Code here
/aah-init-project         # greenfield or brownfield — auto-detected
/aah-discuss              # begin the phase sequence
```

---

## Skills

AAH exposes nine skills, grouped by the three phases of the lifecycle. Each one
runs the agents, scripts, and context codified behind it.

### Orchestrate

| Skill | What it does |
|---|---|
| `/aah-init-project` | Onboards a project into AAH — create a new project from scratch (greenfield) or import an existing codebase (brownfield). |
| `/aah-discuss` | Works through the project's undecided areas in sequence, with mandatory questions and constraint gates, and produces a PRD. |
| `/aah-access` | Validates cloud connectivity and data readiness — probes each AWS service and checks the data behind it. |
| `/aah-arch` | Drafts the system as thin end-to-end vertical slices, carves those slices into modules, and generates architecture documentation. |
| `/aah-ux` | Captures UI requirements as annotated wireframes and low-fidelity mockups that feed back into the architecture documents. |
| `/aah-plan` | Authors one feature per module, orders them by dependency (DAG), and validates through a single Plan Gate. |

### Build

| Skill | What it does |
|---|---|
| `/aah-build` | Dependency-ordered, sequential module development with quality gates and continuous testing. |
| `/aah-fix` | Single entry point for all feedback — bug fixes, enhancements, and reviewer comments — available during and after the build. |

### Validate

| Skill | What it does |
|---|---|
| `/aah-deploy` | Multi-route deployment to AWS with security scanning and tier-based access control. |

**Conditional skills**: `/aah-access` runs only when the project deploys to AWS,
and `/aah-ux` runs only when the project has a frontend. Your answers during
`/aah-discuss` decide which conditional skills apply.
