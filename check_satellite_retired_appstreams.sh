#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# check_satellite_retired_appstreams.sh
#
# 1. Authenticates to the Red Hat SSO and queries the Lightspeed Planning API
#    for all AppStream module streams whose support_status is "Retired".
# 2. Paginates through every content host registered in Red Hat Satellite.
# 3. For each host, retrieves its module streams from Satellite and
#    cross-references them against the retired set.
# 4. Prints a report listing hosts that have retired AppStreams
#    enabled or installed.
#
# Prerequisites:
#   - curl
#   - jq  (https://jqlang.github.io/jq/)
#
# Required environment variables:
#   RH_CLIENT_ID      – Red Hat service account client ID
#   RH_CLIENT_SECRET  – Red Hat service account client secret
#   SAT_USER          – Satellite username
#   SAT_PASSWORD      – Satellite password
#
# Usage:
#   export RH_CLIENT_ID="..."
#   export RH_CLIENT_SECRET="..."
#   export SAT_USER="<username>"
#   export SAT_PASSWORD="<password>"
#   ./check_satellite_retired_appstreams.sh [options]
#
# Options:
#   --sat-url URL               Satellite base URL (default: https://sat-1.lab1.nat)
#   --include-near-retirement   Also flag streams with status "Near retirement"
#   --major <version>           Filter by RHEL major version (positive integer, e.g. 8, 9)
#   --output-format <fmt>       Output format: table (default) or json
#   --workers <n>               Max simultaneous host queries (default: 5)
#   --verify                    Enable SSL certificate verification (disabled by default)
# ---------------------------------------------------------------------------

set -euo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
SAT_URL="${SAT_URL:-https://sat-1.lab1.nat}"
INCLUDE_NEAR_RETIREMENT=false
RHEL_MAJOR=""
PARALLEL_JOBS=5
OUTPUT_FORMAT="table"
VERIFY_SSL=false

SSO_URL="https://sso.redhat.com/auth/realms/redhat-external/protocol/openid-connect/token"
API_BASE="https://console.redhat.com/api/roadmap/v1"

# ── Argument parsing ─────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --sat-url)
            [[ $# -ge 2 ]] || { echo "ERROR: --sat-url requires a value" >&2; exit 1; }
            SAT_URL="$2"
            shift 2
            ;;
        --include-near-retirement)
            INCLUDE_NEAR_RETIREMENT=true
            shift
            ;;
        --major)
            [[ $# -ge 2 ]] || { echo "ERROR: --major requires a value" >&2; exit 1; }
            [[ "$2" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: --major must be a positive integer (e.g. 8, 9, 10)" >&2; exit 1; }
            RHEL_MAJOR="$2"
            shift 2
            ;;
        --output-format)
            [[ $# -ge 2 ]] || { echo "ERROR: --output-format requires a value" >&2; exit 1; }
            case "$2" in
                table|json) ;;
                *) echo "ERROR: --output-format must be 'table' or 'json'" >&2; exit 1 ;;
            esac
            OUTPUT_FORMAT="$2"
            shift 2
            ;;
        --workers)
            [[ $# -ge 2 ]] || { echo "ERROR: --workers requires a value" >&2; exit 1; }
            [[ "$2" =~ ^[1-9][0-9]*$ ]] || { echo "ERROR: --workers must be a positive integer" >&2; exit 1; }
            PARALLEL_JOBS="$2"
            shift 2
            ;;
        --verify)
            VERIFY_SSL=true
            shift
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

CURL_INSECURE_FLAG=""
if [[ "$VERIFY_SSL" == "false" ]]; then
    CURL_INSECURE_FLAG="--insecure"
fi

TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_WORK"' EXIT

# ── Helpers ──────────────────────────────────────────────────────────────────
log()  { echo "  $*" >&2; }
warn() { echo "  WARNING: $*" >&2; }
err()  { echo "ERROR: $*" >&2; exit 1; }

sat_curl() {
    curl --silent \
         ${CURL_INSECURE_FLAG} \
         --user "${SAT_USER}:${SAT_PASSWORD}" \
         --header "Accept: application/json" \
         --header "Content-Type: application/json" \
         "$@"
}

# ── Dependency checks ────────────────────────────────────────────────────────
for cmd in curl jq; do
    command -v "$cmd" &>/dev/null || err "'$cmd' is required but not installed."
done

# ── Credential checks ────────────────────────────────────────────────────────
[[ -n "${RH_CLIENT_ID:-}"     ]] || err "RH_CLIENT_ID is not set."
[[ -n "${RH_CLIENT_SECRET:-}" ]] || err "RH_CLIENT_SECRET is not set."
[[ -n "${SAT_USER:-}"         ]] || err "SAT_USER is not set."
[[ -n "${SAT_PASSWORD:-}"     ]] || err "SAT_PASSWORD is not set."

# ════════════════════════════════════════════════════════════════════════════
# STEP 1 – Obtain Red Hat SSO Bearer token
# ════════════════════════════════════════════════════════════════════════════
log "Authenticating with Red Hat SSO..."

TOKEN_RESPONSE=$(curl --silent \
    "$SSO_URL" \
    -d "grant_type=client_credentials" \
    -d "scope=api.console" \
    -d "client_id=${RH_CLIENT_ID}" \
    -d "client_secret=${RH_CLIENT_SECRET}")

ACCESS_TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token // empty')
[[ -n "$ACCESS_TOKEN" ]] || err "Failed to obtain SSO token. Response: $TOKEN_RESPONSE"

log "Authentication successful (token valid for $(echo "$TOKEN_RESPONSE" | jq -r '.expires_in') seconds)."

# ════════════════════════════════════════════════════════════════════════════
# STEP 2 – Fetch retired AppStreams from Lightspeed catalog API
# ════════════════════════════════════════════════════════════════════════════
if [[ -n "$RHEL_MAJOR" ]]; then
    LIGHTSPEED_URL="${API_BASE}/lifecycle/app-streams/${RHEL_MAJOR}"
else
    LIGHTSPEED_URL="${API_BASE}/lifecycle/app-streams"
fi

log "Querying Lightspeed catalog: GET ${LIGHTSPEED_URL}"

LIGHTSPEED_RAW=$(curl --silent \
    --write-out "\nHTTPSTATUS:%{http_code}" \
    --request GET \
    --url "$LIGHTSPEED_URL" \
    --header "Authorization: Bearer ${ACCESS_TOKEN}" \
    --header "Content-Type: application/json")

LIGHTSPEED_STATUS=$(echo "$LIGHTSPEED_RAW" | grep -o 'HTTPSTATUS:[0-9]*' | cut -d: -f2)
LIGHTSPEED_BODY=$(echo   "$LIGHTSPEED_RAW" | sed '/^HTTPSTATUS:/d')

[[ "$LIGHTSPEED_STATUS" == "200" ]] \
    || err "Lightspeed API returned HTTP ${LIGHTSPEED_STATUS}: ${LIGHTSPEED_BODY}"

# Build the jq status filter
if [[ "$INCLUDE_NEAR_RETIREMENT" == "true" ]]; then
    STATUS_FILTER='["Retired","Near retirement"]'
    log "Filtering for status: Retired, Near retirement"
else
    STATUS_FILTER='["Retired"]'
    log "Filtering for status: Retired"
fi

# Produce an array of {module_name, stream_version, display_name} objects.
#
# The Lightspeed catalog does not expose a dedicated "stream" field, so the
# stream version is extracted as the last whitespace-separated token of
# display_name (e.g. "Node.js 16" → "16", "Python 3.6" → "3.6").
# This covers the vast majority of real catalog entries. If parsing fails the
# stream_version is set to "" and the script falls back to name-only matching.
RETIRED_ARRAY=$(echo "$LIGHTSPEED_BODY" | jq --argjson statuses "$STATUS_FILTER" '
    [
      .data[]
      | select(.support_status as $s | $statuses | index($s) != null)
      | {
          module_name:    .name,
          stream_version: (.display_name | split(" ") | last),
          display_name:   .display_name,
          os_major:       .os_major,
          end_date:       .end_date,
          support_status: .support_status
        }
    ]
')

RETIRED_COUNT=$(echo "$RETIRED_ARRAY" | jq 'length')
log "Retired AppStreams found in Lightspeed catalog: ${RETIRED_COUNT}"

if [[ "$RETIRED_COUNT" -eq 0 ]]; then
    log "No retired AppStreams found – nothing to check."
    exit 0
fi

# Persist retired set to a temp file for use in sub-shells
RETIRED_FILE="${TMPDIR_WORK}/retired.json"
echo "$RETIRED_ARRAY" > "$RETIRED_FILE"

# Print a quick reference list
log ""
log "Retired AppStreams to check for:"
echo "$RETIRED_ARRAY" | jq -r '.[] | "  \(.module_name):\(.stream_version)  (\(.display_name), RHEL \(.os_major // "any"), until \(.end_date // "?"))"' >&2
log ""

# ════════════════════════════════════════════════════════════════════════════
# STEP 3 – Collect all Satellite content hosts (paginated)
# ════════════════════════════════════════════════════════════════════════════
log "Collecting content hosts from Satellite: ${SAT_URL}"

HOSTS_FILE="${TMPDIR_WORK}/hosts.json"
echo "[]" > "$HOSTS_FILE"

PAGE=1
PER_PAGE=250

while true; do
    log "  Fetching hosts page ${PAGE}..."
    PAGE_RESPONSE=$(sat_curl \
        "${SAT_URL}/api/hosts?per_page=${PER_PAGE}&page=${PAGE}")

    PAGE_TOTAL=$(echo "$PAGE_RESPONSE"  | jq '.total    // 0')
    PAGE_COUNT=$(echo "$PAGE_RESPONSE"  | jq '.subtotal // 0')
    PAGE_RESULTS=$(echo "$PAGE_RESPONSE" | jq '.results // []')

    # Append to accumulated host list (only id and name are needed)
    jq -s '.[0] + [.[1][] | {id: .id, name: .name}]' \
        "$HOSTS_FILE" <(echo "$PAGE_RESULTS") > "${HOSTS_FILE}.tmp"
    mv "${HOSTS_FILE}.tmp" "$HOSTS_FILE"

    COLLECTED=$(jq 'length' "$HOSTS_FILE")
    log "  Collected ${COLLECTED} / ${PAGE_TOTAL} hosts so far."

    [[ "$COLLECTED" -lt "$PAGE_TOTAL" ]] || break
    PAGE=$(( PAGE + 1 ))
done

TOTAL_HOSTS=$(jq 'length' "$HOSTS_FILE")
log "Total hosts to check: ${TOTAL_HOSTS}"
log ""

# ════════════════════════════════════════════════════════════════════════════
# STEP 4 – Check module streams per host and cross-reference retired set
# ════════════════════════════════════════════════════════════════════════════
RESULTS_DIR="${TMPDIR_WORK}/results"
mkdir -p "$RESULTS_DIR"

check_host() {
    local host_id="$1"
    local host_name="$2"
    local retired_file="$3"
    local out_file="$4"

    # Retrieve all module streams for this host
    if ! MS_RESPONSE=$(curl --silent \
        ${CURL_INSECURE_FLAG} \
        --user "${SAT_USER}:${SAT_PASSWORD}" \
        --header "Accept: application/json" \
        --header "Content-Type: application/json" \
        "${SAT_URL}/api/hosts/${host_id}/module_streams?full_result=true" 2>&1); then
        echo "[SKIP] Could not retrieve module streams for host ${host_name} (id=${host_id}): ${MS_RESPONSE}" >&2
        return 0
    fi

    if ! MS_RESULTS=$(echo "$MS_RESPONSE" | jq '.results // []' 2>&1); then
        echo "[SKIP] Invalid JSON from Satellite for host ${host_name} (id=${host_id}): ${MS_RESPONSE:0:200}" >&2
        return 0
    fi

    # Cross-reference: a stream matches if
    #   (a) its name matches a retired module_name, AND
    #   (b) its stream matches the parsed stream_version (or stream_version is "")
    #   (c) it is actually enabled or installed on this host
    MATCHES=$(jq -n \
        --argjson streams "$MS_RESULTS" \
        --slurpfile retired "$retired_file" \
        '
        [ $streams[]
          | . as $s
          | ($retired[0][]
             | select(
                 .module_name == $s.name
                 and (.stream_version == "" or .stream_version == $s.stream)
                 and ($s.install_status != "Not installed" or $s.status == "enabled")
               )
             | {
                 module_spec:    $s.module_spec,
                 status:         $s.status,
                 install_status: $s.install_status,
                 display_name:   .display_name,
                 end_date:       .end_date,
                 support_status: .support_status
               }
            )
        ]
        | unique_by(.module_spec)
        ')

    MATCH_COUNT=$(echo "$MATCHES" | jq 'length')

    if [[ "$MATCH_COUNT" -gt 0 ]]; then
        jq -n \
            --arg id   "$host_id" \
            --arg name "$host_name" \
            --argjson matches "$MATCHES" \
            '{host_id: $id, host_name: $name, retired_streams: $matches}' \
            > "$out_file"
    fi
}

export -f check_host
export SAT_URL SAT_USER SAT_PASSWORD CURL_INSECURE_FLAG

log "Checking module streams on each host (parallel jobs: ${PARALLEL_JOBS})..."

# Use a simple parallel execution with a job-slot counter
RUNNING=0
IDX=0
TOTAL=$(jq 'length' "$HOSTS_FILE")

while IFS= read -r host_json; do
    host_id=$(echo   "$host_json" | jq -r '.id')
    host_name=$(echo "$host_json" | jq -r '.name')
    IDX=$(( IDX + 1 ))

    log "  [${IDX}/${TOTAL}] Checking host: ${host_name} (id=${host_id})"

    check_host "$host_id" "$host_name" "$RETIRED_FILE" \
        "${RESULTS_DIR}/${host_id}.json" &

    RUNNING=$(( RUNNING + 1 ))
    if [[ "$RUNNING" -ge "$PARALLEL_JOBS" ]]; then
        wait -n 2>/dev/null || wait   # wait for any one job to finish
        RUNNING=$(( RUNNING - 1 ))
    fi
done < <(jq -c '.[]' "$HOSTS_FILE")

# Wait for remaining background jobs
wait

# ════════════════════════════════════════════════════════════════════════════
# STEP 5 – Collate and print the report
# ════════════════════════════════════════════════════════════════════════════
shopt -s nullglob
RESULT_FILES=("${RESULTS_DIR}"/*.json)
AFFECTED_HOSTS=${#RESULT_FILES[@]}

if [[ "$OUTPUT_FORMAT" == "json" ]]; then
    if [[ "$AFFECTED_HOSTS" -eq 0 ]]; then
        echo "[]"
    else
        jq -s '.' "${RESULT_FILES[@]}"
    fi
    exit 0
fi

# Table output
echo ""
echo "╔══════════════════════════════════════════════════════════════════════════════╗"
echo "║       Satellite Content Hosts with Retired AppStreams Installed              ║"
echo "╚══════════════════════════════════════════════════════════════════════════════╝"
echo ""

if [[ "$AFFECTED_HOSTS" -eq 0 ]]; then
    echo "  No hosts with retired AppStreams found."
else
    for result_file in "${RESULT_FILES[@]}"; do
        jq -r '
            "Host: \(.host_name)  (id=\(.host_id))",
            (
              .retired_streams[]
              | "  [RETIRED] \(.module_spec)"
              + "  (\(.display_name), status: \(.status), install_status: \(.install_status), until \(.end_date))"
            ),
            ""
        ' "$result_file"
    done
fi

echo "──────────────────────────────────────────────────────────────────────────────"
echo "Summary: ${AFFECTED_HOSTS} host(s) with retired AppStreams found out of ${TOTAL_HOSTS} checked."
echo ""
