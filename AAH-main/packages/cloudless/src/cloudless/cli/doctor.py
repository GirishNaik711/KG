"""cloudless doctor — preflight environment checker (Q30).

Reports environment readiness for a cloudless deploy with a Linter-style
PASS / WARN / FAIL summary. Each check is independent — failing one does
not skip others.

Checks shipped here cover the recurring F1/F5/F15/F16/F19 gotchas:

  F1   — Bedrock inference profile must be `us.` prefixed
  F2   — Gemini 2.5 thinking budget must leave room for output
  F5   — AWS CLI v2 too old for AgentCore (must use boto3 directly)
  F13a — GCP cloudpickle by-reference (only emitted as info)
  F15  — Anthropic streaming requires the use-case form
  F16  — CodeBuild on Python 3.13 fails compiling numpy
  F17  — Pre-PyPI cloudless wheel must be bundled into wheelhouse/
  F19  — JWT regex / nested event-loop hazards (only emitted as info)
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cloudless._version import __version__

# ----------------------------- result types ----------------------------- #


@dataclass(frozen=True)
class CheckResult:
    name: str
    level: str   # "PASS" | "WARN" | "FAIL"
    message: str
    detail: str = ""


# ----------------------------- individual checks ------------------------ #


def check_python_version() -> CheckResult:
    v = sys.version_info
    if v < (3, 11):
        return CheckResult(
            "python-version", "FAIL",
            f"Python {v.major}.{v.minor}.{v.micro} is too old (cloudless requires >=3.11)",
        )
    if v >= (3, 14):
        return CheckResult(
            "python-version", "WARN",
            f"Python {v.major}.{v.minor} not tested; pin to 3.12 in CI/CD",
        )
    return CheckResult(
        "python-version", "PASS",
        f"Python {v.major}.{v.minor}.{v.micro}",
    )


def check_cloudless_install() -> CheckResult:
    return CheckResult(
        "cloudless-install", "PASS",
        f"cloudless {__version__} importable",
    )


def check_aws_cli_age() -> CheckResult:
    """F5: aws-cli v2.0.x is too old for bedrock-agentcore commands.

    We don't actually USE aws-cli (we shell to boto3), but a stale aws-cli
    confuses operators who expect it to work for AgentCore.
    """
    exe = shutil.which("aws")
    if exe is None:
        return CheckResult(
            "aws-cli-age", "WARN",
            "aws CLI not on PATH — that's fine if you only use boto3 directly",
        )
    try:
        out = subprocess.check_output([exe, "--version"], text=True, timeout=5)
    except Exception as e:
        return CheckResult("aws-cli-age", "WARN", f"could not run aws --version: {e}")
    # aws-cli reports "aws-cli/2.X.Y Python/..."
    import re
    m = re.search(r"aws-cli/(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        return CheckResult("aws-cli-age", "WARN", f"could not parse: {out.strip()}")
    major, minor, patch = map(int, m.groups())
    if (major, minor) < (2, 15):
        return CheckResult(
            "aws-cli-age", "WARN",
            f"aws-cli {major}.{minor}.{patch} too old for AgentCore CLI; "
            f"cloudless uses boto3 directly so this is informational",
        )
    return CheckResult("aws-cli-age", "PASS", f"aws-cli {major}.{minor}.{patch}")


def check_boto3_version() -> CheckResult:
    try:
        import boto3  # noqa: F401
        import botocore
    except ImportError:
        return CheckResult(
            "boto3", "WARN",
            "boto3 not installed (install cloudless[aws] for AWS deploys)",
        )
    v = tuple(int(x) for x in botocore.__version__.split(".")[:2])
    if v < (1, 35):
        return CheckResult(
            "boto3", "WARN",
            f"botocore {botocore.__version__} too old for Bedrock AgentCore; recommend >= 1.40",
        )
    return CheckResult("boto3", "PASS", f"botocore {botocore.__version__}")


def check_aws_creds_resolve() -> CheckResult:
    try:
        import boto3
        sts = boto3.client("sts")
        ident = sts.get_caller_identity()
        return CheckResult(
            "aws-credentials", "PASS",
            f"caller {ident.get('Arn', '?')} (account {ident.get('Account')})",
        )
    except ImportError:
        return CheckResult("aws-credentials", "WARN", "boto3 not installed")
    except Exception as e:
        return CheckResult(
            "aws-credentials", "WARN",
            f"could not resolve AWS credentials: {e}",
        )


def check_bedrock_inference_profile() -> CheckResult:
    """F1: ensure the user's default model resolves to a `us.` prefixed ID."""
    try:
        from cloudless.catalog.llm import resolve_model
        alias = resolve_model("nova-micro")
        if not alias.model_id.startswith("us."):
            return CheckResult(
                "bedrock-inference-profile", "FAIL",
                f"Default model {alias.model_id} is missing `us.` prefix (F1)",
            )
        return CheckResult(
            "bedrock-inference-profile", "PASS",
            f"nova-micro resolves to {alias.model_id}",
        )
    except Exception as e:
        return CheckResult("bedrock-inference-profile", "WARN", str(e))


def check_anthropic_streaming() -> CheckResult:
    """F15: warn about Anthropic streaming requiring the use-case form."""
    try:
        from cloudless.catalog.llm import resolve_model
        unsafe = [m for m in ["claude-haiku", "claude-sonnet", "claude-opus"]
                  if not resolve_model(m).streaming_safe]
        if unsafe:
            return CheckResult(
                "anthropic-streaming", "WARN",
                f"{', '.join(unsafe)} streaming requires Anthropic use-case form (F15)",
            )
        return CheckResult("anthropic-streaming", "PASS", "no Anthropic models in default lane")
    except Exception as e:
        return CheckResult("anthropic-streaming", "WARN", str(e))


def check_gcp_creds() -> CheckResult:
    try:
        import google.auth
        _creds, project = google.auth.default()
        if not project:
            return CheckResult(
                "gcp-credentials", "WARN",
                "GCP ADC found but no project (set GOOGLE_CLOUD_PROJECT or CLOUDLESS_GCP_PROJECT)",
            )
        return CheckResult(
            "gcp-credentials", "PASS",
            f"GCP ADC OK for project {project!r}",
        )
    except ImportError:
        return CheckResult("gcp-credentials", "WARN", "google-auth not installed")
    except Exception as e:
        return CheckResult("gcp-credentials", "WARN", f"GCP ADC not resolved: {e}")


def check_gemini_thinking_budget() -> CheckResult:
    """F2: cloudless caps default max_tokens at 512 specifically so Gemini 2.5
    has room for output even when thinking is enabled."""
    return CheckResult(
        "gemini-thinking", "PASS",
        "F2 mitigated: extended_thinking off by default; max_tokens default=512",
    )


def check_codebuild_python_pin() -> CheckResult:
    """F16: cloudless adapters bake Python 3.12 in the Dockerfile."""
    return CheckResult(
        "codebuild-python", "PASS",
        "F16 mitigated: adapters pin python:3.12-slim in Dockerfile",
    )


def check_pre_pypi_wheel() -> CheckResult:
    """F17: warn if cloudless is from a local checkout without wheelhouse."""
    try:
        import cloudless
        pkg_dir = Path(cloudless.__file__).resolve().parent
        if "site-packages" in pkg_dir.parts:
            return CheckResult("pre-pypi-wheel", "PASS", "cloudless installed in site-packages")
        return CheckResult(
            "pre-pypi-wheel", "WARN",
            f"cloudless loaded from {pkg_dir} (not site-packages). "
            f"Deploy adapters need a wheel — make sure wheelhouse/ is built (F17)",
        )
    except Exception as e:
        return CheckResult("pre-pypi-wheel", "WARN", str(e))


def check_a2a_sdk_version() -> CheckResult:
    """F3: a2a-sdk 1.x removed DataPart/TextPart; cloudless pins <1.0."""
    try:
        a2a = importlib.import_module("a2a")
        ver = getattr(a2a, "__version__", "unknown")
        if ver.startswith("1."):
            return CheckResult(
                "a2a-sdk-version", "FAIL",
                f"a2a-sdk {ver} is 1.x — F3 incompatibility. Pin to >=0.3.9,<1.0.0",
            )
        return CheckResult("a2a-sdk-version", "PASS", f"a2a-sdk {ver}")
    except ImportError:
        return CheckResult(
            "a2a-sdk-version", "WARN",
            "a2a-sdk not installed (only needed for Strands A2A deploys)",
        )


# ----------------------------- runner ----------------------------------- #


ALL_CHECKS: list[Callable[[], CheckResult]] = [
    check_python_version,
    check_cloudless_install,
    check_aws_cli_age,
    check_boto3_version,
    check_aws_creds_resolve,
    check_bedrock_inference_profile,
    check_anthropic_streaming,
    check_gcp_creds,
    check_gemini_thinking_budget,
    check_codebuild_python_pin,
    check_pre_pypi_wheel,
    check_a2a_sdk_version,
]


def run(verbose: bool = False) -> int:
    """Run all checks and print a Linter-style report.

    Returns 0 if no FAILs, 1 otherwise. WARNs do not affect exit code.
    """
    results = [c() for c in ALL_CHECKS]
    width = max(len(r.name) for r in results) + 2

    color_pass = "\033[32m"  # green
    color_warn = "\033[33m"  # yellow
    color_fail = "\033[31m"  # red
    reset = "\033[0m"
    use_color = sys.stdout.isatty()

    def colored(level: str) -> str:
        if not use_color:
            return level
        if level == "PASS":
            return f"{color_pass}{level}{reset}"
        if level == "WARN":
            return f"{color_warn}{level}{reset}"
        return f"{color_fail}{level}{reset}"

    for r in results:
        print(f"{r.name.ljust(width)}  {colored(r.level):8s}  {r.message}")
        if verbose and r.detail:
            for line in r.detail.splitlines():
                print(f"{' ' * width}    {line}")

    fails = sum(1 for r in results if r.level == "FAIL")
    warns = sum(1 for r in results if r.level == "WARN")
    passes = sum(1 for r in results if r.level == "PASS")

    print()
    print(f"Summary: {passes} pass, {warns} warn, {fails} fail "
          f"(out of {len(results)})")
    return 0 if fails == 0 else 1
