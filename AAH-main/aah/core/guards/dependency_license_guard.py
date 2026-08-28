#!/usr/bin/env python3
"""
PreToolUse:Bash guard: block git commit if requirements.txt adds a
library with a critical or high license risk (GPL, AGPL, LGPL, unknown).

Only triggers when requirements.txt (or similar lockfiles) are staged
for commit. Runs the pre-check engine (PyPI API, no install) to evaluate
new or changed dependencies. Fast path: skips entirely if no dependency
files are staged.

Exit 0 = allow, Exit 2 = block (stderr fed back to agent).
"""

import json
import re
import subprocess
import sys
from pathlib import Path


def _load_license_suppressions(repo_root: Path) -> set[str]:
    """Load suppressed package names from suppressions.yaml files."""
    suppressed = set()

    from aah.core.security.state import ensure_security_state

    candidates = [ensure_security_state(repo_root, notify=True).security_dir / "suppressions.yaml"]
    try:
        from aah.core.common.config import resolve_project_path
        project_path = resolve_project_path()
        if project_path and project_path != repo_root:
            candidates.append(
                ensure_security_state(project_path, notify=True).security_dir / "suppressions.yaml"
            )
    except Exception:
        pass

    for supp_file in candidates:
        if not supp_file.exists():
            continue
        try:
            import yaml
            data = yaml.safe_load(supp_file.read_text(encoding='utf-8')) or {}
            for entry in data.get("suppressions", []):
                fid = entry.get("finding_id", "")
                if fid.startswith("license-scan:") or fid.startswith("pre-check:"):
                    pkg = entry.get("file", "").split("@")[0]
                    if pkg:
                        suppressed.add(pkg.lower().replace("-", "").replace("_", ""))
        except Exception:
            pass

    return suppressed


# setup.py is deliberately absent: it is Python code, so no line grammar reads it
# (`install_requires=["x"],` yields the key, then `"x",` quotes and all). It never
# produced a usable name, only noise that the unknown-package skip swallowed.
DEP_FILES = {
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "pyproject.toml",
    "Pipfile",
}


def _staged_dep_files(repo_root: Path) -> list[Path]:
    """Return dependency files that are staged for commit."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, timeout=5, cwd=str(repo_root),
        )
        if proc.returncode != 0:
            return []
        staged = proc.stdout.strip().splitlines()
        return [
            repo_root / f for f in staged
            if Path(f).name in DEP_FILES and (repo_root / f).exists()
        ]
    except (subprocess.TimeoutExpired, OSError):
        return []


def _spec_list(value: object) -> list[str]:
    """The strings in a TOML array, or [] for anything that is not an array.

    The array check is load-bearing: a bare string would iterate into single
    characters, and some one-letter names ARE real PyPI packages — the same
    false-positive class ``_pyproject_dep_names`` exists to remove.
    """
    return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []


def _pyproject_dep_names(content: str) -> set[str]:
    """Package names declared in a pyproject.toml, read as TOML.

    Only the real dependency tables are read. The requirements.txt grammar below
    treats every ``key = value`` line as a package, so a config key that collides
    with a real PyPI name — ``extend``, ``select``, ``version`` — resolves to an
    unrelated package and blocks the commit over its license.

    Unparseable or oddly shaped content yields no names: fail open, as everywhere
    else in this guard. tomllib is stdlib from 3.11 and the project requires 3.11.
    """
    import tomllib

    try:
        data = tomllib.loads(content)
        project = data.get("project") or {}
        specs = _spec_list(project.get("dependencies"))
        for group in (project.get("optional-dependencies") or {}).values():
            specs += _spec_list(group)
        # Poetry keys its dependency table BY package name and leaves
        # project.dependencies empty; without this a Poetry brownfield repo would
        # report no dependencies at all rather than none being risky.
        poetry = (data.get("tool") or {}).get("poetry") or {}
        for table in ("dependencies", "dev-dependencies"):
            entry = poetry.get(table)
            if isinstance(entry, dict):
                specs += [k for k in entry if isinstance(k, str) and k.lower() != "python"]
    except Exception:
        return set()

    names = set()
    for spec in specs:
        name = re.split(r"[=<>!~\[;(]", spec)[0].strip()
        if name:
            names.add(name)
    return names


def _git_show(repo_root: Path, rev_path: str) -> str | None:
    """Contents of ``rev_path`` (``:pyproject.toml``, ``HEAD:pyproject.toml``).

    None when the path does not exist at that revision.
    """
    try:
        proc = subprocess.run(
            ["git", "show", rev_path],
            capture_output=True, text=True, timeout=5, cwd=str(repo_root),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _new_packages(repo_root: Path, dep_file: Path) -> list[dict]:
    """Dependencies this commit ADDS to ``dep_file``.

    pyproject.toml is compared table-to-table (staged vs HEAD) instead of
    line-to-line, because only its parsed dependency tables are dependencies.
    Diffing the parsed sets keeps the "newly added only" contract the line-based
    path already has: checking every declared dependency on every commit would
    re-block a pre-existing risky dep forever and multiply PyPI lookups inside
    this hook's timeout.
    """
    if dep_file.name != "pyproject.toml":
        return _new_packages_in_diff(repo_root, dep_file)

    rel = dep_file.relative_to(repo_root).as_posix()
    staged = _git_show(repo_root, f":{rel}")
    if staged is None:
        return []
    before = _git_show(repo_root, f"HEAD:{rel}") or ""  # new file → every dep is new
    added = _pyproject_dep_names(staged) - _pyproject_dep_names(before)
    return [{"name": name, "version": None} for name in sorted(added)]


def _new_packages_in_diff(repo_root: Path, dep_file: Path) -> list[dict]:
    """Extract newly added package lines from the staged diff of a dep file."""
    try:
        proc = subprocess.run(
            ["git", "diff", "--cached", "-U0", str(dep_file.relative_to(repo_root))],
            capture_output=True, text=True, timeout=5, cwd=str(repo_root),
        )
        if proc.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, OSError):
        return []

    packages = []
    for line in proc.stdout.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        line = line[1:].strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        pkg_name = re.split(r"[=<>!~\[;]", line)[0].strip()
        if not pkg_name or pkg_name.startswith("["):
            continue
        version_match = re.search(r"==\s*([\w.]+)", line)
        version = version_match.group(1) if version_match else None
        packages.append({"name": pkg_name, "version": version})
    return packages


def check_command(command: str) -> str | None:
    """Check if a git commit should be blocked due to license risk.

    Returns a reason string if blocked, None if allowed.
    Only triggers on git commit commands with staged dependency files.
    """
    if not re.search(r"\bgit\s+commit\b", command):
        return None

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if proc.returncode != 0:
            return None
        repo_root = Path(proc.stdout.strip())
    except (subprocess.TimeoutExpired, OSError):
        return None

    staged_files = _staged_dep_files(repo_root)
    if not staged_files:
        return None

    all_new_packages = []
    for dep_file in staged_files:
        all_new_packages.extend(_new_packages(repo_root, dep_file))

    if not all_new_packages:
        return None

    try:
        from aah.core.security.pre_check import check_package
    except ImportError:
        return None

    try:
        suppressed_packages = _load_license_suppressions(repo_root)
    except Exception as exc:
        return f"Blocked: security state migration failed: {exc}"

    blocking = []
    for pkg in all_new_packages:
        pkg_key = pkg["name"].lower().replace("-", "").replace("_", "")
        if pkg_key in suppressed_packages:
            continue
        result = check_package(pkg["name"], pkg["version"], check_vulnerabilities=False)
        if result.get("error"):
            continue
        if result["license_risk"] in ("critical", "high"):
            blocking.append(
                f"  - {result['package']}=={result['version']}: "
                f"{result['license_spdx']} ({result['license_risk']} risk — "
                f"{result['license_explanation']})"
            )

    if not blocking:
        return None

    lines = [f"Blocked: {len(blocking)} package(s) with critical/high license risk"]
    lines.extend(blocking)
    return "\n".join(lines)


def main() -> None:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")

    if not command:
        sys.exit(0)

    reason = check_command(command)
    if reason:
        print(reason, file=sys.stderr)
        print(
            "\nAAH policy: Dependencies with GPL, AGPL, LGPL, or unknown licenses "
            "are blocked. Use a permissive alternative (MIT, BSD, Apache) or add a "
            "justification in .aah/security/suppressions.yaml.",
            file=sys.stderr,
        )
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
