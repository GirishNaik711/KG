#!/usr/bin/env bash
# aah install script.
#
# Installs prerequisites (Python 3.12, Node.js, uv, GitHub CLI, AWS CLI,
# Claude Code), then installs AAH itself from the harness source shipped
# ALONGSIDE this script.
#
# This installer lives in <harness>/installers/, so the source root is simply
# its parent folder — nothing is downloaded and no repository is contacted.
# AAH is installed as a global uv tool into uv's managed environment.
# The unzipped folder can be deleted after install.
#
#   Install source : the folder containing this script's parent pyproject.toml
#   Installed to   : managed by uv (typically ~/.local/share/uv/tools/aah)
#   Scope          : global (links into ~/.claude)
#
# Usage:
#   ./installers/AAH-Dependency-Installer.sh                      # normal install
#   ./installers/AAH-Dependency-Installer.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export UV_LINK_MODE="${UV_LINK_MODE:-copy}"



# Helper functions
log() { echo "==> $1"; }
warn() { echo "warning: $1" >&2; }

# ================================================================
#  UPFRONT: Ask user for installation preferences
# ================================================================
echo ""
echo "=============================================="
echo "  AAH (Ascend Agentic Harness) Installer"
echo "=============================================="
echo ""

# --- Install plan (nothing to ask: there is exactly one install shape) ---
echo "  ---- AAH Install Plan ----"
echo ""
if [ -f "$REPO_ROOT/pyproject.toml" ]; then
  echo "    Source (this download) : $REPO_ROOT"
else
  echo "    Source (this download) : NOT FOUND next to this script"
  echo "      Run the installer from the unzipped harness folder:"
  echo "      ascend-agentic-harness-main/installers/"
fi
echo "    Install                : global uv tool"
echo "    Links into             : $HOME/.claude"
echo ""
echo "    No repository is contacted - AAH is installed from the files you"
echo "    just unzipped."
echo ""

# --- Detect existing installation ---
echo "  ---- Current Installation ----"
echo ""
EXISTING_BINARY=""
EXISTING_SOURCE=""

if command -v aah >/dev/null 2>&1; then
  EXISTING_BINARY="$(command -v aah)"
  # Try to get source path via 'aah path' — returns the package dir (e.g., .../aah)
  # We want the repo root (one level up if path ends with /aah)
  if _aah_pkg_path="$(aah path 2>/dev/null)"; then
    if [ -f "$_aah_pkg_path/../pyproject.toml" ]; then
      EXISTING_SOURCE="$(cd "$_aah_pkg_path/.." && pwd)"
    else
      EXISTING_SOURCE="$_aah_pkg_path"
    fi
  else
    EXISTING_SOURCE=""
  fi
fi

# Fallback: check uv tool list
if [ -z "$EXISTING_SOURCE" ] && command -v uv >/dev/null 2>&1; then
  # No match is the expected result on a fresh install. Neutralize grep's
  # exit code so `set -euo pipefail` does not terminate the installer.
  uv_aah_line="$(uv tool list 2>/dev/null | grep -i 'aah' | head -1 || true)"
  if [ -n "$uv_aah_line" ]; then
    EXISTING_BINARY="${EXISTING_BINARY:-$(command -v aah 2>/dev/null)}"
  fi
fi

if [ -n "$EXISTING_BINARY" ] || [ -n "$EXISTING_SOURCE" ]; then
  echo "    AAH is currently installed:"
  [ -n "$EXISTING_BINARY" ] && echo "      Binary : $EXISTING_BINARY"
  [ -n "$EXISTING_SOURCE" ] && echo "      Source : $EXISTING_SOURCE"
  echo ""
else
  echo "    No existing AAH installation detected."
  echo ""
fi

# No "which directory?" question: the install location is fixed

# --- Claude Code detection ---
echo "  ---- Claude Code ----"
if command -v claude >/dev/null 2>&1; then
  claude_ver="$(claude --version 2>&1 | head -1)"
  echo "  Claude Code already installed: $claude_ver"
else
  echo "  Claude Code not detected - will install."
fi
echo ""

echo "----------------------------------------------"
echo "  AAH install : global uv tool"
echo "----------------------------------------------"
echo ""

# ================================================================
#  PREREQUISITES: Install or verify required tools
# ================================================================
OS="$(uname -s)"
ARCH="$(uname -m)"

# Apple Silicon Macs: Homebrew lives at /opt/homebrew, not /usr/local
if [ "$OS" = "Darwin" ]; then
    if [ -d "/opt/homebrew/bin" ]; then
        export PATH="/opt/homebrew/bin:$PATH"
    fi
fi

# --- Homebrew (macOS only) ---
if [ "$OS" = "Darwin" ]; then
    if ! command -v brew >/dev/null 2>&1; then
        log "Homebrew not found — installing (required for macOS package management)..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        # Source brew into current session
        if [ -d "/opt/homebrew/bin" ]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [ -x "/usr/local/bin/brew" ]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        if ! command -v brew >/dev/null 2>&1; then
            warn "Homebrew installation failed. Some tools may need manual installation."
        else
            log "Homebrew installed successfully."
        fi
    fi
    # One-time brew update to avoid stale formula errors
    if command -v brew >/dev/null 2>&1; then
        log "updating Homebrew formulas..."
        brew update --quiet || warn "brew update failed — continuing with existing formulas"
    fi
fi

# --- Git ---
log "checking for Git..."
if command -v git >/dev/null 2>&1; then
  log "git found: $(git --version)"
else
  log "Git not found — attempting to install..."
  case "$OS" in
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq && sudo apt-get install -y -qq git
      elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y git
      elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y git
      else
        echo "error: cannot auto-install Git. Install manually and re-run." >&2; exit 1
      fi
      ;;
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        brew install git
      else
        echo "error: Homebrew not found. Install Git manually (https://git-scm.com/) and re-run." >&2; exit 1
      fi
      ;;
    *) echo "error: cannot auto-install Git on $OS. Install manually and re-run." >&2; exit 1 ;;
  esac
  if command -v git >/dev/null 2>&1; then
    log "git installed: $(git --version)"
  else
    echo "error: Git installation failed. Install manually and re-run." >&2; exit 1
  fi
fi

# --- uv ---
# Install uv before Python so it can provide the same managed Python version on
# Ubuntu, Fedora, and macOS without relying on each OS package repository.
#
# ~/.local/bin must be on PATH BEFORE the detection check below. uv installs
# there, but that directory is added to PATH by ~/.profile / ~/.bashrc — neither
# of which is sourced when this script runs as a non-login, non-interactive
# shell (the normal `./installers/...sh` case). Checking first would then miss
# an existing uv and reinstall it on every run.
export PATH="$HOME/.local/bin:$PATH"

log "checking for uv..."
if command -v uv >/dev/null 2>&1; then
  log "uv found: $(uv --version)"
else
  log "uv not found — installing..."
  if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
      warn "uv install failed on first attempt — retrying..."
      sleep 2
      if ! curl -LsSf https://astral.sh/uv/install.sh | sh; then
          echo "error: uv installation failed. Install manually: https://docs.astral.sh/uv/" >&2
          exit 1
      fi
  fi
  # Pick up the freshly installed binary within this same shell.
  hash -r 2>/dev/null || true
fi
if command -v uv >/dev/null 2>&1; then
  log "uv available: $(uv --version)"
else
  echo "error: uv installation failed. Install manually: https://docs.astral.sh/uv/" >&2
  exit 1
fi

# --- Python 3.12+ ---
REQUIRED_PYTHON_MAJOR=3
REQUIRED_PYTHON_MINOR=12
REQUIRED_PYTHON_VERSION="${REQUIRED_PYTHON_MAJOR}.${REQUIRED_PYTHON_MINOR}"

log "checking for Python ${REQUIRED_PYTHON_VERSION}+..."
install_python_needed=true

if command -v python3 >/dev/null 2>&1; then
  PY_VER="$(python3 --version 2>&1 | awk '{print $2}')"
  PY_MAJOR="$(echo "$PY_VER" | cut -d. -f1)"
  PY_MINOR="$(echo "$PY_VER" | cut -d. -f2)"
  if [ "$PY_MAJOR" -gt "$REQUIRED_PYTHON_MAJOR" ] || \
     { [ "$PY_MAJOR" -eq "$REQUIRED_PYTHON_MAJOR" ] && [ "$PY_MINOR" -ge "$REQUIRED_PYTHON_MINOR" ]; }; then
    log "python3 found: $PY_VER"
    install_python_needed=false
  else
    warn "python3 found ($PY_VER) but version ${REQUIRED_PYTHON_VERSION}+ is required."
  fi
fi

if [ "$install_python_needed" = true ]; then
  log "installing Python ${REQUIRED_PYTHON_VERSION} via uv..."
  uv python install --default "$REQUIRED_PYTHON_VERSION"

  if UV_PYTHON_BIN="$(uv python find "$REQUIRED_PYTHON_VERSION" 2>/dev/null)" && \
     [ -x "$UV_PYTHON_BIN" ]; then
    log "Python installed: $($UV_PYTHON_BIN --version 2>&1) ($UV_PYTHON_BIN)"
  else
    echo "error: uv could not install or locate Python ${REQUIRED_PYTHON_VERSION}." >&2
    exit 1
  fi
fi

# --- Node.js (LTS 22+) ---
REQUIRED_NODE_MAJOR=22

log "checking for Node.js ${REQUIRED_NODE_MAJOR}+..."
install_node_needed=true

if command -v node >/dev/null 2>&1; then
  NODE_VER="$(node --version 2>&1 | sed 's/^v//')"
  NODE_MAJOR="$(echo "$NODE_VER" | cut -d. -f1)"
  if [ "$NODE_MAJOR" -ge "$REQUIRED_NODE_MAJOR" ]; then
    log "node found: v$NODE_VER"
    install_node_needed=false
  else
    warn "node found (v$NODE_VER) but version ${REQUIRED_NODE_MAJOR}+ is required."
  fi
fi

if [ "$install_node_needed" = true ]; then
  log "Node.js not found — attempting to install..."
  case "$OS" in
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        curl -fsSL https://deb.nodesource.com/setup_${REQUIRED_NODE_MAJOR}.x | sudo -E bash -
        sudo apt-get install -y -qq nodejs
      elif command -v dnf >/dev/null 2>&1; then
        curl -fsSL https://rpm.nodesource.com/setup_${REQUIRED_NODE_MAJOR}.x | sudo bash -
        sudo dnf install -y nodejs
      else
        warn "Cannot auto-install Node.js. Install Node.js ${REQUIRED_NODE_MAJOR}+ manually: https://nodejs.org/"
      fi
      ;;
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        brew install node@${REQUIRED_NODE_MAJOR}
      else
        warn "Homebrew not found. Install Node.js ${REQUIRED_NODE_MAJOR}+ manually: https://nodejs.org/"
      fi
      ;;
    *) warn "Cannot auto-install Node.js on $OS. Install Node.js ${REQUIRED_NODE_MAJOR}+ manually." ;;
  esac

  if command -v node >/dev/null 2>&1; then
    log "node installed: $(node --version)"
    # Package-manager Node points npm's global prefix at a root-owned path
    # (e.g. /usr/lib/node_modules). Nothing below needs `npm install -g`, but
    # say so once here so a later manual `-g` install is not a surprise.
    npm_root="$(npm root -g 2>/dev/null || echo "")"
    if [ -n "$npm_root" ] && [ ! -w "$(dirname "$npm_root")" ]; then
      log "note: npm's global dir ($npm_root) is root-owned; 'npm install -g' will need sudo or a user prefix."
    fi
  else
    warn "Node.js installation could not be verified. Claude Code requires Node.js."
  fi
fi

# --- Claude Code ---
# Prefer the official native installer (same source the PowerShell installer
# uses). It writes to ~/.local/bin, so it needs no sudo and is immune to the
# root-owned npm global prefix that system-package Node (apt/dnf/NodeSource)
# creates — a bare `npm install -g` there fails with EACCES for a non-root user.
# npm is kept as a fallback for environments where claude.ai is unreachable but
# an internal registry is available; there it is pointed at a user-writable
# prefix for the same reason.
CLAUDE_AVAILABLE=false
log "checking for Claude Code..."
if command -v claude >/dev/null 2>&1; then
  log "Claude Code found: $(claude --version 2>&1 | head -1)"
  CLAUDE_AVAILABLE=true
else
  log "Claude Code not found — installing via official installer..."

  # Every install path below is failure-tolerant: Claude Code is required for
  # AAH but its absence must not abort the harness install under `set -e`, so
  # the user still gets uv/Python/.claude wiring and a clear remediation line.
  if curl -fsSL https://claude.ai/install.sh 2>/dev/null | bash; then
    export PATH="$HOME/.local/bin:$PATH"
    hash -r 2>/dev/null || true
  else
    warn "official installer could not be reached — falling back to npm..."
  fi

  if ! command -v claude >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    npm_prefix="$(npm config get prefix 2>/dev/null || echo "")"
    # A prefix we cannot write to is the EACCES case — relocate npm's globals
    # into the user's home instead of asking for sudo. Walk up to the nearest
    # existing ancestor: node_modules/ may legitimately not exist yet, in which
    # case the parent that npm would create it in is what must be writable.
    npm_probe="$npm_prefix/lib/node_modules"
    while [ -n "$npm_probe" ] && [ ! -e "$npm_probe" ] && [ "$npm_probe" != "/" ]; do
      npm_probe="$(dirname "$npm_probe")"
    done
    if [ -n "$npm_prefix" ] && [ ! -w "$npm_probe" ]; then
      warn "npm global prefix ($npm_prefix) is not writable — using $HOME/.npm-global instead."
      npm config set prefix "$HOME/.npm-global" >/dev/null 2>&1 || true
      export PATH="$HOME/.npm-global/bin:$PATH"
    fi
    npm install -g @anthropic-ai/claude-code || \
      warn "npm install of Claude Code failed."
    hash -r 2>/dev/null || true
  fi

  if command -v claude >/dev/null 2>&1; then
    log "Claude Code installed: $(claude --version 2>&1 | head -1)"
    CLAUDE_AVAILABLE=true
  else
    warn "Claude Code download is restricted on this network."
    warn "Please contact your ITS team to unblock: https://downloads.claude.ai"
    warn "Once unblocked, re-run this installer or install manually:"
    warn "    curl -fsSL https://claude.ai/install.sh | bash"
    warn "Continuing with AAH install..."
  fi
fi


# --- GitHub CLI (gh) ---
log "checking for GitHub CLI (gh) — required for issue sync and repo access"
if command -v gh >/dev/null 2>&1; then
  echo "   gh CLI already installed: $(gh --version 2>&1 | head -1)"
else
  echo "   GitHub CLI (gh) not found — attempting to install..."

  install_gh_linux() {
    if command -v apt-get >/dev/null 2>&1; then
      echo "   Installing gh via apt..."
      if ! sudo -n true 2>/dev/null; then
        echo "   Sudo access required. Please enter your password."
        sudo -v
      fi
      # Add GitHub CLI official repository
      sudo mkdir -p -m 755 /etc/apt/keyrings
      curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null
      sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
      sudo apt-get update -qq
      sudo apt-get install -y -qq gh
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y gh
    elif command -v yum >/dev/null 2>&1; then
      sudo yum install -y gh
    else
      warn "Could not auto-install gh. Install manually: https://cli.github.com/"
      return 1
    fi
  }

  install_gh_macos() {
    if command -v brew >/dev/null 2>&1; then
      brew install gh
    else
      warn "Homebrew not found. Install gh manually: https://cli.github.com/"
      return 1
    fi
  }

  case "$OS" in
    Linux) install_gh_linux || true ;;
    Darwin) install_gh_macos || true ;;
    *) warn "gh auto-install not supported on $OS. Install manually: https://cli.github.com/" ;;
  esac

  if command -v gh >/dev/null 2>&1; then
    echo "   gh CLI installed: $(gh --version 2>&1 | head -1)"
  else
    warn "gh CLI not found after install attempt. Issue sync features will be unavailable."
    warn "Install manually: https://cli.github.com/"
  fi
fi

# No GitHub authentication step: AAH is installed from the source shipped in
# this download, so the installer never contacts a repository. `gh` is provided
# only for the user's own project repos.

# --- AWS CLI ---
install_aws_cli_linux() {
  local aws_arch aws_tmp_dir

  case "$ARCH" in
    x86_64|amd64) aws_arch="x86_64" ;;
    aarch64|arm64) aws_arch="aarch64" ;;
    *)
      warn "AWS CLI auto-install is not supported on Linux architecture: $ARCH"
      return 1
      ;;
  esac

  if ! command -v unzip >/dev/null 2>&1; then
    log "unzip is required by the AWS CLI installer — installing it..."
    if command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update -qq && sudo apt-get install -y -qq unzip
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y unzip
    elif command -v yum >/dev/null 2>&1; then
      sudo yum install -y unzip
    else
      warn "Cannot install unzip automatically. Install it and re-run."
      return 1
    fi
  fi

  aws_tmp_dir="$(mktemp -d)"
  if ! curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-${aws_arch}.zip" \
      -o "$aws_tmp_dir/awscliv2.zip"; then
    rm -rf "$aws_tmp_dir"
    return 1
  fi
  if ! unzip -q "$aws_tmp_dir/awscliv2.zip" -d "$aws_tmp_dir"; then
    rm -rf "$aws_tmp_dir"
    return 1
  fi

  if [ "$(id -u)" -eq 0 ]; then
    if [ -d /usr/local/aws-cli ]; then
      "$aws_tmp_dir/aws/install" --update
    else
      "$aws_tmp_dir/aws/install"
    fi
  elif command -v sudo >/dev/null 2>&1; then
    if [ -d /usr/local/aws-cli ]; then
      sudo "$aws_tmp_dir/aws/install" --update
    else
      sudo "$aws_tmp_dir/aws/install"
    fi
  else
    mkdir -p "$HOME/.local/bin"
    if [ -d "$HOME/.local/aws-cli" ]; then
      "$aws_tmp_dir/aws/install" --install-dir "$HOME/.local/aws-cli" \
        --bin-dir "$HOME/.local/bin" --update
    else
      "$aws_tmp_dir/aws/install" --install-dir "$HOME/.local/aws-cli" \
        --bin-dir "$HOME/.local/bin"
    fi
    export PATH="$HOME/.local/bin:$PATH"
  fi

  rm -rf "$aws_tmp_dir"
}

install_aws_cli_macos() {
  local aws_tmp_dir

  if command -v brew >/dev/null 2>&1; then
    brew install awscli
    return
  fi

  log "Homebrew not found — installing the official AWS CLI package..."
  aws_tmp_dir="$(mktemp -d)"
  if ! curl -fsSL "https://awscli.amazonaws.com/AWSCLIV2.pkg" \
      -o "$aws_tmp_dir/AWSCLIV2.pkg"; then
    rm -rf "$aws_tmp_dir"
    return 1
  fi
  if [ "$(id -u)" -eq 0 ]; then
    installer -pkg "$aws_tmp_dir/AWSCLIV2.pkg" -target /
  elif command -v sudo >/dev/null 2>&1; then
    sudo installer -pkg "$aws_tmp_dir/AWSCLIV2.pkg" -target /
  else
    rm -rf "$aws_tmp_dir"
    warn "Administrator access is required to install the AWS CLI package."
    return 1
  fi
  rm -rf "$aws_tmp_dir"
}

log "checking for AWS CLI..."
AWS_CLI_NEEDS_INSTALL=true
AWS_CLI_VERSION=""
if command -v aws >/dev/null 2>&1; then
  AWS_CLI_VERSION="$(aws --version 2>&1 | head -1)"
  AWS_CLI_RESOLVED_PATH="$(readlink -f "$(command -v aws)" 2>/dev/null || command -v aws)"
  case "$AWS_CLI_RESOLVED_PATH" in
    *"/uv/tools/"*|*"/.local/share/uv/"*) ;;
    *)
      if [[ "$AWS_CLI_VERSION" == aws-cli/2.* ]]; then
        AWS_CLI_NEEDS_INSTALL=false
      fi
      ;;
  esac
fi

if [ "$AWS_CLI_NEEDS_INSTALL" = false ]; then
  log "AWS CLI found: $AWS_CLI_VERSION"
else
  if [ -n "$AWS_CLI_VERSION" ]; then
    log "replacing the uv-managed or non-v2 AWS CLI with the native package..."
  else
    log "AWS CLI not found — installing the native package..."
  fi
  case "$OS" in
    Linux) install_aws_cli_linux ;;
    Darwin) install_aws_cli_macos ;;
    *) warn "AWS CLI auto-install is not supported on $OS." ;;
  esac

  if command -v aws >/dev/null 2>&1; then
    log "AWS CLI installed: $(aws --version 2>&1 | head -1)"
  else
    echo "error: AWS CLI installation could not be verified. Install it and re-run." >&2
    exit 1
  fi
fi

# ================================================================
#  TEARDOWN: purge existing install before reinstalling
# ================================================================
# Required because:
# - uv tool install --force fails with OS error 32 if aah is locked
# - A prior copy-fallback install leaves non-link files the linker refuses to clobber
# Best-effort: the fresh install must proceed regardless of teardown outcome.
if command -v aah >/dev/null 2>&1; then
  log "existing aah detected — tearing down before reinstall"
  pkill -f '/aah$' 2>/dev/null || true
  sleep 2
  aah uninstall --purge || warn "aah uninstall --purge failed — continuing with reinstall"

  # NOTE: we intentionally do NOT delete ~/.claude/agents or ~/.claude/skills.
  # `aah uninstall --purge` already removes exactly our own links/files by name
  # (see linkutil.unlink_if_ours); blowing away the whole directory would also
  # destroy the user's own agents/skills. The linker refreshes our entries in
  # place on reinstall, so no manual teardown of those dirs is needed.

  echo
fi

# ================================================================
#  AAH HARNESS: global install from the shipped source
# ================================================================
# The source root is this script's parent — nothing is downloaded and no
# repository is contacted. uv packages everything into its managed environment.
# The unzipped folder can be deleted after install.
if [ ! -f "$REPO_ROOT/pyproject.toml" ]; then
  echo "error: could not find the AAH source next to this installer." >&2
  echo "       Expected pyproject.toml in: $REPO_ROOT" >&2
  echo "       Run this script from the installers/ folder of the unzipped" >&2
  echo "       harness download (ascend-agentic-harness-main/installers/)." >&2
  exit 1
fi
log "harness source: $REPO_ROOT"

# --- Install as a global uv tool (non-editable) ----------------------------
INSTALL_SRC="$REPO_ROOT"
if command -v cygpath >/dev/null 2>&1; then
  INSTALL_SRC="$(cygpath -m "$INSTALL_SRC")"
fi

log "installing aah (global) from $REPO_ROOT"
_install_stderr="$(mktemp)"
if ! uv tool install --force --reinstall "${INSTALL_SRC}[cloud,gcp]" \
      2> >(tee "$_install_stderr" >&2); then
  if grep -qi 'does not satisfy' "$_install_stderr" 2>/dev/null && \
     grep -qE 'Python.*>=.*3\.11' "$_install_stderr" 2>/dev/null; then
    echo "" >&2
    echo "========================================================================" >&2
    echo "  uv resolved a Python version that doesn't meet the required >=3.11." >&2
    echo "  Fix it with:" >&2
    echo "" >&2
    echo "    uv python install 3.12" >&2
    echo "" >&2
    echo "  then re-run this installer. 'uv python list' shows what uv detects." >&2
    echo "========================================================================" >&2
  else
    echo "error: AAH install failed — see the output above." >&2
  fi
  rm -f "$_install_stderr"
  exit 1
fi
rm -f "$_install_stderr"

echo
log "verifying aah is on PATH"
if [ -d "$HOME/.local/bin" ]; then
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) : ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
  esac
fi
if ! command -v aah >/dev/null 2>&1; then
  warn "'aah' is not on PATH after install."
  warn "add uv's tool bin dir to PATH (run 'uv tool update-shell')."
  exit 1
fi
echo "ok: $(command -v aah)"
echo

# Link skills, agents, and hooks into ~/.claude
log "linking skills/agents/hooks (aah setup)"
aah setup --platform claude || warn "aah setup reported an issue — run 'aah setup' manually"

# Persist ~/.local/bin in the user's shell profile so `aah` works in new terminals
log "updating shell PATH (uv tool update-shell)"
uv tool update-shell 2>/dev/null || warn "could not update shell PATH — add ~/.local/bin to your PATH manually"
echo


# ================================================================
#  Verify codemap and liteparse
# ================================================================
AAH_PY=""
if command -v aah >/dev/null 2>&1; then
  AAH_PY="$(head -1 "$(command -v aah)" | sed 's/^#!//')"
  [ -x "$AAH_PY" ] || AAH_PY=""
fi

if command -v codemap >/dev/null 2>&1; then
  echo "   codemap CLI: $(command -v codemap)"
else
  warn "'codemap' CLI not on PATH (skills/agents call it directly)."
fi

if [ -n "$AAH_PY" ]; then
  if CODEMAP_VER="$("$AAH_PY" -c "import codemap_scale; print(getattr(codemap_scale,'__version__','?'))" 2>/dev/null)"; then
    echo "   codemap-scale v${CODEMAP_VER} importable from the aah environment"
  else
    warn "codemap-scale not importable from the aah environment. Re-run this installer to reinstall."
  fi
fi

echo "==> verifying liteparse (knowledge base document parsing)"
if [ -n "$AAH_PY" ] && "$AAH_PY" -c "import liteparse" 2>/dev/null; then
  echo "   liteparse importable from the aah environment"
else
  warn "liteparse not importable. Binary-document parsing (PDF, DOCX, PPTX) will be unavailable."
fi


# ================================================================
#  FINAL: Run the harness install command (last step)
# ================================================================
echo ""
echo "=============================================="
echo "  AAH Installation Complete"
echo "=============================================="
echo ""
echo "  Install type : global uv tool (links into ~/.claude)"
echo ""

# --- Verification ---
# Report what is actually on disk rather than assuming success. `aah` and
# `claude` land in ~/.local/bin, which the user's CURRENT shell may not have on
# PATH yet (it is added by ~/.profile / ~/.bashrc at shell startup), so a
# missing command here usually means "reload the shell", not "install failed".
# Distinguish those two cases explicitly so a successful install is never
# mistaken for a broken one.
echo "  ---- Verifying Installation ----"
echo ""

verify_ok=true
needs_shell_reload=false

# Resolve a command, falling back to the known install dir so we can tell a
# PATH problem apart from a genuinely missing binary.
check_component() {
  local name="$1" cmd="$2" version_flag="${3:---version}"
  local resolved="" version=""

  if command -v "$cmd" >/dev/null 2>&1; then
    resolved="$(command -v "$cmd")"
  elif [ -x "$HOME/.local/bin/$cmd" ]; then
    resolved="$HOME/.local/bin/$cmd"
    needs_shell_reload=true
  fi

  if [ -z "$resolved" ]; then
    printf "    [MISS] %-14s not found\n" "$name"
    verify_ok=false
    return
  fi

  # Present but non-functional is a real failure, so gate on the exit code
  # rather than reporting [OK] for whatever the command happened to print.
  if version="$("$resolved" "$version_flag" 2>&1 | head -1)" && \
     "$resolved" "$version_flag" >/dev/null 2>&1; then
    printf "    [OK]   %-14s %s\n" "$name" "${version:-installed}"
  else
    printf "    [WARN] %-14s found at %s but not responding\n" "$name" "$resolved"
    verify_ok=false
  fi
}

# `aah` has no --version flag; `path` is its cheapest successful invocation.
check_component "aah" "aah" "path"
# Skip Claude Code verification if it was restricted (already reported above)
if [ "$CLAUDE_AVAILABLE" = true ]; then
  check_component "Claude Code" "claude" "--version"
fi
check_component "uv" "uv" "--version"

claude_dir="$HOME/.claude"
if [ -d "$claude_dir/skills" ] && [ -d "$claude_dir/agents" ]; then
  printf "    [OK]   %-14s %s\n" "AAH assets" "$claude_dir (skills + agents linked)"
else
  printf "    [MISS] %-14s %s\n" "AAH assets" "$claude_dir incomplete"
  verify_ok=false
fi

echo ""

if [ "$needs_shell_reload" = true ]; then
  echo "  NOTE: Installed correctly, but this shell has a stale PATH."
  echo "        Open a new terminal to pick up the changes."
  echo ""
elif [ "$verify_ok" = false ]; then
  echo "  Some components could not be installed (restricted network)."
  echo "  Contact your ITS team to unblock restricted downloads, then"
  echo "  re-run this installer or see: installers/AAH-Dependency-Installer-Guide.md (Troubleshooting)"
  echo ""
fi

echo "  +============================================================================+"
echo "  |                                                                            |"
echo "  |        !!  IMPORTANT - PLEASE READ BEFORE USING AAH  !!                   |"
echo "  |                                                                            |"
echo "  |   Warning: AAH uses the Playwright MCP tool to validate and test the       |"
echo "  |   frontend components of the application you build. Playwright works by    |"
echo "  |   capturing your screen. These captures must not be shared outside the     |"
echo "  |   team without thorough review. Do not pass client data through this       |"
echo "  |   workflow without explicit approval from your LCSP / QRM team.            |"
echo "  |                                                                            |"
echo "  +============================================================================+"
echo ""

echo "  NEXT STEPS:"
echo "    1. Open a new terminal"
echo "    2. Confirm AAH is working:"
echo "         aah status"
echo "    3. Navigate to your project folder"
echo "    4. Start Claude Code:"
echo "         claude"
echo "    5. Initialize your project:"
echo "         /aah-init-project"
echo ""
echo "done."
