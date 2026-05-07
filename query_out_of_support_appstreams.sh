#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# query_out_of_support_appstreams.sh
#
# Queries the Red Hat Lightspeed (Insights) Planning API to find all
# Application Streams installed on registered RHEL systems in your
# Hybrid Cloud Console inventory whose support status is "Retired"
# (i.e., out of support) or optionally also "Near retirement".
#
# API used:
#   GET https://console.redhat.com/api/roadmap/v1/relevant/lifecycle/app-streams
#
# Authentication: Red Hat service account (client credentials flow).
# The service account must belong to a group that has the "RHEL viewer" role.
#
# Prerequisites:
#   - curl
#   - jq  (https://jqlang.github.io/jq/)
#
# Usage:
#   export RH_CLIENT_ID="<your-service-account-client-id>"
#   export RH_CLIENT_SECRET="<your-service-account-client-secret>"
#   ./query_out_of_support_appstreams.sh [--include-near-retirement] [--major 8|9|10]
#
# Options:
#   --include-near-retirement   Also include streams with status "Near retirement"
#   --major <version>           Filter by RHEL major version (8, 9, or 10)
#   --output-format <fmt>       Output format: table (default) or json
# ---------------------------------------------------------------------------

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
SSO_URL="https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
API_BASE="https://console.redhat.com/api/roadmap/v1"
INCLUDE_NEAR_RETIREMENT=false
RHEL_MAJOR=""
OUTPUT_FORMAT="table"

# ── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --include-near-retirement)
            INCLUDE_NEAR_RETIREMENT=true
            shift
            ;;
        --major)
            RHEL_MAJOR="$2"
            shift 2
            ;;
        --output-format)
            OUTPUT_FORMAT="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '/^# Usage:/,/^# -----------/p' "$0" | grep '^#' | sed 's/^# *//'
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

# ── Credential check ─────────────────────────────────────────────────────────
if [[ -z "${RH_CLIENT_ID:-}" || -z "${RH_CLIENT_SECRET:-}" ]]; then
    echo "ERROR: RH_CLIENT_ID and RH_CLIENT_SECRET must be set as environment variables." >&2
    echo "       Export them before running this script:" >&2
    echo "         export RH_CLIENT_ID='<client-id>'" >&2
    echo "         export RH_CLIENT_SECRET='<client-secret>'" >&2
    exit 1
fi

# ── Dependency check ─────────────────────────────────────────────────────────
for cmd in curl jq; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: '$cmd' is required but not installed." >&2
        exit 1
    fi
done

# ── Step 1 – Obtain access token ─────────────────────────────────────────────
echo "Authenticating with Red Hat SSO..." >&2

TOKEN_RESPONSE=$(curl --silent \
    "$SSO_URL" \
    -d "grant_type=client_credentials" \
    -d "scope=api.console" \
    -d "client_id=${RH_CLIENT_ID}" \
    -d "client_secret=${RH_CLIENT_SECRET}")

ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token // empty')

if [[ -z "$ACCESS_TOKEN" ]]; then
    echo "ERROR: Failed to obtain access token. Check your credentials." >&2
    echo "SSO response: $TOKEN_RESPONSE" >&2
    exit 1
fi

echo "Authentication successful (token valid for $(echo "$TOKEN_RESPONSE" | jq -r '.expires_in') seconds)." >&2

# ── Step 2 – Build the API request URL ───────────────────────────────────────
API_URL="${API_BASE}/relevant/lifecycle/app-streams"
QUERY_PARAMS=()

if [[ -n "$RHEL_MAJOR" ]]; then
    QUERY_PARAMS+=("major=${RHEL_MAJOR}")
fi

if [[ ${#QUERY_PARAMS[@]} -gt 0 ]]; then
    API_URL="${API_URL}?$(IFS='&'; echo "${QUERY_PARAMS[*]}")"
fi

# ── Step 3 – Query the Planning API ──────────────────────────────────────────
echo "Querying: GET ${API_URL}" >&2

# --write-out appends the HTTP status code after the body so we can check it
# without relying on --fail (which would silently kill the script via set -e).
RAW_RESPONSE=$(curl --silent \
    --write-out "\nHTTPSTATUS:%{http_code}" \
    --request GET \
    --url "$API_URL" \
    --header "Authorization: Bearer ${ACCESS_TOKEN}" \
    --header "Content-Type: application/json")

HTTP_STATUS=$(echo "$RAW_RESPONSE" | grep -o 'HTTPSTATUS:[0-9]*' | cut -d: -f2)
RESPONSE=$(echo "$RAW_RESPONSE" | sed '/^HTTPSTATUS:/d')

if [[ "$HTTP_STATUS" == "401" ]]; then
    echo "ERROR: Unauthorized (HTTP 401). Your access token may be invalid or expired." >&2
    exit 1
elif [[ "$HTTP_STATUS" == "403" ]]; then
    echo "ERROR: Forbidden (HTTP 403). Ensure your service account is in a group" >&2
    echo "       that has the 'RHEL viewer' role assigned." >&2
    exit 1
elif [[ "$HTTP_STATUS" != "200" ]]; then
    echo "ERROR: API request failed with HTTP ${HTTP_STATUS}." >&2
    echo "Response body: ${RESPONSE}" >&2
    exit 1
fi

TOTAL=$(echo "$RESPONSE" | jq -r '.meta.count // 0')
echo "Total app streams returned for your inventory: ${TOTAL}" >&2

# ── Step 4 – Filter for out-of-support (and optionally near-retirement) streams
if [[ "$INCLUDE_NEAR_RETIREMENT" == "true" ]]; then
    STATUS_FILTER='"Retired", "Near retirement"'
    echo "Filtering for status: Retired, Near retirement" >&2
else
    STATUS_FILTER='"Retired"'
    echo "Filtering for status: Retired (out of support)" >&2
fi

FILTERED=$(echo "$RESPONSE" | jq --argjson statuses "[${STATUS_FILTER}]" '
    .data | map(select(.support_status as $s | $statuses | index($s) != null))
')

MATCH_COUNT=$(echo "$FILTERED" | jq 'length')
echo "Matching app streams found: ${MATCH_COUNT}" >&2
echo "" >&2

# ── Step 5 – Output results ───────────────────────────────────────────────────
if [[ "$OUTPUT_FORMAT" == "json" ]]; then
    echo "$FILTERED" | jq .
    exit 0
fi

# Table output
if [[ "$MATCH_COUNT" -eq 0 ]]; then
    echo "No out-of-support application streams found in your inventory."
    exit 0
fi

echo "╔══════════════════════════════════════════════════════════════════════════════╗"
echo "║       Out-of-Support Application Streams in Your RHEL Inventory             ║"
echo "╚══════════════════════════════════════════════════════════════════════════════╝"
echo ""

echo "$FILTERED" | jq -r '.[] | 
    "AppStream  : \(.display_name)",
    "Module name: \(.name)",
    "RHEL major : \(.os_major // "N/A")",
    "Status     : \(.support_status)",
    "End date   : \(.end_date // "N/A")",
    "# Systems  : \(.count)",
    "Systems    :",
    (.systems_detail[] | "  - \(.display_name)  [RHEL \(.os_major).\(.os_minor // "x")]  id=\(.id)"),
    "────────────────────────────────────────────────────────────────────────────────"
'
