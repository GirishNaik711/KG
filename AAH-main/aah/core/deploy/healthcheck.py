#!/usr/bin/env python3
"""
Post-deploy health and connectivity probing.

Probes deployed services with exponential backoff. Used after each layer deploys
and for final inter-service connectivity verification.

Usage:
    aah run core.deploy.healthcheck probe --url https://my-service.run.app --endpoint /health
    aah run core.deploy.healthcheck verify-connectivity --project-path <path>
"""

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import urllib.request
    import urllib.error
except ImportError:
    pass


def probe_service(
    url: str,
    endpoint: str = "/health",
    retries: int = 5,
    backoff: float = 2.0,
    timeout: int = 10,
) -> dict:
    """
    Probe a deployed service with exponential backoff.

    Returns:
        {
            "healthy": bool,
            "http_code": int | None,
            "latency_ms": float | None,
            "attempts": int,
            "error": str | None,
            "url": str,
        }
    """
    probe_url = url.rstrip("/") + endpoint
    last_error = None
    last_code = None

    for attempt in range(1, retries + 1):
        start = time.time()
        try:
            req = urllib.request.Request(probe_url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                latency = (time.time() - start) * 1000
                code = resp.status
                if 200 <= code < 400:
                    return {
                        "healthy": True,
                        "http_code": code,
                        "latency_ms": round(latency, 1),
                        "attempts": attempt,
                        "error": None,
                        "url": probe_url,
                    }
                last_code = code
                last_error = f"HTTP {code}"
        except urllib.error.HTTPError as e:
            last_code = e.code
            # 404 on /health is acceptable — service is running but no health endpoint
            if e.code == 404 and endpoint == "/health":
                latency = (time.time() - start) * 1000
                return {
                    "healthy": True,
                    "http_code": 404,
                    "latency_ms": round(latency, 1),
                    "attempts": attempt,
                    "error": "No /health endpoint (404) — service is running",
                    "url": probe_url,
                }
            last_error = f"HTTP {e.code}: {e.reason}"
        except urllib.error.URLError as e:
            last_error = f"Connection failed: {e.reason}"
        except TimeoutError:
            last_error = f"Timeout after {timeout}s"
        except Exception as e:
            last_error = str(e)

        # Exponential backoff before retry
        if attempt < retries:
            wait = backoff ** (attempt - 1)
            time.sleep(wait)

    return {
        "healthy": False,
        "http_code": last_code,
        "latency_ms": None,
        "attempts": retries,
        "error": last_error,
        "url": probe_url,
    }


def check_layer(service_urls: dict[str, str], endpoint: str = "/health") -> dict:
    """
    Healthcheck all services in a deploy layer.

    Args:
        service_urls: {service_name: deployed_url}
        endpoint: health endpoint to probe

    Returns:
        {
            "all_healthy": bool,
            "results": {service_name: probe_result}
        }
    """
    results = {}
    all_healthy = True

    for name, url in service_urls.items():
        result = probe_service(url, endpoint=endpoint)
        results[name] = result
        if not result["healthy"]:
            all_healthy = False

    return {"all_healthy": all_healthy, "results": results}


def verify_connectivity(all_service_urls: dict[str, str]) -> dict:
    """
    Verify inter-service connectivity by hitting /ready on each service.

    /ready should verify that the service can reach its dependencies
    (unlike /health which only checks if the service itself is up).

    If /ready is not implemented (404), falls back to /health and warns.

    Returns:
        {
            "all_connected": bool,
            "results": {service_name: probe_result},
            "warnings": [str]
        }
    """
    results = {}
    all_connected = True
    warnings = []

    for name, url in all_service_urls.items():
        result = probe_service(url, endpoint="/ready", retries=3, backoff=2.0)

        # If /ready returns 404, fall back to /health
        if not result["healthy"] and result["http_code"] == 404:
            warnings.append(
                f"{name}: /ready not implemented — falling back to /health. "
                "Consider adding /ready to verify inter-service connections."
            )
            result = probe_service(url, endpoint="/health", retries=3, backoff=2.0)

        results[name] = result
        if not result["healthy"]:
            all_connected = False

    return {
        "all_connected": all_connected,
        "results": results,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Post-deploy healthcheck probing")
    sub = parser.add_subparsers(dest="command", required=True)

    probe_p = sub.add_parser("probe", help="Probe a single service")
    probe_p.add_argument("--url", type=str, required=True)
    probe_p.add_argument("--endpoint", type=str, default="/health")
    probe_p.add_argument("--retries", type=int, default=5)
    probe_p.add_argument("--backoff", type=float, default=2.0)

    layer_p = sub.add_parser("check-layer", help="Healthcheck all services in a layer")
    layer_p.add_argument("--services", type=str, required=True, help="JSON: {name: url}")

    conn_p = sub.add_parser("verify-connectivity", help="Verify inter-service /ready endpoints")
    conn_p.add_argument("--services", type=str, required=True, help="JSON: {name: url}")

    args = parser.parse_args()

    if args.command == "probe":
        result = probe_service(args.url, endpoint=args.endpoint, retries=args.retries, backoff=args.backoff)
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["healthy"] else 1)

    elif args.command == "check-layer":
        service_urls = json.loads(args.services)
        result = check_layer(service_urls)
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["all_healthy"] else 1)

    elif args.command == "verify-connectivity":
        service_urls = json.loads(args.services)
        result = verify_connectivity(service_urls)
        json.dump(result, sys.stdout, indent=2)
        print()
        sys.exit(0 if result["all_connected"] else 1)


if __name__ == "__main__":
    main()
