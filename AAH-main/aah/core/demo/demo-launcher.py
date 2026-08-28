#!/usr/bin/env python3
"""
RAPIDS Demo Launcher.

Spawns terminal windows/tabs — one per selected phase — for demo presentation.

Modes:
    present   — (default) Each tab runs demo-phase-presenter.py (read-only review)
    worktree  — Each tab runs in a git worktree with state for live execution.
                Onboarding tab launches Claude directly. Other tabs show previous
                phase summary, ask confirmation, then launch Claude.

Usage:
    python3 demo-launcher.py <project-path> <phases> [--mode present|worktree] [--cleanup]

    <phases> is a comma-separated list: research,analysis,plan

Examples:
    python3 demo-launcher.py projects/my-project research,analysis,plan
    python3 demo-launcher.py projects/my-project research,analysis --mode worktree
    python3 demo-launcher.py projects/my-project --cleanup

Dependencies:
    pip install rich  (for the presenter)
"""

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PHASE_ORDER = ["research", "analysis", "plan", "implement", "deploy", "sustain"]
PHASE_LABELS = {
    "research": "R — Research",
    "analysis": "A — Analysis",
    "plan": "P — Plan",
    "implement": "I — Implement",
    "deploy": "D — Deploy",
    "sustain": "S — Sustain",
}
PHASE_TAB_COLORS = {
    "onboarding": "#AAAAAA",
    "research": "#00CED1",
    "analysis": "#DA70D6",
    "plan": "#FFD700",
    "implement": "#32CD32",
    "deploy": "#4169E1",
    "sustain": "#DC143C",
}
PHASE_COMMANDS = {
    "research": "/rapids-research",
    "analysis": "/rapids-analyze",
    "plan": "/rapids-plan",
    "implement": "/rapids-implement",
    "deploy": "/rapids-deploy",
    "sustain": "/rapids-sustain",
}

# Onboarding launches the full workspace initialization sequence
ONBOARDING_PROMPT = (
    "Start the RAPIDS onboarding demo. Run these commands in order: "
    "1) /rapids-init-workspace demo-workspace "
    "2) /rapids-new-project demo-app --stack python-fastapi "
    "3) /rapids-research"
)

# Map each phase to its previous phase (for intro display)
PREVIOUS_PHASE_MAP = {
    "onboarding": None,
    "research": "onboarding",
    "analysis": "research",
    "plan": "analysis",
    "implement": "plan",
    "deploy": "implement",
    "sustain": "deploy",
}


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def detect_platform():
    """Detect the runtime platform.

    Returns one of: 'macos', 'wsl2', 'windows', 'linux', 'unknown'.
    Detection order matters: WSL2 reports sys.platform == 'linux', so it
    must be checked before the generic Linux branch.
    """
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "linux":
        if os.environ.get("WSL_DISTRO_NAME"):
            return "wsl2"
        try:
            with open("/proc/version", "r", encoding="utf-8") as f:
                if "microsoft" in f.read().lower():
                    return "wsl2"
        except (FileNotFoundError, PermissionError):
            pass
        return "linux"
    return "unknown"


# ---------------------------------------------------------------------------
# Script discovery
# ---------------------------------------------------------------------------

def find_presenter_script():
    """Locate demo-phase-presenter.py relative to this script."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    presenter = os.path.join(script_dir, "demo-phase-presenter.py")
    if os.path.isfile(presenter):
        return presenter
    project_root = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_root:
        presenter = os.path.join(project_root, "scripts", "demo-phase-presenter.py")
        if os.path.isfile(presenter):
            return presenter
    print("Error: Could not find demo-phase-presenter.py")
    sys.exit(1)


def find_worktree_setup_script():
    """Locate demo-worktree-setup.py relative to this script."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    setup = os.path.join(script_dir, "demo-worktree-setup.py")
    if os.path.isfile(setup):
        return setup
    project_root = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if project_root:
        setup = os.path.join(project_root, "scripts", "demo-worktree-setup.py")
        if os.path.isfile(setup):
            return setup
    print("Error: Could not find demo-worktree-setup.py")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Worktree setup
# ---------------------------------------------------------------------------

def _setup_one_worktree(phase, project_abs, setup_script, python_path, tag):
    """Set up a single worktree or onboarding directory. Returns (phase, wt_path, error)."""
    if phase == "onboarding":
        cmd = [python_path, setup_script, project_abs, phase]
    else:
        cmd = [python_path, setup_script, project_abs, phase, "--tag", tag]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=project_abs)
    if result.returncode != 0:
        return phase, None, result.stderr.strip()
    wt_path = result.stdout.strip().split("\n")[-1].strip()
    return phase, wt_path, None


def setup_worktrees(project_path, phases, tag_map=None):
    """Create worktrees for onboarding + each selected phase in parallel.

    Returns dict mapping phase name to worktree path.
    """
    setup_script = find_worktree_setup_script()
    python_path = sys.executable
    project_abs = os.path.abspath(project_path)
    tag_map = tag_map or {}

    all_phases = ["onboarding"] + phases

    # Validate tags upfront before spawning threads
    for phase in phases:
        if not tag_map.get(phase):
            print(f"Error: No tag provided for phase '{phase}'. Tag-based worktrees are required.")
            sys.exit(1)

    worktree_paths = {}
    errors = []

    with ThreadPoolExecutor(max_workers=min(len(all_phases), 4)) as pool:
        futures = {
            pool.submit(_setup_one_worktree, phase, project_abs, setup_script,
                        python_path, tag_map.get(phase)): phase
            for phase in all_phases
        }
        for future in as_completed(futures):
            phase, wt_path, err = future.result()
            if err:
                errors.append(f"Error setting up worktree for {phase}: {err}")
            else:
                worktree_paths[phase] = wt_path
                label = "onboarding directory" if phase == "onboarding" else f"{phase} worktree"
                print(f"  -> {label}: {wt_path}")

    if errors:
        for e in errors:
            print(e)
        sys.exit(1)

    return worktree_paths


# ---------------------------------------------------------------------------
# Wrapper script generation (worktree mode)
# ---------------------------------------------------------------------------

def write_worktree_wrapper(phase, worktree_path, project_path, presenter_script, project_name):
    """Write a shell wrapper script for a worktree-mode terminal tab.

    For onboarding: launches Claude directly with /rapids-research auto-prompt.
    For other phases: shows previous phase intro, asks confirmation, then
    launches Claude with the phase-specific command.

    Sources the user's shell profile so that claude is in PATH and auth
    tokens are accessible.

    Returns the script path.
    """
    script_path = os.path.join(tempfile.gettempdir(), f"rapids-demo-{phase}.sh")
    project_abs = os.path.abspath(project_path)
    previous_phase = PREVIOUS_PHASE_MAP.get(phase)
    python_path = os.path.abspath(sys.executable)

    phase_title = "Onboarding" if phase == "onboarding" else phase.title()

    with open(script_path, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n")
        # Source user shell profiles so that `claude` and other user-installed
        # tools are on PATH. On macOS the default shell is zsh but we run bash,
        # so we must source both profiles explicitly.
        f.write('[ -f "$HOME/.bash_profile" ] && source "$HOME/.bash_profile"\n')
        f.write('[ -f "$HOME/.bashrc" ] && source "$HOME/.bashrc"\n')
        f.write('[ -f "$HOME/.zshrc" ] && source "$HOME/.zshrc" 2>/dev/null || true\n')
        f.write(f"cd {shlex.quote(worktree_path)}\n")
        # Force Claude to treat this worktree as the project root so that
        # list-projects.py finds the worktree's .rapids/ instead of walking
        # up to the outer repo.
        f.write(f"export CLAUDE_PROJECT_DIR={shlex.quote(worktree_path)}\n")
        f.write("clear\n")

        if phase == "onboarding":
            # Onboarding: show RAPIDS banner via presenter, pause, then launch Claude
            f.write(f"{shlex.quote(python_path)} {shlex.quote(presenter_script)}"
                    f" {shlex.quote(project_abs)} onboarding --mode intro\n")
            f.write('echo ""\n')
            f.write('read -p "Press Enter to launch Claude..." _rapids_pause\n')
            f.write(f'claude -- "{ONBOARDING_PROMPT}"\n')
        else:
            # Show previous phase intro (always, for every non-onboarding phase)
            if previous_phase:
                if previous_phase == "onboarding":
                    # First real phase — show RAPIDS banner + current phase objective
                    f.write(f"{shlex.quote(python_path)} {shlex.quote(presenter_script)}"
                            f" {shlex.quote(project_abs)} onboarding --mode intro --next-phase {phase}"
                            f" --worktree-path {shlex.quote(worktree_path)}\n")
                else:
                    f.write(f"{shlex.quote(python_path)} {shlex.quote(presenter_script)}"
                            f" {shlex.quote(project_abs)} {previous_phase} --mode intro"
                            f" --worktree-path {shlex.quote(worktree_path)}\n")
                f.write('echo ""\n')

            # Confirmation prompt
            f.write(f'read -p "Do you want to execute the {phase_title} phase? (y/n): " confirm\n')
            f.write('if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then\n')
            # Launch Claude with the phase-specific command
            phase_cmd = PHASE_COMMANDS.get(phase, f"/rapids-{phase}")
            f.write(f'    claude -- "{phase_cmd}"\n')
            f.write('else\n')
            f.write('    echo "Skipped."\n')
            f.write('fi\n')

        f.write("exec bash\n")

    os.chmod(script_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return script_path


# ---------------------------------------------------------------------------
# Present-mode wrapper (original behavior)
# ---------------------------------------------------------------------------

def _write_present_wrapper(python_path, presenter_script, project_abs, phase):
    """Write a shell wrapper script for present-mode (original behavior).

    Returns the script path.
    """
    script_path = os.path.join(tempfile.gettempdir(), f"rapids-demo-{phase}.sh")
    python_abs = os.path.abspath(python_path)
    with open(script_path, "w", encoding="utf-8") as f:
        f.write("#!/bin/bash\n")
        f.write('[ -f "$HOME/.bash_profile" ] && source "$HOME/.bash_profile"\n')
        f.write('[ -f "$HOME/.bashrc" ] && source "$HOME/.bashrc"\n')
        f.write('[ -f "$HOME/.zshrc" ] && source "$HOME/.zshrc" 2>/dev/null || true\n')
        f.write(f"{shlex.quote(python_abs)} {shlex.quote(presenter_script)}"
                f" {shlex.quote(project_abs)} {phase}\n")
        f.write("exec bash\n")
    os.chmod(script_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    return script_path


# ---------------------------------------------------------------------------
# Launch backends — Present mode (original)
# ---------------------------------------------------------------------------

def _macos_has_iterm2():
    """Return True if iTerm2 is installed on this Mac."""
    result = subprocess.run(
        ["osascript", "-e",
         'tell application "System Events" to return (name of processes) contains "iTerm2"'],
        capture_output=True, text=True
    )
    return result.stdout.strip() == "true"


def _launch_macos_tabs(tab_specs):
    """Open each demo phase as a tab in one terminal window using AppleScript.

    tab_specs: list of (title, script_path) tuples.
    Prefers iTerm2 if running; falls back to Terminal.app.
    Each script_path must be an absolute, executable bash script.
    """
    if _macos_has_iterm2():
        # iTerm2 AppleScript: one window, one tab per phase.
        # First tab uses the new window's current session.
        # Subsequent tabs create a new tab, then write to its current session.
        tab_blocks = []
        for i, (title, script) in enumerate(tab_specs):
            esc_script = script.replace("\\", "\\\\").replace('"', '\\"')
            esc_title = title.replace("\\", "\\\\").replace('"', '\\"')
            if i == 0:
                tab_blocks.append(
                    f'    tell current session of current window\n'
                    f'        set name to "{esc_title}"\n'
                    f'        write text "bash {esc_script}"\n'
                    f'    end tell'
                )
            else:
                tab_blocks.append(
                    f'    tell current window\n'
                    f'        tell (create tab with default profile)\n'
                    f'            tell current session\n'
                    f'                set name to "{esc_title}"\n'
                    f'                write text "bash {esc_script}"\n'
                    f'            end tell\n'
                    f'        end tell\n'
                    f'    end tell'
                )
        tabs_as = "\n".join(tab_blocks)
        applescript = f'tell application "iTerm2"\n    activate\n    create window with default profile\n{tabs_as}\nend tell'
    else:
        # Terminal.app AppleScript:
        # - First tab: `do script` returns a tab reference; set its custom title.
        # - Subsequent tabs: `do script ... in (make new tab)` in the same window;
        #   set the custom title on the returned tab reference.
        # Using a variable per tab avoids any timing/focus race conditions.
        lines = ['tell application "Terminal"', "    activate"]
        for i, (title, script) in enumerate(tab_specs):
            esc_script = script.replace("\\", "\\\\").replace('"', '\\"')
            esc_title = title.replace("\\", "\\\\").replace('"', '\\"')
            var = f"t{i}"
            if i == 0:
                lines.append(f'    set {var} to do script "bash {esc_script}"')
            else:
                lines.append(
                    f'    set {var} to do script "bash {esc_script}" in (make new tab at end of tabs of front window)'
                )
            lines.append(f'    set custom title of {var} to "{esc_title}"')
        lines.append("end tell")
        applescript = "\n".join(lines)

    subprocess.run(["osascript", "-e", applescript], check=False)


def launch_macos_present(project_path, phases, presenter_script):
    """Launch macOS Terminal.app tabs in present mode."""
    python_path = os.path.abspath(sys.executable)
    project_abs = os.path.abspath(project_path)

    tab_specs = []
    for phase in phases:
        script_path = os.path.join(tempfile.gettempdir(), f"rapids-demo-{phase}.sh")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/bash\n")
            f.write("clear\n")
            f.write(f"{shlex.quote(python_path)} {shlex.quote(presenter_script)} {shlex.quote(project_abs)} {phase}\n")
            f.write("exec bash\n")
        os.chmod(script_path, 0o755)
        tab_specs.append((PHASE_LABELS.get(phase, phase.title()), script_path))

    _launch_macos_tabs(tab_specs)
    print(f"Opened {len(phases)} tab(s) for: {', '.join(PHASE_LABELS.get(p, p) for p in phases)}")
    print("Switch between tabs to walk through each phase.")


def launch_wsl2_present(project_path, phases, presenter_script):
    """Launch Windows Terminal tabs from WSL2 in present mode."""
    wt_exe = shutil.which("wt.exe")
    if not wt_exe:
        print("Warning: Windows Terminal (wt.exe) not found in PATH.")
        print("Falling back to sequential mode.\n")
        launch_sequential_present(project_path, phases, presenter_script)
        return

    wsl_exe = "wsl.exe"
    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
    python_path = sys.executable
    project_abs = os.path.abspath(project_path)

    wrapper_scripts = []
    for phase in phases:
        wrapper_scripts.append(
            _write_present_wrapper(python_path, presenter_script, project_abs, phase)
        )

    cmd = [wt_exe, "-w", "new"]
    for i, phase in enumerate(phases):
        if i > 0:
            cmd.append(";")
        tab_color = PHASE_TAB_COLORS.get(phase, "#FFFFFF")
        tab_title = PHASE_LABELS.get(phase, phase.title())
        cmd.extend([
            "new-tab",
            "--title", tab_title,
            "--tabColor", tab_color,
            "--",
            wsl_exe, "-d", distro, "--", "bash", "--login", "-i", "-c", f"source {wrapper_scripts[i]}",
        ])

    try:
        subprocess.run(cmd, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        print("Warning: wt.exe timed out.")
    except OSError as e:
        print(f"Warning: Failed to launch wt.exe: {e}")
        launch_sequential_present(project_path, phases, presenter_script)
        return

    print(f"Opened {len(phases)} tab(s) for: {', '.join(PHASE_LABELS.get(p, p) for p in phases)}")
    print("Switch between tabs to walk through each phase.")


def launch_windows_present(project_path, phases, presenter_script):
    """Launch Windows Terminal tabs from native Windows in present mode."""
    wt_exe = shutil.which("wt.exe")
    if not wt_exe:
        launch_sequential_present(project_path, phases, presenter_script)
        return

    python_path = sys.executable
    project_abs = os.path.abspath(project_path)
    cmd = [wt_exe, "-w", "new"]

    for i, phase in enumerate(phases):
        if i > 0:
            cmd.append(";")
        tab_color = PHASE_TAB_COLORS.get(phase, "#FFFFFF")
        tab_title = PHASE_LABELS.get(phase, phase.title())
        cmd.extend([
            "new-tab",
            "--title", tab_title,
            "--tabColor", tab_color,
            "--",
            "cmd", "/k",
            f'"{python_path}" "{presenter_script}" "{project_abs}" {phase}',
        ])

    try:
        subprocess.run(cmd, check=False, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        launch_sequential_present(project_path, phases, presenter_script)
        return

    print(f"Opened {len(phases)} tab(s) for: {', '.join(PHASE_LABELS.get(p, p) for p in phases)}")


def _find_linux_terminal():
    """Find the first available Linux terminal emulator."""
    terminals = [
        ("gnome-terminal", True),
        ("konsole", False),
        ("xfce4-terminal", True),
        ("x-terminal-emulator", False),
        ("xterm", False),
    ]
    for name, multi_tab in terminals:
        if shutil.which(name):
            return name, multi_tab
    return None, False


def launch_linux_present(project_path, phases, presenter_script):
    """Launch Linux terminal emulator in present mode."""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        launch_sequential_present(project_path, phases, presenter_script)
        return

    term_name, multi_tab = _find_linux_terminal()
    if not term_name:
        launch_sequential_present(project_path, phases, presenter_script)
        return

    python_path = sys.executable
    project_abs = os.path.abspath(project_path)

    def _bash_cmd(phase):
        return (
            f"{shlex.quote(python_path)} {shlex.quote(presenter_script)}"
            f" {shlex.quote(project_abs)} {phase} ; exec bash"
        )

    try:
        if term_name == "gnome-terminal" and multi_tab:
            cmd = ["gnome-terminal"]
            for phase in phases:
                title = PHASE_LABELS.get(phase, phase.title())
                cmd.extend(["--tab", "--title", title, "--", "bash", "-c", _bash_cmd(phase)])
            subprocess.run(cmd, check=False, timeout=30)
        elif term_name == "xfce4-terminal" and multi_tab:
            cmd = ["xfce4-terminal"]
            for phase in phases:
                title = PHASE_LABELS.get(phase, phase.title())
                cmd.extend(["--tab", "--title", title, "-e", f"bash -c {shlex.quote(_bash_cmd(phase))}"])
            subprocess.run(cmd, check=False, timeout=30)
        else:
            for i, phase in enumerate(phases):
                title = PHASE_LABELS.get(phase, phase.title())
                if term_name == "konsole":
                    cmd = ["konsole", "--new-tab", "-p", f"tabtitle={title}", "-e", "bash", "-c", _bash_cmd(phase)]
                elif term_name == "xterm":
                    cmd = ["xterm", "-T", title, "-e", "bash", "-c", _bash_cmd(phase)]
                else:
                    cmd = [term_name, "-e", "bash", "-c", _bash_cmd(phase)]
                subprocess.Popen(cmd)
                if i < len(phases) - 1:
                    time.sleep(1)
    except (OSError, subprocess.TimeoutExpired):
        launch_sequential_present(project_path, phases, presenter_script)
        return

    print(f"Opened {len(phases)} tab(s) via {term_name}")


def launch_sequential_present(project_path, phases, presenter_script):
    """Fallback: run each phase presenter sequentially."""
    python_path = sys.executable
    project_abs = os.path.abspath(project_path)

    print(f"Running {len(phases)} phase(s) sequentially in this terminal.\n")

    for i, phase in enumerate(phases):
        phase_label = PHASE_LABELS.get(phase, phase.title())
        print(f"{'=' * 60}")
        print(f"  Phase {i + 1}/{len(phases)}: {phase_label}")
        print(f"{'=' * 60}\n")
        subprocess.run([python_path, presenter_script, project_abs, phase], check=False)
        if i < len(phases) - 1:
            next_label = PHASE_LABELS.get(phases[i + 1], phases[i + 1])
            print(f"\n--- Press Enter for next phase ({next_label}) ---")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                break

    print(f"\nDemo complete. Showed {len(phases)} phase(s).")


# ---------------------------------------------------------------------------
# Launch backends — Worktree mode
# ---------------------------------------------------------------------------

def launch_wsl2_worktree(project_path, phases, worktree_paths, presenter_script, project_name):
    """Launch Windows Terminal tabs from WSL2 in worktree mode."""
    wt_exe = shutil.which("wt.exe")
    if not wt_exe:
        print("Warning: Windows Terminal (wt.exe) not found.")
        launch_sequential_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
        return

    wsl_exe = "wsl.exe"
    distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")

    all_phases = ["onboarding"] + phases
    wrapper_scripts = []
    for phase in all_phases:
        wt_path = worktree_paths[phase]
        wrapper_scripts.append(
            write_worktree_wrapper(phase, wt_path, project_path, presenter_script, project_name)
        )

    cmd = [wt_exe, "-w", "new"]
    for i, phase in enumerate(all_phases):
        if i > 0:
            cmd.append(";")
        tab_color = PHASE_TAB_COLORS.get(phase, "#FFFFFF")
        tab_title = f"{'Onboarding' if phase == 'onboarding' else PHASE_LABELS.get(phase, phase.title())} (Demo)"
        cmd.extend([
            "new-tab",
            "--title", tab_title,
            "--tabColor", tab_color,
            "--",
            wsl_exe, "-d", distro, "--", "bash", "--login", "-i", "-c", f"source {wrapper_scripts[i]}",
        ])

    try:
        subprocess.run(cmd, check=False, timeout=30)
    except subprocess.TimeoutExpired:
        print("Warning: wt.exe timed out.")
    except OSError as e:
        print(f"Warning: Failed to launch wt.exe: {e}")
        launch_sequential_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
        return

    tab_names = ["Onboarding (Demo)"] + [f"{PHASE_LABELS.get(p, p)} (Demo)" for p in phases]
    print(f"Opened {len(all_phases)} tab(s): {', '.join(tab_names)}")
    print("Walk through tabs left-to-right to demo each phase.")


def launch_macos_worktree(project_path, phases, worktree_paths, presenter_script, project_name):
    """Launch macOS Terminal.app tabs in worktree mode."""
    all_phases = ["onboarding"] + phases

    tab_specs = []
    for phase in all_phases:
        wt_path = worktree_paths[phase]
        wrapper = write_worktree_wrapper(phase, wt_path, project_path, presenter_script, project_name)
        title = "Onboarding (Demo)" if phase == "onboarding" else f"{PHASE_LABELS.get(phase, phase.title())} (Demo)"
        tab_specs.append((title, wrapper))

    _launch_macos_tabs(tab_specs)
    tab_names = ["Onboarding (Demo)"] + [f"{PHASE_LABELS.get(p, p)} (Demo)" for p in phases]
    print(f"Opened {len(all_phases)} tab(s): {', '.join(tab_names)}")


def _git_bash_discovery_bat():
    """Return batch script lines that discover git-bash at runtime on any Windows PC.

    Resolution order (mirrors Claude Code's own lookup):
    1. CLAUDE_CODE_GIT_BASH_PATH already set in the environment (user override)
    2. Common system-wide Git installs  (C:\\Program Files\\Git, C:\\Git)
    3. User-local install via %LOCALAPPDATA%\\Programs\\Git  (winget default)
    4. Derive from git.exe location found on PATH via `where git`

    The result is stored in %CLAUDE_CODE_GIT_BASH_PATH% for the rest of the
    script — nothing is hard-coded, so the .bat runs correctly on any machine.
    """
    return (
        ":: Discover git-bash — resolved at runtime, not hard-coded\n"
        "if not \"%CLAUDE_CODE_GIT_BASH_PATH%\"==\"\" goto :git_bash_found\n"
        "if exist \"C:\\Program Files\\Git\\bin\\bash.exe\" (\n"
        "    set \"CLAUDE_CODE_GIT_BASH_PATH=C:\\Program Files\\Git\\bin\\bash.exe\"\n"
        "    goto :git_bash_found\n"
        ")\n"
        "if exist \"C:\\Program Files (x86)\\Git\\bin\\bash.exe\" (\n"
        "    set \"CLAUDE_CODE_GIT_BASH_PATH=C:\\Program Files (x86)\\Git\\bin\\bash.exe\"\n"
        "    goto :git_bash_found\n"
        ")\n"
        "if exist \"C:\\Git\\bin\\bash.exe\" (\n"
        "    set \"CLAUDE_CODE_GIT_BASH_PATH=C:\\Git\\bin\\bash.exe\"\n"
        "    goto :git_bash_found\n"
        ")\n"
        "if exist \"%LOCALAPPDATA%\\Programs\\Git\\bin\\bash.exe\" (\n"
        "    set \"CLAUDE_CODE_GIT_BASH_PATH=%LOCALAPPDATA%\\Programs\\Git\\bin\\bash.exe\"\n"
        "    goto :git_bash_found\n"
        ")\n"
        ":: Fall back: derive bash.exe from wherever git.exe lives on PATH\n"
        "for /f \"delims=\" %%G in ('where git 2^>nul') do (\n"
        "    set \"_GIT_EXE=%%G\"\n"
        "    goto :git_found_on_path\n"
        ")\n"
        "echo WARNING: git not found on PATH. Claude Code requires git-bash.\n"
        "echo Install Git for Windows: https://git-scm.com/downloads/win\n"
        "goto :git_bash_found\n"
        ":git_found_on_path\n"
        "for %%G in (\"%_GIT_EXE%\") do set \"_GIT_DIR=%%~dpG\"\n"
        "if exist \"%_GIT_DIR%..\\bin\\bash.exe\" (\n"
        "    set \"CLAUDE_CODE_GIT_BASH_PATH=%_GIT_DIR%..\\bin\\bash.exe\"\n"
        "    goto :git_bash_found\n"
        ")\n"
        "if exist \"%_GIT_DIR%bash.exe\" (\n"
        "    set \"CLAUDE_CODE_GIT_BASH_PATH=%_GIT_DIR%bash.exe\"\n"
        "    goto :git_bash_found\n"
        ")\n"
        ":git_bash_found\n"
    )


def launch_windows_worktree(project_path, phases, worktree_paths, presenter_script, project_name):
    """Launch Windows Terminal tabs in worktree mode (native Windows)."""
    wt_exe = shutil.which("wt.exe")
    if not wt_exe:
        launch_sequential_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
        return

    all_phases = ["onboarding"] + phases
    cmd = [wt_exe, "-w", "new"]

    for i, phase in enumerate(all_phases):
        if i > 0:
            cmd.append(";")
        wt_path = worktree_paths[phase]
        tab_color = PHASE_TAB_COLORS.get(phase, "#FFFFFF")
        tab_title = f"{'Onboarding' if phase == 'onboarding' else PHASE_LABELS.get(phase, phase.title())} (Demo)"
        phase_title = "Onboarding" if phase == "onboarding" else phase.title()
        previous_phase = PREVIOUS_PHASE_MAP.get(phase)

        if phase == "onboarding":
            # Onboarding: show RAPIDS banner via presenter, pause, then launch Claude
            batch_script = os.path.join(wt_path, "_demo_onboarding.bat")
            project_abs = os.path.abspath(project_path)
            with open(batch_script, "w", encoding="utf-8") as bf:
                bf.write("@echo off\n")
                bf.write(_git_bash_discovery_bat())
                bf.write(f'cd /d "{wt_path}"\n')
                bf.write(f'"{sys.executable}" "{find_presenter_script()}" "{project_abs}" onboarding --mode intro\n')
                bf.write("echo.\n")
                bf.write('pause\n')
                bf.write(f'claude -- "{ONBOARDING_PROMPT}"\n')
            inner_cmd = f'"{batch_script}"'
        else:
            # Create a batch script for proper interactive prompt handling
            batch_script = os.path.join(wt_path, f"_demo_{phase}.bat")
            phase_cmd = PHASE_COMMANDS.get(phase, f"/rapids-{phase}")
            project_abs = os.path.abspath(project_path)
            with open(batch_script, "w", encoding="utf-8") as bf:
                bf.write("@echo off\n")
                bf.write(_git_bash_discovery_bat())
                bf.write(f'cd /d "{wt_path}"\n')
                if previous_phase:
                    if previous_phase == "onboarding":
                        bf.write(f'"{sys.executable}" "{find_presenter_script()}" "{project_abs}" onboarding --mode intro --next-phase {phase} --worktree-path "{wt_path}"\n')
                    else:
                        bf.write(f'"{sys.executable}" "{find_presenter_script()}" "{project_abs}" {previous_phase} --mode intro --worktree-path "{wt_path}"\n')
                    bf.write("echo.\n")
                bf.write(f'set /p confirm="Do you want to execute the {phase_title} phase? (y/n): "\n')
                bf.write('if /i "%confirm%"=="y" (\n')
                bf.write(f'    claude -- "{phase_cmd}"\n')
                bf.write(') else (\n')
                bf.write('    echo Skipped.\n')
                bf.write(')\n')

            inner_cmd = f'"{batch_script}"'

        cmd.extend([
            "new-tab", "--title", tab_title, "--tabColor", tab_color,
            "--", "cmd", "/k", inner_cmd,
        ])

    try:
        subprocess.run(cmd, check=False, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        launch_sequential_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
        return

    print(f"Opened {len(all_phases)} tab(s)")


def launch_linux_worktree(project_path, phases, worktree_paths, presenter_script, project_name):
    """Launch Linux terminal emulator in worktree mode."""
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        launch_sequential_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
        return

    term_name, multi_tab = _find_linux_terminal()
    if not term_name:
        launch_sequential_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
        return

    all_phases = ["onboarding"] + phases

    try:
        if term_name == "gnome-terminal" and multi_tab:
            cmd = ["gnome-terminal"]
            for phase in all_phases:
                wt_path = worktree_paths[phase]
                wrapper = write_worktree_wrapper(phase, wt_path, project_path, presenter_script, project_name)
                title = f"{'Onboarding' if phase == 'onboarding' else PHASE_LABELS.get(phase, phase.title())} (Demo)"
                cmd.extend(["--tab", "--title", title, "--", "bash", wrapper])
            subprocess.run(cmd, check=False, timeout=30)
        else:
            for i, phase in enumerate(all_phases):
                wt_path = worktree_paths[phase]
                wrapper = write_worktree_wrapper(phase, wt_path, project_path, presenter_script, project_name)
                title = f"{'Onboarding' if phase == 'onboarding' else PHASE_LABELS.get(phase, phase.title())} (Demo)"
                if term_name == "konsole":
                    cmd = ["konsole", "--new-tab", "-p", f"tabtitle={title}", "-e", "bash", wrapper]
                elif term_name == "xterm":
                    cmd = ["xterm", "-T", title, "-e", "bash", wrapper]
                else:
                    cmd = [term_name, "-e", "bash", wrapper]
                subprocess.Popen(cmd)
                if i < len(all_phases) - 1:
                    time.sleep(1)
    except (OSError, subprocess.TimeoutExpired):
        launch_sequential_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
        return

    print(f"Opened {len(all_phases)} tab(s) via {term_name}")


def launch_sequential_worktree(project_path, phases, worktree_paths, presenter_script, project_name):
    """Fallback: run each phase sequentially in worktree mode."""
    python_path = sys.executable
    project_abs = os.path.abspath(project_path)
    all_phases = ["onboarding"] + phases

    print(f"Running {len(all_phases)} phase(s) sequentially (no GUI terminal detected).\n")

    for i, phase in enumerate(all_phases):
        wt_path = worktree_paths[phase]
        phase_title = "Onboarding" if phase == "onboarding" else phase.title()
        previous_phase = PREVIOUS_PHASE_MAP.get(phase)

        print(f"\n{'=' * 60}")
        print(f"  Phase {i + 1}/{len(all_phases)}: {phase_title} (Demo)")
        print(f"  Worktree: {wt_path}")
        print(f"{'=' * 60}\n")

        if phase == "onboarding":
            subprocess.run(
                [python_path, presenter_script, project_abs, "onboarding", "--mode", "intro",
                 "--worktree-path", wt_path],
                check=False
            )
            print()
        elif previous_phase:
            if previous_phase == "onboarding":
                subprocess.run(
                    [python_path, presenter_script, project_abs, "onboarding", "--mode", "intro",
                     "--next-phase", phase, "--worktree-path", wt_path],
                    check=False
                )
            else:
                subprocess.run(
                    [python_path, presenter_script, project_abs, previous_phase, "--mode", "intro",
                     "--worktree-path", wt_path],
                    check=False
                )
            print()

        if phase == "onboarding":
            print(f"Onboarding worktree ready at: {wt_path}")
            print(f'Run: cd {wt_path} && claude -- "{ONBOARDING_PROMPT}"')
        else:
            phase_cmd = PHASE_COMMANDS.get(phase, f"/rapids-{phase}")
            print(f"Worktree ready at: {wt_path}")
            print(f'Run: cd {wt_path} && claude -- "{phase_cmd}"')

        if i < len(all_phases) - 1:
            print(f"\n--- Press Enter for next phase ---")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                break

    print(f"\nDemo setup complete. {len(all_phases)} worktree(s) ready.")


# ---------------------------------------------------------------------------
# Dispatchers
# ---------------------------------------------------------------------------

PLATFORM_NAMES = {
    "macos": "macOS (Terminal.app)",
    "wsl2": "WSL2 (Windows Terminal)",
    "windows": "Windows (Windows Terminal)",
    "linux": "Linux",
    "unknown": "Unknown",
}


def launch_present_mode(project_path, phases, presenter_script):
    """Launch demo in present mode (original read-only behavior)."""
    platform = detect_platform()
    print(f"Platform detected: {PLATFORM_NAMES.get(platform, platform)}")

    if platform == "macos":
        launch_macos_present(project_path, phases, presenter_script)
    elif platform == "wsl2":
        launch_wsl2_present(project_path, phases, presenter_script)
    elif platform == "windows":
        launch_windows_present(project_path, phases, presenter_script)
    elif platform == "linux":
        launch_linux_present(project_path, phases, presenter_script)
    else:
        launch_sequential_present(project_path, phases, presenter_script)


def launch_worktree_mode(project_path, phases, presenter_script, tag_map=None):
    """Launch demo in worktree mode (live execution in isolated branches)."""
    platform = detect_platform()
    project_name = os.path.basename(os.path.abspath(project_path))
    print(f"Platform detected: {PLATFORM_NAMES.get(platform, platform)}")
    mode_detail = "worktree (tag-based)" if tag_map else "worktree"
    print(f"Mode: {mode_detail}\n")

    # Set up worktrees
    worktree_paths = setup_worktrees(project_path, phases, tag_map=tag_map)

    print(f"\nLaunching {len(phases) + 1} terminal(s)...\n")

    if platform == "macos":
        launch_macos_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
    elif platform == "wsl2":
        launch_wsl2_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
    elif platform == "windows":
        launch_windows_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
    elif platform == "linux":
        launch_linux_worktree(project_path, phases, worktree_paths, presenter_script, project_name)
    else:
        launch_sequential_worktree(project_path, phases, worktree_paths, presenter_script, project_name)


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def run_cleanup(project_path):
    """Run demo worktree cleanup."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cleanup_script = os.path.join(script_dir, "demo-worktree-cleanup.py")
    if not os.path.isfile(cleanup_script):
        print("Error: Could not find demo-worktree-cleanup.py")
        sys.exit(1)

    project_abs = os.path.abspath(project_path)
    project_name = os.path.basename(project_abs)
    subprocess.run(
        [sys.executable, cleanup_script, project_abs, project_name],
        check=False, cwd=project_abs
    )


# ---------------------------------------------------------------------------
# Argument parsing and main
# ---------------------------------------------------------------------------

def parse_args():
    """Parse command-line arguments."""
    args = sys.argv[1:]
    mode = "present"
    cleanup = False
    tag_map = None
    positional = []

    i = 0
    while i < len(args):
        if args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1].lower()
            i += 2
        elif args[i] == "--cleanup":
            cleanup = True
            i += 1
        elif args[i] == "--tag-map" and i + 1 < len(args):
            # JSON mapping phase name to git tag
            try:
                tag_map = json.loads(args[i + 1])
            except json.JSONDecodeError:
                print(f"Error: --tag-map value must be valid JSON: {args[i + 1]}")
                sys.exit(1)
            i += 2
        else:
            positional.append(args[i])
            i += 1

    return positional, mode, cleanup, tag_map


def main():
    positional, mode, cleanup, tag_map = parse_args()

    if cleanup:
        if positional:
            run_cleanup(positional[0])
        else:
            print("Usage: demo-launcher.py <project-path> --cleanup")
            sys.exit(1)
        return

    if len(positional) < 2:
        print("Usage: demo-launcher.py <project-path> <phases> [--mode present|worktree] [--cleanup] [--tag-map JSON]")
        print()
        print("  <phases>       Comma-separated: research,analysis,plan")
        print("  --mode         present (read-only review) or worktree (live execution)")
        print("  --cleanup      Remove demo worktrees for this project")
        print('  --tag-map      JSON mapping phase to git tag (e.g. \'{"research":"rapids/proj/onboarding"}\')')
        sys.exit(1)

    project_path = positional[0]
    phases_arg = positional[1]

    # Parse phases
    phases = [p.strip().lower() for p in phases_arg.split(",") if p.strip()]
    invalid = [p for p in phases if p not in PHASE_ORDER]
    if invalid:
        print(f"Error: Invalid phases: {', '.join(invalid)}")
        print(f"Valid phases: {', '.join(PHASE_ORDER)}")
        sys.exit(1)

    # Validate project path
    if not os.path.isdir(project_path):
        print(f"Error: Project path not found: {project_path}")
        sys.exit(1)

    if mode not in ("present", "worktree"):
        print(f"Error: Unknown mode '{mode}'. Use 'present' or 'worktree'.")
        sys.exit(1)

    presenter_script = find_presenter_script()

    if mode == "worktree":
        launch_worktree_mode(project_path, phases, presenter_script, tag_map=tag_map)
    else:
        launch_present_mode(project_path, phases, presenter_script)


if __name__ == "__main__":
    main()
