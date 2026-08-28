#!/usr/bin/env python3
"""
Cloud credential validation helpers.

Validates AWS/GCP credentials are available via cloud SDK credential chains
without ever storing credentials in AAH files.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def check_aws_credentials() -> dict:
    """
    Verify AWS credentials are available via CLI.

    Uses `aws sts get-caller-identity` to validate — avoids importing boto3
    as a hard dependency.
    """
    try:
        result = subprocess.run(
            ["aws", "sts", "get-caller-identity"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            identity = json.loads(result.stdout)
            return {
                "available": True,
                "identity": {
                    "account": identity.get("Account"),
                    "arn": identity.get("Arn"),
                    "user_id": identity.get("UserId"),
                },
                "error": None,
            }
        return {
            "available": False,
            "identity": None,
            "error": f"AWS CLI returned error: {result.stderr.strip()}",
        }
    except FileNotFoundError:
        return {
            "available": False,
            "identity": None,
            "error": "AWS CLI not installed. Install from https://aws.amazon.com/cli/",
        }
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "identity": None,
            "error": "AWS credential check timed out",
        }
    except Exception as e:
        return {
            "available": False,
            "identity": None,
            "error": str(e),
        }


def check_gcp_credentials() -> dict:
    """
    Verify GCP credentials are available via gcloud CLI.

    Uses `gcloud auth print-access-token` to validate.
    """
    try:
        result = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Get project ID
            proj_result = subprocess.run(
                ["gcloud", "config", "get-value", "project"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            project = proj_result.stdout.strip() if proj_result.returncode == 0 else None
            return {
                "available": True,
                "project": project,
                "error": None,
            }
        return {
            "available": False,
            "project": None,
            "error": f"gcloud auth failed: {result.stderr.strip()}",
        }
    except FileNotFoundError:
        return {
            "available": False,
            "project": None,
            "error": "gcloud CLI not installed. Install from https://cloud.google.com/sdk/docs/install",
        }
    except subprocess.TimeoutExpired:
        return {
            "available": False,
            "project": None,
            "error": "GCP credential check timed out",
        }
    except Exception as e:
        return {
            "available": False,
            "project": None,
            "error": str(e),
        }


def get_required_aws_iam_policies() -> list[str]:
    """AWS IAM policies required for cloudless deployment."""
    return [
        "arn:aws:iam::aws:policy/BedrockAgentCoreFullAccess",
        "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess",
        "arn:aws:iam::aws:policy/AmazonS3FullAccess",
        "arn:aws:iam::aws:policy/AWSCodeBuildAdminAccess",
        "arn:aws:iam::aws:policy/CloudWatchLogsFullAccess",
    ]


def get_required_gcp_roles() -> list[str]:
    """GCP IAM roles required for cloudless deployment."""
    return [
        "roles/aiplatform.admin",
        "roles/storage.admin",
        "roles/iam.serviceAccountUser",
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Check cloud credentials")
    parser.add_argument("cloud", choices=["aws", "gcp"], help="Cloud provider to check")
    args = parser.parse_args()

    if args.cloud == "aws":
        result = check_aws_credentials()
    else:
        result = check_gcp_credentials()

    json.dump(result, sys.stdout, indent=2)
    print()
    sys.exit(0 if result["available"] else 1)


if __name__ == "__main__":
    main()
