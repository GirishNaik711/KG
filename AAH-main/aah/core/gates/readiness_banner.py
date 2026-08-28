#!/usr/bin/env python3
"""
Pre-flight banner for cloud and data readiness gates.

Prints a one-page reference (what the gate does, prerequisite checklist,
what it promises) to stderr before the gate executes. Stdout is untouched
so JSON output remains parseable by downstream tools.

Pure ASCII to avoid Windows cp1252 encoding issues. Suppressed via
RAPIDS_NO_BANNER=1 or after first display in the same process.
"""

import os
import sys


_BANNER_ENV_FLAG = "RAPIDS_BANNER_SHOWN"
_NO_BANNER_ENV = "RAPIDS_NO_BANNER"

WIDTH = 78
HR = "=" * WIDTH


CLOUD_BANNER = f"""
{HR}
  RAPIDS  ::  Cloud Readiness Gate
{HR}

| WHAT IT DOES
  Resolves the cloud services implied by your design decisions and proves
  each one is reachable from this machine using your existing CLI login.
  Records pass / fail / not-tested per service with latency and probe
  metadata.

| CHECKLIST
  1. Cloud CLI signed in?    aws sts get-caller-identity / gcloud auth list
  2. Region / project set?   AWS_REGION / gcloud config set project
  3. Analysis phase done?    decision registry exists
  4. Public cloud reachable? curl -I https://sts.amazonaws.com
  5. aah run installed?   aah run --help

  Tip: never paste keys. RAPIDS uses your CLI login; secrets stay in Secret Manager.

| WHAT THIS GATE PROMISES
  - Read-only. No writes, no deploys.
  - Secrets fetched from Secret Manager / Key Vault, used once, deleted.
{HR}
"""


DATA_BANNER = f"""
{HR}
  RAPIDS  ::  Data Readiness Gate
{HR}

| WHAT IT DOES
  Connects to the databases and storage that cloud readiness proved
  reachable and verifies they hold the schema and seed data your project
  needs. All checks are read-only -- no writes, no DDL, no migrations.

| CHECKLIST
  1. Cloud Readiness passed?     cloud readiness gate completed
  2. Cloud CLI signed in?        aws sts get-caller-identity / gcloud auth list
  3. Tables/datasets known?      schema files in knowledge/  OR
                                 --suggest-tables / --suggest-langsmith-datasets
  4. Read access on the data?    SELECT / s3:ListBucket / storage.objectViewer
  5. Secret IAM (if applicable)? secretsmanager:GetSecretValue / secretmanager.secretAccessor
  6. aah run installed?       aah run --help

  Tip: secret paths inherited from cloud readiness. You never paste passwords.

| WHAT THIS GATE PROMISES
  - Read-only. No DDL, migrations, INSERTs, or DELETEs.
  - Passwords fetched fresh per run, used once, deleted from memory.
{HR}
"""


def print_banner(gate: str = "cloud", force: bool = False, stream=sys.stderr) -> None:
    """
    Print the readiness banner once per process.

    Parameters
    ----------
    gate : "cloud" or "data"
    force : bypass the once-per-process and RAPIDS_NO_BANNER guards
    stream : where to write (default sys.stderr so JSON on stdout stays clean)
    """
    if not force:
        if os.environ.get(_NO_BANNER_ENV):
            return
        if os.environ.get(_BANNER_ENV_FLAG):
            return
    text = DATA_BANNER if gate == "data" else CLOUD_BANNER
    print(text, file=stream)
    os.environ[_BANNER_ENV_FLAG] = "1"


def main() -> None:
    """CLI: print the banner standalone for review."""
    import argparse
    parser = argparse.ArgumentParser(description="Print the RAPIDS readiness banner")
    parser.add_argument("--gate", choices=["cloud", "data"], default="cloud")
    args = parser.parse_args()
    print_banner(args.gate, force=True, stream=sys.stdout)


if __name__ == "__main__":
    main()
