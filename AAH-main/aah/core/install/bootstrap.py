"""Self-persisting bootstrap for ``aah install``.

The one-liner setup is::

    uvx --from git+<url> aah install            # user, global
    uvx --from git+<url> aah install --project  # user, local (project venv)
    uvx --from git+<url> aah install --dev      # developer, editable clone in cwd

``uvx`` runs the tool from uv's *ephemeral cache*. If ``aah install`` merely
linked ``~/.claude`` from there, every symlink would point into the cache and
DANGLE the moment uv prunes it. So before linking, ``aah install`` detects it is
running from the cache and first bootstraps a **durable** install, then re-execs
the durable ``aah install`` to do the actual linking.

Detection is deterministic, not a heuristic: we ask uv for its cache dir
(``uv cache dir`` — the authoritative path, honoring ``UV_CACHE_DIR`` / config /
platform defaults) and test whether this package lives under it. The three
install kinds land in non-overlapping trees:

* ephemeral ``uvx``       -> under ``uv cache dir``      -> needs_persist == True
* durable ``uv tool``     -> under ``uv tool dir``       -> link only
* editable dev clone      -> the source repo path        -> link only

The git URL is never hardcoded in code: it is read back from the installed
package metadata (``[project.urls] Repository`` in pyproject -> ``Project-URL``
in the wheel METADATA), preferring the exact URL the user installed from
(PEP 610 ``direct_url.json``) when available. https is tried first, ssh second.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _uv_query(args: list[str]) -> str | None:
    """Run a read-only ``uv`` query and return clean stdout (no ANSI), or None.

    uv colorizes output on some terminals even under capture; we force
    ``NO_COLOR`` and strip any residual escape codes so path parsing is exact.
    """
    import shutil

    if not shutil.which("uv"):
        return None
    env = {**os.environ, "NO_COLOR": "1", "CLICOLOR": "0"}
    try:
        out = subprocess.run(
            ["uv", *args],
            capture_output=True, text=True, check=True, env=env,
        ).stdout
    except Exception:
        return None
    return _ANSI_RE.sub("", out).strip() or None


# ---------------------------------------------------------------------------
# Ephemeral detection
# ---------------------------------------------------------------------------

def _uv_cache_dir() -> Path | None:
    """Authoritative uv cache dir (``uv cache dir``), or None if uv is absent."""
    out = _uv_query(["cache", "dir"])
    return Path(out).resolve() if out else None


def running_from_cache() -> bool:
    """True when this ``aah`` package is running from uv's ephemeral cache."""
    cache = _uv_cache_dir()
    if cache is None:
        return False
    here = Path(__file__).resolve()
    return cache == here or cache in here.parents


# ---------------------------------------------------------------------------
# Repo URL resolution — read back from install metadata, never hardcoded
# ---------------------------------------------------------------------------

def _url_from_direct_url() -> str | None:
    """Exact URL this package was installed from (PEP 610 ``direct_url.json``).

    For a ``git+…`` install this is precisely the spec uvx resolved — https or
    ssh, whatever the user typed — so it round-trips forks and auth schemes for
    free. Returns a bare ``https://…`` / ``ssh://…`` (``git+`` prefix stripped).
    """
    try:
        from importlib.metadata import distribution
        import json

        text = distribution("aah").read_text("direct_url.json")
        if not text:
            return None
        data = json.loads(text)
        url = data.get("url")
        vcs = (data.get("vcs_info") or {}).get("vcs")
        if url and vcs:  # a VCS (git) install, not a local dir/wheel
            return url
    except Exception:
        pass
    return None


def local_source_dir() -> Path | None:
    """Local directory this ``aah`` was installed from (PEP 610 ``dir_info``), else None.

    Set when launched via ``uvx --from <path> …`` / ``uv pip install <path>``. In
    that case the durable install must persist from THIS local path — not reach
    out to git — so local testing needs no repo access or auth.
    """
    try:
        from importlib.metadata import distribution
        from urllib.parse import unquote, urlparse
        import json

        text = distribution("aah").read_text("direct_url.json")
        if not text:
            return None
        data = json.loads(text)
        if "dir_info" not in data:
            return None  # a git/archive install, not a local dir
        url = data.get("url", "")
        if not url.startswith("file://"):
            return None
        path = Path(unquote(urlparse(url).path))
        return path if path.exists() else None
    except Exception:
        return None


def _url_from_project_urls() -> str | None:
    """``[project.urls] Repository`` from the wheel METADATA (Project-URL)."""
    try:
        from importlib.metadata import metadata

        for entry in metadata("aah").get_all("Project-URL") or []:
            # format: "Repository, https://github.com/org/repo"
            label, _, value = entry.partition(",")
            if label.strip().lower() in ("repository", "source", "homepage"):
                return value.strip()
    except Exception:
        pass
    return None


def _base_repo_url() -> str:
    """Resolve the harness repo URL. Precedence: env > direct_url > project.urls."""
    env = os.environ.get("AAH_REPO_URL")
    if env:
        return env
    return _url_from_direct_url() or _url_from_project_urls() or ""


def _parse_host_org_repo(url: str) -> tuple[str, str, str] | None:
    """Extract (host, org, repo) from an https/ssh/git+ github-style URL."""
    u = url.strip()
    u = re.sub(r"^git\+", "", u)          # git+https://…  -> https://…
    u = re.sub(r"\.git$", "", u)          # trailing .git
    # ssh scp-like:  git@github.com:org/repo
    m = re.match(r"^(?:ssh://)?[\w.-]+@([\w.-]+)[:/](.+?)/(.+)$", u)
    if m:
        return m.group(1), m.group(2), m.group(3)
    # https / ssh URL form:  https://host/org/repo
    m = re.match(r"^[a-z]+://(?:[^@/]+@)?([\w.-]+)/(.+?)/(.+)$", u)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


# Extras pulled in for a durable install (mirrors AAH-Dependency-Installer.sh) — placed on the
# package NAME per PEP 508: ``aah[cloud,gcp] @ git+…``, NOT on the URL.
_EXTRAS = "[cloud,gcp]"


def install_arg_variants() -> list[list[str]]:
    """Ordered install-argument variants to try for a durable install.

    Each variant is the arg tail appended after ``uv tool install --force`` (or
    ``uv pip install --python <venv>``). Precedence:

    1. **Local source** (``uvx --from <path>`` / ``uv pip install <path>``) —
       persist EDITABLE from that same path. No git, no auth: local testing and
       ``uvx --from ../script`` just work, and edits stay live.
    2. **Git** — ``aah[extras] @ git+https://…`` then the ssh form.

    Env ``AAH_REPO_URL`` forces the git path (overrides local detection).
    """
    # 1. Local dir install → editable from that path (unless AAH_REPO_URL forces git).
    if not os.environ.get("AAH_REPO_URL"):
        local = local_source_dir()
        if local is not None:
            return [["--editable", f"{local}{_EXTRAS}"]]

    # 2. Git install spec (https first, ssh fallback).
    base = _base_repo_url()
    parts = _parse_host_org_repo(base) if base else None
    if not parts:
        if base:  # unparseable but present -> use it verbatim (best effort)
            spec = base if base.startswith("git+") else f"git+{base}"
            return [[f"aah{_EXTRAS} @ {spec}"]]
        raise RuntimeError(
            "cannot determine an install source to bootstrap a durable install.\n"
            "set AAH_REPO_URL, or install non-ephemerally: "
            'uv tool install "aah @ git+<url>" && aah install'
        )
    host, org, repo = parts
    return [
        [f"aah{_EXTRAS} @ git+https://{host}/{org}/{repo}.git"],
        [f"aah{_EXTRAS} @ git+ssh://git@{host}/{org}/{repo}.git"],
    ]


def clone_urls() -> list[str]:
    """Ordered clone URLs for ``--dev``: https first, ssh fallback."""
    base = _base_repo_url()
    parts = _parse_host_org_repo(base) if base else None
    if not parts:
        raise RuntimeError(
            "cannot determine the harness repo URL to clone for --dev.\n"
            "set AAH_REPO_URL to the repo (https or ssh)."
        )
    host, org, repo = parts
    return [
        f"https://{host}/{org}/{repo}.git",
        f"ssh://git@{host}/{org}/{repo}.git",
    ]


# ---------------------------------------------------------------------------
# Persist actions
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> int:
    print(f"    $ {' '.join(cmd)}")
    return subprocess.run(cmd, check=False).returncode


def _run_captured(cmd: list[str]) -> tuple[int, str]:
    """Run a command, printing stdout live, capturing stderr. Returns (exit_code, stderr)."""
    print(f"    $ {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0 and result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode, result.stderr or ""


_PYTHON_VERSION_MISMATCH_RE = re.compile(
    r"Python\s+(\d+\.\d+(?:\.\d+)?)\)?\s+does not satisfy\s+Python\s*>=\s*3\.11"
    r"|No Python\b.*?(?:found|available).+>=\s*3\.11",
    re.IGNORECASE,
)


def _diagnose_python_version(stderr: str) -> str | None:
    """If stderr indicates a Python version mismatch, return an actionable message."""
    match = _PYTHON_VERSION_MISMATCH_RE.search(stderr)
    if not match:
        return None
    resolved = match.group(1) if match.lastindex and match.group(1) else "an older version"
    repo_url = _base_repo_url() or "<repo-url>"
    return (
        f"\n{'=' * 72}\n"
        f"  uv resolved Python {resolved}, which doesn't meet the required >=3.11.\n"
        f"  This usually means uv isn't discovering a newer interpreter you already\n"
        f"  have installed.\n"
        f"\n"
        f"  Try one of:\n"
        f"    1) UV_PYTHON=3.11 uvx --from git+{repo_url} aah install\n"
        f"    2) uv python install 3.11   (let uv manage its own copy)\n"
        f"\n"
        f"  Run `uv python list` to see what uv currently detects.\n"
        f"{'=' * 72}\n"
    )


def _uv_tool_bin_dir() -> Path | None:
    """uv's tool executable dir (``uv tool dir --bin``), or None if unavailable.

    A freshly-installed tool lands here even before the shell's PATH is updated
    (``uv tool update-shell``), so we probe it directly rather than relying on
    ``which``.
    """
    out = _uv_query(["tool", "dir", "--bin"])
    return Path(out).resolve() if out else None


def _durable_aah_bin() -> str | None:
    """Locate a durable ``aah`` (NOT the ephemeral cache copy).

    Tries ``which`` first (works once PATH is set up), then uv's tool-bin dir so
    a just-installed tool resolves even if the current shell's PATH is stale.
    """
    import shutil

    cache = _uv_cache_dir()

    def _is_ephemeral(p: Path) -> bool:
        p = p.resolve()
        return bool(cache) and (cache == p or cache in p.parents)

    found = shutil.which("aah")
    if found and not _is_ephemeral(Path(found)):
        return found

    bindir = _uv_tool_bin_dir()
    if bindir:
        for name in ("aah", "aah.exe"):
            cand = bindir / name
            if cand.exists() and not _is_ephemeral(cand):
                return str(cand)
    return None


def persist_global() -> str:
    """Durable ``uv tool install`` (local editable path, else git). Returns the aah bin."""
    print("==> bootstrap: installing a durable `aah` uv tool (ephemeral uvx run detected)")
    variants = install_arg_variants()
    last = 1
    last_stderr = ""
    for i, args in enumerate(variants):
        last, last_stderr = _run_captured(["uv", "tool", "install", "--force", *args])
        if last == 0:
            break
        if i < len(variants) - 1:
            print("    (retrying over the next install source)")
    if last != 0:
        hint = _diagnose_python_version(last_stderr)
        if hint:
            print(hint, file=sys.stderr)
        raise RuntimeError("durable `uv tool install` failed over all install sources.")
    binp = _durable_aah_bin()
    if not binp:
        raise RuntimeError(
            "durable install succeeded but `aah` is not on PATH — run `uv tool update-shell`."
        )
    return binp


def persist_local(project_dir: Path) -> str:
    """Durable project venv (``uv venv`` + ``uv pip install``). Returns the aah bin."""
    print("==> bootstrap: creating a project venv install (ephemeral uvx --project run)")
    venv = project_dir / ".venv"
    if not (venv / "bin" / "python").exists() and not (venv / "Scripts" / "python.exe").exists():
        if _run(["uv", "venv", str(venv)]) != 0:
            raise RuntimeError("`uv venv` failed.")
    variants = install_arg_variants()
    last = 1
    last_stderr = ""
    for i, args in enumerate(variants):
        last, last_stderr = _run_captured(["uv", "pip", "install", "--python", str(venv), *args])
        if last == 0:
            break
        if i < len(variants) - 1:
            print("    (retrying over the next install source)")
    if last != 0:
        hint = _diagnose_python_version(last_stderr)
        if hint:
            print(hint, file=sys.stderr)
        raise RuntimeError("`uv pip install` failed over all install sources.")
    for cand in (venv / "bin" / "aah", venv / "Scripts" / "aah.exe", venv / "Scripts" / "aah"):
        if cand.exists():
            return str(cand)
    raise RuntimeError(f"venv install succeeded but no aah binary under {venv}.")


def _dev_source_dir(project_dir: Path) -> Path:
    """Resolve the editable source for ``--dev``.

    #4: prefer the LOCAL source we were launched from (``uvx --from ../script``)
    — no clone, no auth, edits stay live. Otherwise clone the harness into
    ``<cwd>/ascend-agentic-harness`` (reusing an existing clone; https then ssh).
    """
    import shutil

    local = local_source_dir()
    if local is not None:
        print(f"==> bootstrap --dev: using local source at {local} (no clone)")
        return local

    if not shutil.which("git"):
        raise RuntimeError("--dev needs `git` on PATH to clone the harness.")
    clone_dir = project_dir / "ascend-agentic-harness"
    if (clone_dir / ".git").exists():
        print(f"==> bootstrap --dev: reusing existing clone at {clone_dir}")
        return clone_dir
    print(f"==> bootstrap --dev: cloning the harness into {clone_dir}")
    for url in clone_urls():
        if _run(["git", "clone", url, str(clone_dir)]) == 0:
            return clone_dir
        print("    (retrying clone over the next URL form)")
    raise RuntimeError("git clone failed over all URL forms.")


def persist_dev(project_dir: Path, project: bool) -> str:
    """Editable install of the harness source. Returns the durable aah bin.

    * ``project`` (#1) — editable into a project venv (``uv pip install -e`` into
      ``./.venv``): a fully-local editable dev setup.
    * else — editable global uv tool (live everywhere).
    """
    src = _dev_source_dir(project_dir)

    if project:
        print("==> bootstrap --dev --project: editable install into a project venv")
        venv = project_dir / ".venv"
        if not (venv / "bin" / "python").exists() and not (venv / "Scripts" / "python.exe").exists():
            if _run(["uv", "venv", str(venv)]) != 0:
                raise RuntimeError("`uv venv` failed.")
        if _run(["uv", "pip", "install", "--python", str(venv),
                 "-e", f"{src}{_EXTRAS}"]) != 0:
            raise RuntimeError("editable `uv pip install` into the venv failed.")
        for cand in (venv / "bin" / "aah", venv / "Scripts" / "aah.exe", venv / "Scripts" / "aah"):
            if cand.exists():
                return str(cand)
        raise RuntimeError(f"venv editable install succeeded but no aah under {venv}.")

    print("==> bootstrap --dev: editable global uv tool install")
    if _run(["uv", "tool", "install", "--force", "--reinstall",
             "--editable", f"{src}{_EXTRAS}"]) != 0:
        raise RuntimeError("editable `uv tool install` failed.")
    binp = _durable_aah_bin()
    if not binp:
        raise RuntimeError(
            "editable install succeeded but `aah` is not on PATH — run `uv tool update-shell`."
        )
    return binp


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def maybe_bootstrap(project: bool, dev: bool, platform: str) -> bool:
    """If running ephemerally (or ``--dev``), create a DURABLE install and re-exec
    ``aah setup`` on it to do the linking. Returns True if it handled everything
    (caller stops); False to fall through to normal in-process linking.
    """
    if not dev and not running_from_cache():
        return False  # durable install already — link in-process as usual

    project_dir = Path.cwd()
    if dev:
        aah_bin = persist_dev(project_dir, project=project)
    elif project:
        aah_bin = persist_local(project_dir)
    else:
        aah_bin = persist_global()

    # Re-exec the DURABLE aah's `setup` to link (setup never re-bootstraps).
    setup_cmd = [aah_bin, "setup", "--platform", platform]
    if project:
        setup_cmd.append("--project")
    print("==> bootstrap: linking via durable install (aah setup)")
    rc = _run(setup_cmd)
    if rc != 0:
        raise RuntimeError(f"`{aah_bin} setup` (link step) failed with exit {rc}.")
    return True
