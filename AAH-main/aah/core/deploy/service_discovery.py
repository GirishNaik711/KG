#!/usr/bin/env python3
"""
Convention-based multi-service scanner for the serverless deploy pipeline.

Discovers deployable services in a AAH project by scanning project structure:
- Pattern 1 (single-service): src/ = backend, frontend/ = frontend
- Pattern 2 (multi-service): services/ dir with subfolders

Usage:
    aah run core.deploy.service_discovery discover --project-path <path>
"""

import argparse
import json
import sys
from pathlib import Path


# Frontend folder names (detected as frontend services)
FRONTEND_FOLDER_NAMES = {"frontend", "web", "ui", "client"}

# Frontend config files (if any of these exist in a folder, it's a frontend)
FRONTEND_CONFIG_FILES = {
    "next.config.js", "next.config.mjs", "next.config.ts",
    "vite.config.js", "vite.config.ts", "vite.config.mjs",
    "angular.json",
    "nuxt.config.ts", "nuxt.config.js",
    "svelte.config.js",
}


def discover_services(project_path: Path) -> list[dict]:
    """
    Convention-based service scanner. Scans root-level folders for deployable services.

    Rules:
    1. src/ + root pyproject.toml = main backend (always present if src/ exists)
    2. Any other root-level folder with its own pyproject.toml / package.json / go.mod
       = additional service
    3. Folder named frontend/web/ui/client OR containing frontend config files
       (next.config.*, vite.config.*, angular.json) = frontend service
    4. Dependencies inferred from: env var references in .env* files,
       or explicit depends_on in service-manifest.yaml
    5. Deploy order: backend services first (by dependency), frontend last

    Returns list of:
    {name, path, language, framework, port, is_frontend, dependencies, has_dockerfile}
    """
    return _discover_from_root(project_path)


def _discover_from_root(project_path: Path) -> list[dict]:
    """
    Scan project root for all deployable services.

    1. src/ + root pyproject.toml = main backend
    2. Any other root folder with its own pyproject.toml/package.json/go.mod = service
    3. Frontend folders detected by name or config files
    """
    services = []
    backend_names = []

    # Skip these root-level folders (not services)
    skip_folders = {
        ".aah", ".git", ".github", ".gitlab", ".vscode", ".idea",
        "node_modules", "__pycache__", ".venv", "venv", "env",
        "tests", "test", "docs", "scripts", "config", "data",
        "packages", "build", "dist", ".next", "coverage",
    }

    # 1. Main backend: src/ + root pyproject.toml
    src_dir = project_path / "src"
    has_root_project = (
        (project_path / "pyproject.toml").exists()
        or (project_path / "package.json").exists()
        or (project_path / "go.mod").exists()
    )

    if src_dir.is_dir() and has_root_project:
        language = detect_language(project_path)
        framework = detect_framework(project_path, language)
        port = detect_port(project_path, language, framework, is_frontend=False)
        entry_point = detect_entry_point(project_path, language, framework)

        services.append({
            "name": "backend",
            "path": ".",
            "language": language,
            "framework": framework,
            "port": port,
            "is_frontend": False,
            "dependencies": [],
            "has_dockerfile": (project_path / "Dockerfile").exists(),
            "entry_point": entry_point,
        })
        backend_names.append("backend")

    # 2. Scan root-level folders for additional services
    for entry in sorted(project_path.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name.startswith("_"):
            continue
        if entry.name.lower() in skip_folders:
            continue
        if entry.name == "src":
            continue  # Already handled as main backend

        # Check if this folder is a frontend
        is_frontend = _is_frontend(entry)

        # Must have its own project indicator to be a service
        if not _is_service_folder(entry):
            continue

        language = detect_language(entry)
        framework = detect_framework(entry, language)
        port = detect_port(entry, language, framework, is_frontend)
        entry_point = detect_entry_point(entry, language, framework)
        dependencies = _detect_dependencies(entry, project_path)

        # Frontends depend on all known backends by default
        if is_frontend and not dependencies:
            dependencies = list(backend_names)

        services.append({
            "name": entry.name,
            "path": entry.name,
            "language": language,
            "framework": framework,
            "port": port,
            "is_frontend": is_frontend,
            "dependencies": dependencies,
            "has_dockerfile": (entry / "Dockerfile").exists(),
            "entry_point": entry_point,
        })

        if not is_frontend:
            backend_names.append(entry.name)

    return services


def _is_service_folder(path: Path) -> bool:
    """Check if a folder is a deployable service (has project indicator files)."""
    indicators = [
        "Dockerfile", "pyproject.toml", "package.json", "go.mod",
        "requirements.txt", "Cargo.toml",
    ]
    return any((path / f).exists() for f in indicators)


def _is_frontend(path: Path) -> bool:
    """Detect if a service path is a frontend service."""
    # Check folder name
    if path.name.lower() in FRONTEND_FOLDER_NAMES:
        return True

    # Check for frontend config files
    for config_file in FRONTEND_CONFIG_FILES:
        if (path / config_file).exists():
            return True

    return False


def detect_language(service_path: Path) -> str:
    """
    Detect programming language from project files.

    Returns: python | node | go | rust | unknown
    """
    if (service_path / "pyproject.toml").exists() or (service_path / "requirements.txt").exists():
        return "python"
    if (service_path / "package.json").exists():
        return "node"
    if (service_path / "go.mod").exists():
        return "go"
    if (service_path / "Cargo.toml").exists():
        return "rust"

    # Fallback: scan for source files
    if list(service_path.rglob("*.py")):
        return "python"
    if list(service_path.rglob("*.ts")) or list(service_path.rglob("*.js")):
        return "node"
    if list(service_path.rglob("*.go")):
        return "go"

    return "unknown"


def detect_framework(service_path: Path, language: str) -> str:
    """
    Detect framework from project files and imports.

    Returns: fastapi | flask | express | nextjs | vite | adk | langgraph | django | bare
    """
    if language == "python":
        return _detect_python_framework(service_path)
    elif language == "node":
        return _detect_node_framework(service_path)
    elif language == "go":
        return "bare"  # Go frameworks less standardized
    return "bare"


def _detect_python_framework(service_path: Path) -> str:
    """Detect Python framework from pyproject.toml or imports."""
    pyproject = service_path / "pyproject.toml"
    requirements = service_path / "requirements.txt"

    # Read dependency sources
    deps_text = ""
    if pyproject.exists():
        deps_text = pyproject.read_text(encoding="utf-8", errors="ignore").lower()
    if requirements.exists():
        deps_text += "\n" + requirements.read_text(encoding="utf-8", errors="ignore").lower()

    # Check in priority order
    if "google-adk" in deps_text or "google_adk" in deps_text:
        return "adk"
    if "langgraph" in deps_text:
        return "langgraph"
    if "fastapi" in deps_text:
        return "fastapi"
    if "flask" in deps_text:
        return "flask"
    if "django" in deps_text:
        return "django"

    # Fallback: scan imports in src/
    src_dir = service_path / "src"
    scan_dirs = [src_dir] if src_dir.exists() else [service_path]
    for scan_dir in scan_dirs:
        for py_file in scan_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8", errors="ignore")
                if "from google.adk" in content or "import google.adk" in content:
                    return "adk"
                if "from langgraph" in content:
                    return "langgraph"
                if "from fastapi" in content or "FastAPI()" in content:
                    return "fastapi"
                if "from flask" in content or "Flask(__name__)" in content:
                    return "flask"
            except (OSError, PermissionError):
                continue

    return "bare"


def _detect_node_framework(service_path: Path) -> str:
    """Detect Node.js framework from package.json."""
    pkg_json = service_path / "package.json"
    if not pkg_json.exists():
        return "bare"

    try:
        content = pkg_json.read_text(encoding="utf-8", errors="ignore").lower()
        if '"next"' in content:
            return "nextjs"
        if '"vite"' in content or '"@vitejs' in content:
            return "vite"
        if '"express"' in content:
            return "express"
        if '"@angular/core"' in content:
            return "angular"
        if '"vue"' in content:
            return "vue"
        if '"svelte"' in content:
            return "svelte"
    except (OSError, PermissionError):
        pass

    return "bare"


def detect_port(service_path: Path, language: str, framework: str, is_frontend: bool) -> int:
    """
    Detect service port from configuration or use convention defaults.

    Priority:
    1. Dockerfile EXPOSE directive
    2. Procfile port
    3. Framework convention default
    """
    # Check Dockerfile for EXPOSE
    dockerfile = service_path / "Dockerfile"
    if dockerfile.exists():
        try:
            content = dockerfile.read_text(encoding="utf-8", errors="ignore")
            for line in content.splitlines():
                if line.strip().upper().startswith("EXPOSE"):
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        try:
                            return int(parts[1].split("/")[0])  # Handle "8080/tcp"
                        except ValueError:
                            pass
        except (OSError, PermissionError):
            pass

    # Convention defaults
    if is_frontend:
        if framework == "nextjs":
            return 3000
        if framework in ("vite", "vue", "svelte"):
            return 5173
        if framework == "angular":
            return 4200
        return 3000  # Generic frontend default

    # Backend defaults
    if framework in ("fastapi", "adk", "langgraph", "flask"):
        return 8080  # Cloud Run convention
    if framework == "django":
        return 8000
    if framework == "express":
        return 3000
    if language == "go":
        return 8080

    return 8080  # Universal backend default (Cloud Run expects this)


def detect_entry_point(service_path: Path, language: str, framework: str) -> str:
    """
    Detect the application entry point.

    Returns path relative to service_path (e.g., "src/main.py", "index.js")
    """
    if language == "python":
        # Check common entry points in priority order
        candidates = [
            "src/main.py",
            "src/app.py",
            "main.py",
            "app.py",
            "src/server.py",
            "server.py",
        ]
        for candidate in candidates:
            if (service_path / candidate).exists():
                return candidate

        # For ADK: look for agent.py
        if framework == "adk":
            if (service_path / "src/agent.py").exists():
                return "src/agent.py"
            if (service_path / "agent.py").exists():
                return "agent.py"

        return "src/main.py"  # Default assumption

    elif language == "node":
        candidates = [
            "src/index.js", "src/index.ts",
            "index.js", "index.ts",
            "src/server.js", "src/server.ts",
            "server.js", "server.ts",
        ]
        for candidate in candidates:
            if (service_path / candidate).exists():
                return candidate
        return "src/index.js"

    elif language == "go":
        candidates = [
            "cmd/server/main.go",
            "cmd/main.go",
            "main.go",
        ]
        for candidate in candidates:
            if (service_path / candidate).exists():
                return candidate
        return "main.go"

    return ""


def _detect_dependencies(service_path: Path, project_path: Path) -> list[str]:
    """
    Detect service dependencies from:
    1. Explicit service-manifest.yaml in service folder
    2. Env var references to other service names in .env files
    """
    dependencies = []

    # 1. Explicit manifest
    manifest = service_path / "service-manifest.yaml"
    if manifest.exists():
        try:
            from aah.core.common.io_utils import read_yaml
            data = read_yaml(manifest)
            return data.get("depends_on", [])
        except Exception:
            pass

    # 2. Scan .env files for references to sibling service names
    # Sibling = any root-level folder that is a service (has project indicators)
    sibling_names = set()
    for entry in project_path.iterdir():
        if entry.is_dir() and entry != service_path and not entry.name.startswith("."):
            if _is_service_folder(entry) and entry.name != "src":
                sibling_names.add(entry.name)
    # Also include "backend" (the main src/ service)
    if (project_path / "src").is_dir():
        sibling_names.add("backend")

    if not sibling_names:
        return []

    env_files = list(service_path.glob(".env*"))
    for env_file in env_files:
        try:
            content = env_file.read_text(encoding="utf-8", errors="ignore")
            content_upper = content.upper()
            for sibling in sibling_names:
                # Look for SIBLING_URL or SIBLING_HOST patterns
                sibling_upper = sibling.upper().replace("-", "_")
                if f"{sibling_upper}_URL" in content_upper or f"{sibling_upper}_HOST" in content_upper:
                    if sibling not in dependencies:
                        dependencies.append(sibling)
        except (OSError, PermissionError):
            continue

    return dependencies


def main() -> None:
    parser = argparse.ArgumentParser(description="Convention-based multi-service scanner")
    sub = parser.add_subparsers(dest="command", required=True)

    disc_p = sub.add_parser("discover", help="Discover services in project")
    disc_p.add_argument("--project-path", type=Path, required=True)

    args = parser.parse_args()

    if args.command == "discover":
        services = discover_services(args.project_path)
        json.dump({"services": services, "count": len(services)}, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
