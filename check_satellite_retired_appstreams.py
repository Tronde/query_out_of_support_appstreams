#!/usr/bin/env python3
"""
check_satellite_retired_appstreams.py

1. Authenticates to the Red Hat SSO and queries the Lightspeed Planning API
   for all AppStream module streams whose support_status is "Retired"
   (optionally also "Near retirement").
2. Paginates through every content host registered in Red Hat Satellite.
3. Queries each host's module streams in parallel via Satellite's REST API.
4. Cross-references the results against the retired set and reports hosts
   that have retired AppStreams enabled or installed.

Authentication:
  Lightspeed – Red Hat service account (client credentials OAuth2).
               Set RH_CLIENT_ID and RH_CLIENT_SECRET environment variables.
  Satellite   – HTTP basic auth.
               Set SAT_USER and SAT_PASSWORD environment variables.

Prerequisites:
  pip install requests

Usage:
  export RH_CLIENT_ID="<client-id>"
  export RH_CLIENT_SECRET="<client-secret>"
  export SAT_USER="<username>"
  export SAT_PASSWORD="<password>"

  python3 check_satellite_retired_appstreams.py [options]

Options:
  --sat-url URL             Satellite base URL (default: https://sat-1.lab1.nat)
  --include-near-retirement Also flag streams with status "Near retirement"
  --major VERSION           Filter Lightspeed query to one RHEL major version
  --workers N               Parallel host-query threads (default: 5)
  --output-format FORMAT    Output format: table (default) or json
  --verify                  Enable SSL certificate verification (disabled by
                            default for lab environments with self-signed certs)
  --help                    Show this help message and exit
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib3.exceptions import InsecureRequestWarning

try:
    import requests
    import urllib3
except ImportError:
    print(
        "ERROR: 'requests' library is required. Install it with: pip install requests",
        file=sys.stderr,
    )
    sys.exit(1)

# ── Constants ────────────────────────────────────────────────────────────────
SSO_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
LIGHTSPEED_BASE = "https://console.redhat.com/api/roadmap/v1"


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check Satellite content hosts for retired AppStream module streams."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sat-url",
        default=os.environ.get("SAT_URL", "https://sat-1.lab1.nat"),
        help="Satellite base URL (default: https://sat-1.lab1.nat)",
    )
    parser.add_argument(
        "--include-near-retirement",
        action="store_true",
        default=False,
        help="Also flag streams with status 'Near retirement'",
    )
    def positive_int(value: str) -> int:
        try:
            intval = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"--major must be a positive integer, got: {value!r}")
        if intval < 1:
            raise argparse.ArgumentTypeError(f"--major must be a positive integer, got: {intval}")
        return intval

    parser.add_argument(
        "--major",
        type=positive_int,
        metavar="VERSION",
        help="Filter Lightspeed query to one RHEL major version (e.g. 8 or 9)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=5,
        metavar="N",
        help="Number of parallel threads for host queries (default: 5)",
    )
    parser.add_argument(
        "--output-format",
        choices=["table", "json"],
        default="table",
        help="Output format: table (default) or json",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        default=False,
        help=(
            "Enable SSL certificate verification. "
            "Disabled by default for lab environments with self-signed certificates."
        ),
    )
    return parser.parse_args()


# ── Lightspeed helpers (reused from PoC) ────────────────────────────────────
def get_access_token(client_id: str, client_secret: str) -> str:
    """Obtain a short-lived Bearer token from Red Hat SSO."""
    print("Authenticating with Red Hat SSO...", file=sys.stderr)
    try:
        resp = requests.post(
            SSO_URL,
            data={
                "grant_type": "client_credentials",
                "scope": "api.console",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as exc:
        print(f"ERROR: SSO request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(
            f"ERROR: Authentication failed (HTTP {resp.status_code}).\n"
            f"Response: {resp.text}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        payload = resp.json()
    except ValueError:
        print(f"ERROR: SSO returned non-JSON response: {resp.text[:200]}", file=sys.stderr)
        sys.exit(1)

    token = payload.get("access_token")
    if not token:
        print(f"ERROR: No access_token in SSO response: {payload}", file=sys.stderr)
        sys.exit(1)

    print(
        f"SSO authentication successful (token valid for {payload.get('expires_in', '?')} seconds).",
        file=sys.stderr,
    )
    return token


def fetch_retired_appstreams(
    token: str,
    major: Optional[int],
    include_near_retirement: bool,
) -> list[dict]:
    """
    Query the Lightspeed lifecycle catalog for all AppStream module streams,
    then filter for Retired (and optionally Near retirement) status.

    Returns a list of dicts with keys:
        module_name, stream_version, display_name, os_major,
        end_date, support_status
    """
    if major is not None:
        url = f"{LIGHTSPEED_BASE}/lifecycle/app-streams/{major}"
    else:
        url = f"{LIGHTSPEED_BASE}/lifecycle/app-streams"

    print(f"Querying Lightspeed catalog: GET {url}", file=sys.stderr)

    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
    except requests.exceptions.RequestException as exc:
        print(f"ERROR: Lightspeed API request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if resp.status_code != 200:
        print(
            f"ERROR: Lightspeed API returned HTTP {resp.status_code}.\n"
            f"Response: {resp.text}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        data = resp.json().get("data", [])
    except ValueError:
        print(f"ERROR: Lightspeed API returned non-JSON response: {resp.text[:200]}", file=sys.stderr)
        sys.exit(1)

    target_statuses = {"Retired"}
    if include_near_retirement:
        target_statuses.add("Near retirement")

    label = " / ".join(sorted(target_statuses))
    print(f"Filtering for status: {label}", file=sys.stderr)

    retired: list[dict] = []
    for entry in data:
        if entry.get("support_status") not in target_statuses:
            continue

        display_name = entry.get("display_name", "")
        # Extract stream version as the last whitespace-separated token of
        # display_name (e.g. "Node.js 16" → "16", "Python 3.6" → "3.6").
        parts = display_name.split()
        stream_version = parts[-1] if parts else ""

        retired.append(
            {
                "module_name":    entry.get("name", ""),
                "stream_version": stream_version,
                "display_name":   display_name,
                "os_major":       entry.get("os_major"),
                "end_date":       entry.get("end_date", "?"),
                "support_status": entry.get("support_status", ""),
            }
        )

    print(
        f"Retired AppStreams found in Lightspeed catalog: {len(retired)}",
        file=sys.stderr,
    )
    return retired


# ── Satellite helpers ────────────────────────────────────────────────────────
class SatelliteAPIError(Exception):
    """Raised when a Satellite REST API call fails."""


def sat_get(
    sat_url: str,
    path: str,
    sat_auth: tuple[str, str],
    verify: bool,
    params: Optional[dict] = None,
) -> dict:
    """GET request against the Satellite REST API."""
    url = f"{sat_url}{path}"
    try:
        resp = requests.get(
            url,
            auth=sat_auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            params=params,
            verify=verify,
            timeout=60,
        )
    except requests.exceptions.RequestException as exc:
        raise SatelliteAPIError(f"Request to {url} failed: {exc}") from exc

    if resp.status_code not in (200, 201):
        raise SatelliteAPIError(
            f"HTTP {resp.status_code} for {url}: {resp.text[:400]}"
        )

    try:
        return resp.json()
    except ValueError as exc:
        raise SatelliteAPIError(
            f"Non-JSON response from {url}: {resp.text[:200]}"
        ) from exc


def fetch_all_hosts(
    sat_url: str,
    sat_auth: tuple[str, str],
    verify: bool,
) -> list[dict]:
    """Paginate through /api/hosts and return all hosts as {id, name} dicts."""
    print(f"Collecting content hosts from Satellite: {sat_url}", file=sys.stderr)

    hosts: list[dict] = []
    page = 1
    per_page = 250

    while True:
        print(f"  Fetching hosts page {page}...", file=sys.stderr)
        try:
            data = sat_get(
                sat_url,
                "/api/hosts",
                sat_auth,
                verify,
                params={"per_page": per_page, "page": page},
            )
        except SatelliteAPIError as exc:
            print(f"ERROR: Failed to list hosts: {exc}", file=sys.stderr)
            sys.exit(1)

        results = data.get("results", [])
        total = data.get("total", 0)

        for h in results:
            hosts.append({"id": h["id"], "name": h.get("name", str(h["id"]))})

        print(
            f"  Collected {len(hosts)} / {total} hosts so far.",
            file=sys.stderr,
        )
        if len(hosts) >= total:
            break
        page += 1

    print(f"Total hosts to check: {len(hosts)}", file=sys.stderr)
    return hosts


def fetch_host_module_streams(
    sat_url: str,
    host: dict,
    sat_auth: tuple[str, str],
    verify: bool,
) -> list[dict]:
    """Return all module streams reported by Satellite for a single host."""
    data = sat_get(
        sat_url,
        f"/api/hosts/{host['id']}/module_streams",
        sat_auth,
        verify,
        params={"full_result": "true"},
    )
    return data.get("results", [])


# ── Cross-reference ──────────────────────────────────────────────────────────
def build_retired_lookup(retired: list[dict]) -> dict[tuple[str, str], dict]:
    """
    Build a dict keyed by (module_name, stream_version) → retired entry.
    An entry with stream_version="" acts as a name-only wildcard fallback.
    """
    lookup: dict[tuple[str, str], dict] = {}
    for entry in retired:
        key = (entry["module_name"], entry["stream_version"])
        lookup[key] = entry
    return lookup


def find_matches(
    module_streams: list[dict],
    retired_lookup: dict[tuple[str, str], dict],
) -> list[dict]:
    """
    Return the subset of module_streams that are:
      1. Enabled or installed on the host (not merely listed as "Not installed"
         with status "disabled" or "unknown").
      2. Present in the retired_lookup by (name, stream) – with fallback to
         (name, "") for catalog entries where stream extraction failed.
    """
    matches: list[dict] = []
    seen: set[str] = set()

    for ms in module_streams:
        # Only flag streams the host is actually using
        installed = ms.get("install_status", "") != "Not installed"
        enabled   = ms.get("status", "") == "enabled"
        if not (installed or enabled):
            continue

        name   = ms.get("name", "")
        stream = ms.get("stream", "")

        retired_entry = retired_lookup.get((name, stream)) or retired_lookup.get(
            (name, "")
        )
        if retired_entry is None:
            continue

        module_spec = ms.get("module_spec", f"{name}:{stream}")
        if module_spec in seen:
            continue
        seen.add(module_spec)

        matches.append(
            {
                "module_spec":    module_spec,
                "status":         ms.get("status", ""),
                "install_status": ms.get("install_status", ""),
                "display_name":   retired_entry["display_name"],
                "end_date":       retired_entry["end_date"],
                "support_status": retired_entry["support_status"],
            }
        )

    return matches


# ── Output ───────────────────────────────────────────────────────────────────
def print_table(results: list[dict], total_hosts: int) -> None:
    sep = "─" * 80
    print()
    print("╔══════════════════════════════════════════════════════════════════════════════╗")
    print("║      Satellite Content Hosts with Retired AppStreams Installed               ║")
    print("╚══════════════════════════════════════════════════════════════════════════════╝")
    print()

    if not results:
        print("  No hosts with retired AppStreams found.")
    else:
        for host_result in results:
            print(f"Host: {host_result['host_name']}  (id={host_result['host_id']})")
            for ms in host_result["retired_streams"]:
                print(
                    f"  [RETIRED] {ms['module_spec']}"
                    f"  ({ms['display_name']}, "
                    f"status: {ms['status']}, "
                    f"install_status: {ms['install_status']}, "
                    f"until: {ms['end_date']})"
                )
            print()

    print(sep)
    print(
        f"Summary: {len(results)} host(s) with retired AppStreams found "
        f"out of {total_hosts} checked."
    )
    print()


def print_json(results: list[dict]) -> None:
    print(json.dumps(results, indent=2))


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    args = parse_args()

    ssl_verify = args.verify
    if not ssl_verify:
        urllib3.disable_warnings(InsecureRequestWarning)

    # Collect credentials from environment
    client_id     = os.environ.get("RH_CLIENT_ID", "")
    client_secret = os.environ.get("RH_CLIENT_SECRET", "")
    sat_user      = os.environ.get("SAT_USER", "")
    sat_password  = os.environ.get("SAT_PASSWORD", "")

    missing = [
        name
        for name, val in (
            ("RH_CLIENT_ID",     client_id),
            ("RH_CLIENT_SECRET", client_secret),
            ("SAT_USER",         sat_user),
            ("SAT_PASSWORD",     sat_password),
        )
        if not val
    ]
    if missing:
        print(
            "ERROR: The following environment variables are not set: "
            + ", ".join(missing),
            file=sys.stderr,
        )
        sys.exit(1)

    sat_auth: tuple[str, str] = (sat_user, sat_password)

    # ── 1. Lightspeed: get retired AppStreams ────────────────────────────────
    token = get_access_token(client_id, client_secret)
    retired = fetch_retired_appstreams(token, args.major, args.include_near_retirement)

    if not retired:
        print("No retired AppStreams found in Lightspeed catalog – nothing to check.")
        sys.exit(0)

    print("\nRetired AppStreams to check for:", file=sys.stderr)
    for entry in retired:
        print(
            f"  {entry['module_name']}:{entry['stream_version']}"
            f"  ({entry['display_name']}, "
            f"RHEL {entry['os_major'] or 'any'}, "
            f"until {entry['end_date']})",
            file=sys.stderr,
        )
    print("", file=sys.stderr)

    retired_lookup = build_retired_lookup(retired)

    # ── 2. Satellite: collect all content hosts ──────────────────────────────
    all_hosts = fetch_all_hosts(args.sat_url, sat_auth, ssl_verify)
    total_hosts = len(all_hosts)

    # ── 3. Check module streams per host (parallel) ──────────────────────────
    print(
        f"\nChecking module streams on {total_hosts} host(s) "
        f"with {args.workers} parallel worker(s)...",
        file=sys.stderr,
    )

    all_results: list[dict] = []

    def check_host(host: dict) -> Optional[dict]:
        """Worker function: returns a result dict or None if no matches."""
        try:
            streams = fetch_host_module_streams(
                args.sat_url, host, sat_auth, ssl_verify
            )
        except SatelliteAPIError as exc:
            print(
                f"  [SKIP] Could not retrieve module streams for host "
                f"{host['name']} (id={host['id']}): {exc}",
                file=sys.stderr,
            )
            return None

        matches = find_matches(streams, retired_lookup)
        if matches:
            return {
                "host_id":        str(host["id"]),
                "host_name":      host["name"],
                "retired_streams": matches,
            }
        return None

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(check_host, host): host for host in all_hosts}
        done = 0
        for future in as_completed(futures):
            host = futures[future]
            done += 1
            try:
                result = future.result()
            except Exception as exc:
                print(
                    f"  [ERROR] Host {host['name']}: {exc}",
                    file=sys.stderr,
                )
                result = None

            status = "MATCH" if result else "clean"
            print(
                f"  [{done}/{total_hosts}] {host['name']} → {status}",
                file=sys.stderr,
            )
            if result:
                all_results.append(result)

    # Sort results alphabetically by host name for consistent output
    all_results.sort(key=lambda r: r["host_name"])

    # ── 4. Output ────────────────────────────────────────────────────────────
    if args.output_format == "json":
        print_json(all_results)
    else:
        print_table(all_results, total_hosts)


if __name__ == "__main__":
    main()
