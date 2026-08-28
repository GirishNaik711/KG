#!/usr/bin/env bash
# Launch a Claude Code session pinned to a specific RAPIDS project.
#
# Usage:
#   rapids-open workspace/project
#   rapids-open my-workspace/inventory-api
#
# Opens Claude Code with RAPIDS_CONTEXT set so that all `aah run` commands
# in that session resolve to the specified workspace/project — no contention
# with rapids-config.yaml. Safe for parallel sessions.

set -euo pipefail

if [[ $# -ne 1 ]] || [[ "$1" != */* ]]; then
    echo "Usage: rapids-open <workspace>/<project>" >&2
    echo "Example: rapids-open acme-ws/inventory-api" >&2
    exit 1
fi

CONTEXT="$1"
WORKSPACE="${CONTEXT%%/*}"
PROJECT="${CONTEXT#*/}"

TOOLS_DIR="$(cd "$(dirname "$0")" && pwd)"
# aah/tools/ -> aah/ -> framework root
FRAMEWORK_ROOT="$(cd "$TOOLS_DIR/../.." && pwd)"

# Validate the project exists (uses the installed `aah` on PATH)
CONFIG_OUT=$(RAPIDS_CONTEXT="$CONTEXT" aah run core.common.config project-path 2>&1) || {
    echo "Error: could not resolve project for $CONTEXT" >&2
    echo "$CONFIG_OUT" >&2
    exit 1
}

PROJECT_PATH="$CONFIG_OUT"
echo "Opening Claude Code for: $WORKSPACE/$PROJECT"
echo "Project path: $PROJECT_PATH"

export RAPIDS_CONTEXT="$CONTEXT"
exec claude --add-dir "$FRAMEWORK_ROOT" --add-dir "$PROJECT_PATH"