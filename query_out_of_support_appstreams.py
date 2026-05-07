#!/usr/bin/env python3
"""
query_out_of_support_appstreams.py

Queries the Red Hat Lightspeed (Insights) Planning API to find all
Application Streams installed on registered RHEL systems in your
Hybrid Cloud Console inventory whose support status is "Retired"
(i.e., out of support) or optionally also "Near retirement".

API used:
    GET https://console.redhat.com/api/roadmap/v1/relevant/lifecycle/app-streams

Authentication: Red Hat service account (client credentials flow).
The service account must belong to a group that has the "RHEL viewer" role.

Prerequisites:
    pip install requests

Usage:
    export RH_CLIENT_ID="<your-service-account-client-id>"
    export RH_CLIENT_SECRET="<your-service-account-client-secret>"
    python3 query_out_of_support_appstreams.py [options]

Options:
    --include-near-retirement   Also include streams with status "Near retirement"
    --major <version>           Filter by RHEL major version (8, 9, or 10)
    --output-format <fmt>       table (default), json, or csv
    --help                      Show this help message and exit
"""

import argparse
import csv
import json
import os
import sys
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library is required. Install it with: pip install requests", file=sys.stderr)
    sys.exit(1)

SSO_URL = "https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
API_BASE = "https://console.redhat.com/api/roadmap/v1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find RHEL systems with out-of-support Application Streams via the Lightspeed Planning API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--include-near-retirement",
        action="store_true",
        default=False,
        help="Also include streams with status 'Near retirement'",
    )
    parser.add_argument(
        "--major",
        type=int,
        choices=[8, 9, 10],
        help="Filter by RHEL major version (8, 9, or 10)",
    )
    parser.add_argument(
        "--output-format",
        choices=["table", "json", "csv"],
        default="table",
        help="Output format: table (default), json, or csv",
    )
    return parser.parse_args()


def get_access_token(client_id: str, client_secret: str) -> str:
    """Obtain a short-lived Bearer token from Red Hat SSO using client credentials."""
    print("Authenticating with Red Hat SSO...", file=sys.stderr)
    response = requests.post(
        SSO_URL,
        data={
            "grant_type": "client_credentials",
            "scope": "api.console",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=30,
    )
    if response.status_code != 200:
        print(
            f"ERROR: Authentication failed (HTTP {response.status_code}).\n"
            f"Response: {response.text}",
            file=sys.stderr,
        )
        sys.exit(1)

    payload = response.json()
    token = payload.get("access_token")
    if not token:
        print(f"ERROR: No access_token in SSO response: {payload}", file=sys.stderr)
        sys.exit(1)

    expires_in = payload.get("expires_in", "?")
    print(f"Authentication successful (token valid for {expires_in} seconds).", file=sys.stderr)
    return token


def fetch_relevant_appstreams(
    token: str,
    major: Optional[int] = None,
) -> list[dict]:
    """Call the Planning API for app streams based on hosts in inventory."""
    url = f"{API_BASE}/relevant/lifecycle/app-streams"
    params: dict = {}
    if major is not None:
        params["major"] = major

    print(f"Querying: GET {url}  params={params}", file=sys.stderr)

    response = requests.get(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        params=params,
        timeout=60,
    )

    if response.status_code == 403:
        print(
            "ERROR: Access denied (HTTP 403). "
            "Ensure your service account is in a group with the 'RHEL viewer' role.",
            file=sys.stderr,
        )
        sys.exit(1)
    if response.status_code != 200:
        print(
            f"ERROR: API request failed (HTTP {response.status_code}).\nResponse: {response.text}",
            file=sys.stderr,
        )
        sys.exit(1)

    data = response.json()
    total = data.get("meta", {}).get("count", 0)
    print(f"Total app streams returned for your inventory: {total}", file=sys.stderr)
    return data.get("data", [])


def filter_out_of_support(
    appstreams: list[dict],
    include_near_retirement: bool,
) -> list[dict]:
    """Keep only streams that are retired (and optionally near-retirement)."""
    target_statuses = {"Retired"}
    if include_near_retirement:
        target_statuses.add("Near retirement")

    label = ", ".join(sorted(target_statuses))
    print(f"Filtering for status: {label}", file=sys.stderr)

    return [s for s in appstreams if s.get("support_status") in target_statuses]


def print_table(streams: list[dict]) -> None:
    if not streams:
        print("No out-of-support application streams found in your inventory.")
        return

    separator = "─" * 80
    header = "Out-of-Support Application Streams in Your RHEL Inventory"
    print(f"\n{'═' * 80}")
    print(f"  {header}")
    print(f"{'═' * 80}\n")

    for stream in streams:
        print(f"  AppStream  : {stream.get('display_name', 'N/A')}")
        print(f"  Module name: {stream.get('name', 'N/A')}")
        print(f"  RHEL major : {stream.get('os_major', 'N/A')}")
        print(f"  Status     : {stream.get('support_status', 'N/A')}")
        print(f"  End date   : {stream.get('end_date', 'N/A')}")
        print(f"  # Systems  : {stream.get('count', 0)}")
        systems = stream.get("systems_detail", [])
        if systems:
            print("  Systems    :")
            for sys_info in systems:
                os_minor = sys_info.get("os_minor")
                minor_str = str(os_minor) if os_minor is not None else "x"
                print(
                    f"    - {sys_info.get('display_name', 'N/A')}"
                    f"  [RHEL {sys_info.get('os_major', '?')}.{minor_str}]"
                    f"  id={sys_info.get('id', 'N/A')}"
                )
        print(f"  {separator}")


def print_json(streams: list[dict]) -> None:
    print(json.dumps(streams, indent=2, default=str))


def print_csv(streams: list[dict]) -> None:
    """Flatten systems_detail into one row per system × appstream pair."""
    writer = csv.writer(sys.stdout)
    writer.writerow([
        "appstream_display_name",
        "module_name",
        "os_major",
        "support_status",
        "end_date",
        "system_id",
        "system_display_name",
        "system_os_major",
        "system_os_minor",
    ])
    for stream in streams:
        for sys_info in stream.get("systems_detail", []):
            writer.writerow([
                stream.get("display_name", ""),
                stream.get("name", ""),
                stream.get("os_major", ""),
                stream.get("support_status", ""),
                stream.get("end_date", ""),
                sys_info.get("id", ""),
                sys_info.get("display_name", ""),
                sys_info.get("os_major", ""),
                sys_info.get("os_minor", ""),
            ])


def main() -> None:
    args = parse_args()

    client_id = os.environ.get("RH_CLIENT_ID", "")
    client_secret = os.environ.get("RH_CLIENT_SECRET", "")

    if not client_id or not client_secret:
        print(
            "ERROR: RH_CLIENT_ID and RH_CLIENT_SECRET must be set as environment variables.\n"
            "       export RH_CLIENT_ID='<client-id>'\n"
            "       export RH_CLIENT_SECRET='<client-secret>'",
            file=sys.stderr,
        )
        sys.exit(1)

    token = get_access_token(client_id, client_secret)

    all_streams = fetch_relevant_appstreams(token, major=args.major)

    out_of_support = filter_out_of_support(all_streams, args.include_near_retirement)

    print(f"Matching app streams found: {len(out_of_support)}", file=sys.stderr)
    print("", file=sys.stderr)

    if args.output_format == "json":
        print_json(out_of_support)
    elif args.output_format == "csv":
        print_csv(out_of_support)
    else:
        print_table(out_of_support)


if __name__ == "__main__":
    main()
