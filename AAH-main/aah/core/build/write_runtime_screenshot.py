#!/usr/bin/env python3
"""
Persist the aah-runtime-validator's UI-render screenshot (Playwright MCP).

Called by the aah-runtime-validator subagent in step 11.5 (UI render check)
after `browser_take_screenshot`. The agent has no Write/Edit tools, so — like
write_test_dockerfile — file persistence routes through this tiny subprocess
writer. It drops the image into the wave's runtime-results folder with a
canonical name so the orchestrator/user can find it next to wave-N-all.json:

  .aah/build/runtime-results/wave-{N}-ui.png

The Playwright MCP `browser_take_screenshot` tool saves the image to its own
output location (or returns it inline). This writer accepts either route:

  1. --from PATH   : copy an image the browser tool already wrote to disk
  2. stdin base64  : decode a base64-encoded image piped on stdin

Exactly one route must be used. The screenshot is evidence for human review;
it is NOT a verification artifact — the orchestrator never reads it for gate
dispatch, so the attestation library is not involved (mirrors
write_test_dockerfile).

Prints the written path to stdout so the agent can record it in the
`ui_render.details.screenshot_path` field of the results blob.

Usage:
  aah run core.build.write_runtime_screenshot --wave N --from /tmp/shot.png [--project-path PATH]
  base64_data | aah run core.build.write_runtime_screenshot --wave N --stdin-base64 [--ext png]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import shutil
import sys
from pathlib import Path

# Image extensions the browser tool may produce; used to validate --ext and
# to sanity-check --from inputs.
_ALLOWED_EXT = ("png", "jpg", "jpeg", "webp")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Persist the runtime-validator UI screenshot for a wave"
    )
    parser.add_argument("--wave", type=int, required=True)
    parser.add_argument(
        "--from",
        dest="from_path",
        type=Path,
        default=None,
        help="Path to an image the browser tool already saved (will be copied).",
    )
    parser.add_argument(
        "--stdin-base64",
        action="store_true",
        help="Read base64-encoded image bytes from stdin and decode them.",
    )
    parser.add_argument(
        "--ext",
        default="png",
        help="Image extension for the stdin-base64 route (default: png).",
    )
    parser.add_argument("--project-path", type=Path, default=None)
    args = parser.parse_args()

    from aah.core.common.config import require_project_path
    project_path = require_project_path(args.project_path)

    # Exactly one input route.
    if bool(args.from_path) == bool(args.stdin_base64):
        print(
            "Error: provide exactly one of --from PATH or --stdin-base64.",
            file=sys.stderr,
        )
        sys.exit(1)

    output_dir = project_path / ".aah" / "build" / "runtime-results"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.from_path:
        # A relative --from resolves against the PROJECT, not the shell's cwd.
        # The agent runs commands from varying working directories, and resolving
        # against cwd is how #90's bare-root filenames became unfindable.
        src = args.from_path
        if not src.is_absolute():
            src = project_path / src
        if not src.is_file():
            print(f"Error: --from path is not a regular file: {src}", file=sys.stderr)
            sys.exit(1)
        if src.stat().st_size == 0:
            print(f"Error: --from image is empty (0 bytes): {src}", file=sys.stderr)
            sys.exit(1)
        ext = src.suffix.lstrip(".").lower() or "png"
        if ext not in _ALLOWED_EXT:
            print(
                f"Error: unsupported image extension '.{ext}' "
                f"(allowed: {', '.join(_ALLOWED_EXT)}).",
                file=sys.stderr,
            )
            sys.exit(1)
        out_path = output_dir / f"wave-{args.wave}-ui.{ext}"
        shutil.copyfile(src, out_path)
    else:
        ext = args.ext.lstrip(".").lower()
        if ext not in _ALLOWED_EXT:
            print(
                f"Error: unsupported --ext '{ext}' "
                f"(allowed: {', '.join(_ALLOWED_EXT)}).",
                file=sys.stderr,
            )
            sys.exit(1)
        raw = sys.stdin.buffer.read()
        if not raw.strip():
            print("Error: no base64 data on stdin.", file=sys.stderr)
            sys.exit(1)
        # Strip surrounding whitespace and any embedded newlines/spaces — tools
        # (and `echo`) commonly emit line-wrapped base64 with a trailing
        # newline, which validate=True would otherwise reject as excess data.
        cleaned = b"".join(raw.split())
        try:
            # validate=True rejects non-base64 noise instead of silently
            # dropping it, so a malformed pipe fails loudly.
            image_bytes = base64.b64decode(cleaned, validate=True)
        except (binascii.Error, ValueError) as e:
            print(f"Error: could not decode base64 image from stdin: {e}", file=sys.stderr)
            sys.exit(1)
        if not image_bytes:
            print("Error: decoded image is empty.", file=sys.stderr)
            sys.exit(1)
        out_path = output_dir / f"wave-{args.wave}-ui.{ext}"
        out_path.write_bytes(image_bytes)

    # Report success ONLY after the canonical file is provably on disk and
    # non-empty. The agent chains `&& rm -f` on this exit code, so a false
    # success here would delete the only copy of the image.
    if not out_path.is_file() or out_path.stat().st_size == 0:
        print(
            f"Error: persistence produced no usable file at {out_path}. "
            "The source image has NOT been removed.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"UI screenshot written to: {out_path}", file=sys.stderr)
    print(str(out_path))
    sys.exit(0)


if __name__ == "__main__":
    main()
