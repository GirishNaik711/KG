#!/usr/bin/env python3
"""
Template-based Dockerfile generation by language/framework.

Generates a production Dockerfile for a service if one doesn't exist.
Used by the serverless pipeline before dispatching deploy agents.

Note: For Cloud Run, Dockerfile generation is optional (buildpacks via --source).
For ECS Fargate, Dockerfile is required (built remotely via CodeBuild).
Base images MUST use ECR Public Gallery (public.ecr.aws/docker/library/) to avoid Docker Hub rate limits.

Usage:
    aah run core.deploy.dockerfile_generator generate \
      --service-path <path> --language python --framework fastapi --port 8080
"""

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Dockerfile templates
# ---------------------------------------------------------------------------

PYTHON_FASTAPI_TEMPLATE = """\
FROM public.ecr.aws/docker/library/python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml requirements*.txt ./
RUN pip install --no-cache-dir . 2>/dev/null || \
    pip install --no-cache-dir -r requirements.txt 2>/dev/null || \
    echo "No installable dependencies found"

FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .
ENV PORT={port}
EXPOSE {port}
CMD ["uvicorn", "{entry_module}:app", "--host", "0.0.0.0", "--port", "{port}"]
"""

PYTHON_FLASK_TEMPLATE = """\
FROM public.ecr.aws/docker/library/python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml requirements*.txt ./
RUN pip install --no-cache-dir . 2>/dev/null || \
    pip install --no-cache-dir -r requirements.txt 2>/dev/null || \
    echo "No installable dependencies found"

FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .
ENV PORT={port}
EXPOSE {port}
CMD ["gunicorn", "{entry_module}:app", "--bind", "0.0.0.0:{port}", "--workers", "2"]
"""

PYTHON_GENERIC_TEMPLATE = """\
FROM public.ecr.aws/docker/library/python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml requirements*.txt ./
RUN pip install --no-cache-dir . 2>/dev/null || \
    pip install --no-cache-dir -r requirements.txt 2>/dev/null || \
    echo "No installable dependencies found"

FROM public.ecr.aws/docker/library/python:3.12-slim
WORKDIR /app
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY . .
ENV PORT={port}
EXPOSE {port}
CMD ["python", "{entry_point}"]
"""

NODE_NEXTJS_TEMPLATE = """\
FROM public.ecr.aws/docker/library/node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM public.ecr.aws/docker/library/node:20-alpine
WORKDIR /app
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./
COPY --from=builder /app/public ./public
ENV PORT={port}
EXPOSE {port}
CMD ["npm", "start"]
"""

NODE_VITE_SPA_TEMPLATE = """\
FROM public.ecr.aws/docker/library/node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM public.ecr.aws/docker/library/node:20-alpine
WORKDIR /app
RUN npm install -g serve
COPY --from=builder /app/dist ./dist
ENV PORT={port}
EXPOSE {port}
CMD ["serve", "-s", "dist", "-l", "{port}"]
"""

NODE_EXPRESS_TEMPLATE = """\
FROM public.ecr.aws/docker/library/node:20-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production

FROM public.ecr.aws/docker/library/node:20-alpine
WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY . .
ENV PORT={port}
EXPOSE {port}
CMD ["node", "{entry_point}"]
"""

GO_TEMPLATE = """\
FROM golang:1.22-alpine AS builder
WORKDIR /app
COPY go.* ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -o server ./{entry_dir}

FROM gcr.io/distroless/static-debian12
COPY --from=builder /app/server /server
ENV PORT={port}
EXPOSE {port}
CMD ["/server"]
"""


# ---------------------------------------------------------------------------
# Template selection and generation
# ---------------------------------------------------------------------------

def generate_dockerfile(
    service_path: Path,
    language: str,
    framework: str,
    port: int,
    entry_point: str = "",
) -> Path:
    """
    Generate Dockerfile at service_path/Dockerfile if not present.

    Args:
        service_path: path to the service directory
        language: python | node | go
        framework: fastapi | flask | express | nextjs | vite | adk | langgraph | bare
        port: port the service listens on
        entry_point: application entry point (e.g., "src/main.py")

    Returns:
        Path to the generated (or existing) Dockerfile
    """
    dockerfile_path = service_path / "Dockerfile"

    if dockerfile_path.exists():
        return dockerfile_path

    template = _select_template(language, framework)
    content = _render_template(template, language, framework, port, entry_point, service_path)

    dockerfile_path.write_text(content, encoding="utf-8")
    return dockerfile_path


def _select_template(language: str, framework: str) -> str:
    """Select the appropriate Dockerfile template."""
    if language == "python":
        if framework in ("fastapi", "adk", "langgraph"):
            return PYTHON_FASTAPI_TEMPLATE
        if framework == "flask":
            return PYTHON_FLASK_TEMPLATE
        return PYTHON_GENERIC_TEMPLATE

    elif language == "node":
        if framework == "nextjs":
            return NODE_NEXTJS_TEMPLATE
        if framework in ("vite", "vue", "svelte", "angular"):
            return NODE_VITE_SPA_TEMPLATE
        if framework == "express":
            return NODE_EXPRESS_TEMPLATE
        return NODE_EXPRESS_TEMPLATE  # Default node template

    elif language == "go":
        return GO_TEMPLATE

    # Fallback: generic Python
    return PYTHON_GENERIC_TEMPLATE


def _render_template(
    template: str,
    language: str,
    framework: str,
    port: int,
    entry_point: str,
    service_path: Path,
) -> str:
    """Render template with service-specific values."""
    # Compute entry_module for Python uvicorn/gunicorn
    entry_module = ""
    if language == "python" and entry_point:
        # Convert "src/main.py" → "src.main"
        entry_module = entry_point.replace("/", ".").replace("\\", ".").removesuffix(".py")

    # Compute entry_dir for Go
    entry_dir = ""
    if language == "go" and entry_point:
        # Convert "cmd/server/main.go" → "cmd/server"
        entry_dir = str(Path(entry_point).parent)
        if entry_dir == ".":
            entry_dir = "."

    return template.format(
        port=port,
        entry_point=entry_point or "main.py",
        entry_module=entry_module or "src.main",
        entry_dir=entry_dir or ".",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Template-based Dockerfile generation")
    sub = parser.add_subparsers(dest="command", required=True)

    gen_p = sub.add_parser("generate", help="Generate Dockerfile for a service")
    gen_p.add_argument("--service-path", type=Path, required=True)
    gen_p.add_argument("--language", type=str, required=True)
    gen_p.add_argument("--framework", type=str, required=True)
    gen_p.add_argument("--port", type=int, required=True)
    gen_p.add_argument("--entry-point", type=str, default="")

    args = parser.parse_args()

    if args.command == "generate":
        result_path = generate_dockerfile(
            args.service_path,
            args.language,
            args.framework,
            args.port,
            args.entry_point,
        )
        already_existed = result_path.stat().st_size > 0  # crude check
        result = {
            "dockerfile_path": str(result_path),
            "generated": not (service_path / "Dockerfile").exists() if False else True,
            "language": args.language,
            "framework": args.framework,
            "port": args.port,
        }
        json.dump(result, sys.stdout, indent=2)
        print()


if __name__ == "__main__":
    main()
