#!/usr/bin/env python3
"""AAH welcome screen emitter.

Modes:
  --hook           : PreToolUse hook — full welcome as systemMessage
  --hook-expansion : UserPromptExpansion hook — full welcome as systemMessage
"""

import json
import os
import sys

if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

# Deloitte brand green (#86BC25) as a truecolor ANSI escape. The CLI's
# systemMessage renderer resets styling at each newline, so any colored
# multi-line block must re-apply the escape on every line. Honors the
# NO_COLOR convention (https://no-color.org/): when set, the frame renders
# plain so the banner degrades cleanly on terminals without color.
if os.environ.get("NO_COLOR"):
    _GREEN = ""
    _BOLD = ""
    _RESET = ""
else:
    _GREEN = "\033[38;2;134;188;37m"
    _BOLD = "\033[1m"
    _RESET = "\033[0m"

# The AAH block-letter logo. The solid `█` glyphs stay in the default
# foreground; the box-drawing "shadow" glyphs behind them carry the green
# accent (along with the frame and the Deloitte dot).
_SHADOW_CHARS = frozenset("╗║╝═╔╚")


def _colorize_logo_row(row: str) -> tuple[str, int]:
    """Return (styled_row, visible_width) with shadow glyphs greened.

    `█` blocks and spaces render default; contiguous runs of box-drawing
    shadow glyphs are wrapped in a single green/reset pair to keep the
    escape count low. The visible width is the raw character count so the
    frame padding stays aligned.
    """
    out: list[str] = []
    in_shadow = False
    for ch in row:
        is_shadow = ch in _SHADOW_CHARS
        if is_shadow and not in_shadow:
            out.append(_GREEN)
            in_shadow = True
        elif not is_shadow and in_shadow:
            out.append(_RESET)
            in_shadow = False
        out.append(ch)
    if in_shadow:
        out.append(_RESET)
    return "".join(out), len(row)


_LOGO_ROWS = [
    r" █████╗   █████╗  ██╗  ██╗",
    r"██╔══██╗ ██╔══██╗ ██║  ██║",
    r"███████║ ███████║ ███████║",
    r"██╔══██║ ██╔══██║ ██╔══██║",
    r"██║  ██║ ██║  ██║ ██║  ██║",
    r"╚═╝  ╚═╝ ╚═╝  ╚═╝ ╚═╝  ╚═╝",
]
_TAGLINE = "Ascend Agentic Harness"
_SUBTAGLINE = "AI-Native Delivery Framework for Agentic-AI Development"
_BRAND = "Deloitte"  # rendered default; the trailing dot is green


def _build_banner() -> str:
    """Compose the framed AAH logo with a 'Deloitte.' tab in the top border.

    The rounded frame is green (_GREEN) and the Deloitte wordmark's dot is
    the signature green accent; the AAH `█` letterforms and tagline stay in
    the terminal's default foreground while the box-drawing "shadow" glyphs
    behind the letters carry the green accent. Every colored segment is
    wrapped individually so the per-line style reset in the systemMessage
    renderer can't bleed color onto the interior text.
    """
    inner = 61  # interior width between the vertical borders
    pad = 3     # left indent for interior content
    g, r = _GREEN, _RESET

    def framed(content: str, content_len: int | None = None) -> str:
        # content may contain color escapes; content_len is its *visible*
        # width so padding to the right border stays aligned.
        vis = content_len if content_len is not None else len(content)
        return f"{g}│{r}{' ' * pad}{content}{' ' * (inner - pad - vis)}{g}│{r}"

    # Top border with an embedded "Deloitte." tab:
    #   ╭─ Deloitte. ─────────────────╮
    # The corner + dashes are green; "Deloitte" is default; the dot is green.
    tab_visible = f" {_BRAND}. "          # visible text inside the border
    lead = 1                               # one dash before the tab
    trail = inner - lead - len(tab_visible)
    b = _BOLD
    top = (
        f"{g}╭{'─' * lead}{r}"
        f"{g} {r}{b}{_BRAND}{r}{b}{g}.{r}{g} {r}"  # " Deloitte. " bold, bold green dot
        f"{g}{'─' * trail}╮{r}"
    )
    bottom = f"{g}╰{'─' * inner}╯{r}"
    rule = f"{g}├{'─' * inner}┤{r}"
    blank = framed("")

    rows = [top, blank]
    for row in _LOGO_ROWS:
        styled, vis = _colorize_logo_row(row)
        rows.append(framed(styled, vis))
    rows.append(blank)
    rows.append(rule)
    rows.append(framed(f"{b}{_TAGLINE}{r}", len(_TAGLINE)))
    rows.append(framed(_SUBTAGLINE))
    rows.append(bottom)
    return "\n".join(rows)


AAH_BANNER = _build_banner()

AAH_PHASE_TABLE = """\
┌───┬──────────────┬───────────────────┬─────────────────────────────────────────┐
│ # │ Phase        │ Command           │ What it does                            │
├───┼──────────────┼───────────────────┼─────────────────────────────────────────┤
│ 1 │ Init         │ /aah-init-project │ Onboard a project (new or existing)     │
│ 2 │ Discuss      │ /aah-discuss      │ Research, decisions, and PRD            │
│ + │ Access       │ /aah-access       │ Cloud connectivity & data readiness     │
│ 3 │ Architecture │ /aah-arch         │ Vertical-slice architecture             │
│ + │ UX           │ /aah-ux           │ Wireframes & screen inventory           │
│ 4 │ Plan         │ /aah-plan         │ Specs, features, DAG, waves             │
│ 5 │ Build        │ /aah-build        │ Implementation behind quality gates     │
│ + │ Fix          │ /aah-fix          │ Bug intake, blast-radius, rework        │
│ 6 │ Deploy       │ /aah-deploy       │ Provision & ship the validated build    │
└───┴──────────────┴───────────────────┴─────────────────────────────────────────┘"""

AAH_FULL_OUTPUT = AAH_BANNER + """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  The Ascend Agentic Harness (AAH) is an AI-driven delivery
  framework that ships production software through structured
  phases — with Claude Code as the engine.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  FAQ

  ◆ What is AAH?
    AI-driven delivery framework using Claude Code as the engine.

  ◆ What is OBV?
    The core delivery spine — Orchestrate (Discuss → Arch → Plan),
    Build + Validate, Deploy. Non-core skills (UX, Access, Fix)
    are optional and invoked on demand alongside core phases.

  ◆ Where do I start?
    Run /aah-init-project to scaffold or import a project.

  ◆ What stacks are supported?
    Stack-agnostic — Python, Node, React, Go, FastAPI, Next.js, etc.

  ◆ What are phases?
    Init → Discuss → Architecture → Plan → Build → Deploy.
    Each phase produces artifacts that feed the next.

  ◆ What are ports?
    A sanctioned seam on the spine where a non-core capability (UX, Access, Evals)
    plugs in without editing the core skill.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Phases

""" + AAH_PHASE_TABLE + """

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Managing secrets & .env

  ◆ Claude Code cannot read or write your .env — this is by design, so
    real secrets never enter the model's context.
  ◆ AAH plans it for you: /aah-plan writes a committed .env.example
    catalog (non-secret cloud values from /aah-access are pre-filled).
  ◆ YOU create the real file — run  cp .env.example .env  and fill in
    the remaining secret values.
  ◆ Build and the runtime validator read values only from that
    gitignored .env; an empty/missing value fails with ERR_CDR_78_EX_CONFIG.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Next Steps

  → New here?        Run /aah-init-project
  → Returning?       Run /aah-resume

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _hook_mode() -> None:
    """PreToolUse hook: read stdin, check skill, emit systemMessage."""
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    skill = tool_input.get("skill", "")

    if skill != "aah":
        sys.exit(0)

    response = json.dumps({"systemMessage": "\n" + AAH_FULL_OUTPUT + "\n"})
    sys.stdout.write(response + "\n")
    sys.stdout.flush()
    sys.exit(0)


def _hook_expansion_mode() -> None:
    """UserPromptExpansion hook: fires when user types /aah directly."""
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError, ValueError):
        sys.exit(0)

    command_name = hook_input.get("command_name", "")
    if command_name != "aah":
        sys.exit(0)

    response = json.dumps({"systemMessage": "\n" + AAH_FULL_OUTPUT + "\n"})
    sys.stdout.write(response + "\n")
    sys.stdout.flush()
    sys.exit(0)


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="AAH welcome screen")
    parser.add_argument("--hook", action="store_true")
    parser.add_argument("--hook-expansion", action="store_true")
    args = parser.parse_args()

    if args.hook_expansion:
        _hook_expansion_mode()
    elif args.hook:
        _hook_mode()


if __name__ == "__main__":
    main()
