# Lightspeed API — Out-of-Support AppStream Queries

Sample scripts that use the **Red Hat Lightspeed (Insights) Planning API** to identify
RHEL systems in your Hybrid Cloud Console inventory that have Application Streams installed
with a support status of **Retired** (out of support).

This is a proof of concept. Continue at your own risk. Feedback and comments are welcome, please use the issue tracker for it.

The `main` branch contains the latest developer version of this PoC. Please check the released version which were tested in my lab environment.

## Background

Red Hat RHEL 8+ ships software via [Application Streams](https://access.redhat.com/support/policy/updates/rhel-app-streams-life-cycle).
Each stream has its own lifecycle and can reach end-of-support independently of the
underlying RHEL release. The Lightspeed Planning API (`/api/roadmap/v1`) exposes this
lifecycle data, including which streams are already **Retired** across your registered fleet.

**API endpoint used:**
```
GET https://console.redhat.com/api/roadmap/v1/relevant/lifecycle/app-streams
```

The `/relevant/` variant cross-references lifecycle data with the systems actually
registered in your Hybrid Cloud Console inventory, so only streams present on _your_
hosts are returned. Each result includes a `support_status` field and a `systems_detail`
list showing which hosts have that stream installed.

`support_status` values returned by the API:

| Status | Meaning |
|---|---|
| `Supported` | Fully supported |
| `Near retirement` | Approaching end-of-support |
| `Retired` | **Out of support** |
| `Not installed` | Defined but not found in inventory |
| `Upcoming release` | Not yet released |
| `Unknown` | Status could not be determined |

## Authentication

Authentication uses a Red Hat **service account** (client credentials OAuth2 flow).
The service account must be placed in a Hybrid Cloud Console user group that has the
**RHEL viewer** role assigned.

### Setup steps

1. Log in to the [Red Hat Hybrid Cloud Console](https://console.redhat.com/).
2. Go to **Settings → Service Accounts** and create a new service account.
3. Note the generated **Client ID** and **Client secret** (shown only once).
4. Create or choose a user group with the **RHEL viewer** role and add the service account to it.
5. Export the credentials as environment variables before running the scripts:

```bash
export RH_CLIENT_ID="<your-client-id>"
export RH_CLIENT_SECRET="<your-client-secret>"
```

## Files

| File | Description |
|---|---|
| `query_out_of_support_appstreams.sh` | Bash — query Hybrid Cloud Console inventory for retired AppStreams (requires `curl` and `jq`) |
| `query_out_of_support_appstreams.py` | Python 3 — same as above (requires `requests`) |
| `check_satellite_retired_appstreams.sh` | Bash — cross-reference Satellite hosts against the retired catalog (requires `curl` and `jq`) |
| `check_satellite_retired_appstreams.py` | Python 3 — same as above (requires `requests`) |

## Bash script

### Prerequisites

- `curl`
- `jq` — [https://jqlang.github.io/jq/](https://jqlang.github.io/jq/)

### Usage

```bash
chmod +x query_out_of_support_appstreams.sh

# Basic: list all Retired appstreams across all RHEL versions
./query_out_of_support_appstreams.sh

# Include streams that are nearing end-of-support as well
./query_out_of_support_appstreams.sh --include-near-retirement

# Filter to RHEL 8 systems only
./query_out_of_support_appstreams.sh --major 8

# Output raw JSON instead of the human-readable table
./query_out_of_support_appstreams.sh --output-format json

# Show only AppStream and module names, without host details
./query_out_of_support_appstreams.sh --appstreams-only

# Combine flags: near-retirement streams on RHEL 8, names only, as JSON
./query_out_of_support_appstreams.sh --include-near-retirement --major 8 --appstreams-only --output-format json
```

## Python script

### Prerequisites

```bash
pip install requests
```

### Usage

```bash
# Basic: list all Retired appstreams across all RHEL versions
python3 query_out_of_support_appstreams.py

# Include streams that are nearing end-of-support as well
python3 query_out_of_support_appstreams.py --include-near-retirement

# Filter to RHEL 9 systems only
python3 query_out_of_support_appstreams.py --major 9

# Output as JSON
python3 query_out_of_support_appstreams.py --output-format json

# Output as CSV (one row per system × appstream pair)
python3 query_out_of_support_appstreams.py --output-format csv > out_of_support.csv

# Show only AppStream and module names, without host details
python3 query_out_of_support_appstreams.py --appstreams-only

# Combine flags: near-retirement streams on RHEL 8, names only, as CSV
python3 query_out_of_support_appstreams.py --include-near-retirement --major 8 --appstreams-only --output-format csv > near_retirement.csv
```

## Example output (table format)

```
════════════════════════════════════════════════════════════════════════════════
  Out-of-Support Application Streams in Your RHEL Inventory
════════════════════════════════════════════════════════════════════════════════

  AppStream  : Node.js 16
  Module name: nodejs
  RHEL major : 8
  Status     : Retired
  End date   : 2024-04-30
  # Systems  : 2
  Systems    :
    - server01.example.com  [RHEL 8.9]  id=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    - server02.example.com  [RHEL 8.10] id=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
  ────────────────────────────────────────────────────────────────────────────────
```

## Example output (`--appstreams-only`)

```
════════════════════════════════════════════════════════════════════════════════
  Out-of-Support Application Streams in Your RHEL Inventory
════════════════════════════════════════════════════════════════════════════════

  AppStream  : Node.js 16
  Module name: nodejs
  ────────────────────────────────────────────────────────────────────────────────
  AppStream  : Python 3.6
  Module name: python36
  ────────────────────────────────────────────────────────────────────────────────
```

---

## Satellite — Check Content Hosts for Retired AppStreams

The `check_satellite_retired_appstreams.*` scripts extend the approach above by
cross-referencing the Lightspeed retired-AppStream catalog against actual module
streams reported by a **Red Hat Satellite** server.

### How it works

1. Authenticate to Red Hat SSO and obtain a Bearer token (same as above).
2. Query the Lightspeed lifecycle catalog for all retired (and optionally
   near-retirement) AppStream module streams.
3. Paginate through all content hosts registered in Satellite
   (`GET <SAT_URL>/api/hosts`).
4. For each host, retrieve its module streams
   (`GET <SAT_URL>/api/hosts/:id/module_streams`) and cross-reference against
   the retired set.
5. Print a report of every host that has a retired AppStream **enabled or
   installed**.

A host is only flagged if the matching module stream has
`install_status != "Not installed"` or `status == "enabled"`.

Matching is done on both module **name** (e.g. `nodejs`) and **stream version**
(e.g. `16`, extracted from the Lightspeed `display_name` field) to avoid false
positives such as flagging `nodejs:18` when only `nodejs:16` is retired.

### Additional prerequisites

- A **Satellite user account** with at least **View hosts** permission.
- Export credentials before running:

```bash
export SAT_USER="<satellite-username>"
export SAT_PASSWORD="<satellite-password>"
```

### Bash script

```bash
chmod +x check_satellite_retired_appstreams.sh

# Basic usage
./check_satellite_retired_appstreams.sh

# Use a different Satellite server and restrict to RHEL 8
./check_satellite_retired_appstreams.sh --sat-url https://satellite.example.com --major 8

# Also flag "Near retirement" streams, output as JSON
./check_satellite_retired_appstreams.sh --include-near-retirement --output-format json

# Increase parallel host queries
./check_satellite_retired_appstreams.sh --workers 10

# Enable SSL certificate verification (disabled by default for lab environments)
./check_satellite_retired_appstreams.sh --verify
```

### Python script

```bash
# Basic usage
python3 check_satellite_retired_appstreams.py

# Use a different Satellite server and restrict to RHEL 9
python3 check_satellite_retired_appstreams.py --sat-url https://satellite.example.com --major 9

# Also include "Near retirement" streams, output as JSON
python3 check_satellite_retired_appstreams.py --include-near-retirement --output-format json

# Save JSON output to a file
python3 check_satellite_retired_appstreams.py --output-format json > retired_report.json

# Enable SSL certificate verification
python3 check_satellite_retired_appstreams.py --verify
```

### Example output (Satellite)

```
Authenticating with Red Hat SSO...
Authentication successful (token valid for 300 seconds).
Querying Lightspeed catalog: GET https://console.redhat.com/api/roadmap/v1/lifecycle/app-streams
Filtering for status: Retired
Retired AppStreams found in Lightspeed catalog: 12
Collecting content hosts from Satellite: https://sat-1.example.com
Total hosts to check: 5
Checking module streams on each host (parallel jobs: 5)...
  [1/5] rhel8-client-01.example.com → MATCH
  [2/5] rhel9-server-01.example.com → clean
  ...

╔══════════════════════════════════════════════════════════════════════════════╗
║       Satellite Content Hosts with Retired AppStreams Installed              ║
╚══════════════════════════════════════════════════════════════════════════════╝

Host: rhel8-client-01.example.com  (id=3)
  [RETIRED] nodejs:16  (Node.js 16, status: enabled, install_status: Up-to-date, until: 2024-04-30)

Host: rhel8-client-02.example.com  (id=4)
  [RETIRED] python36:3.6  (Python 3.6, status: enabled, install_status: Installed, until: 2024-10-31)

────────────────────────────────────────────────────────────────────────────────
Summary: 2 host(s) with retired AppStreams found out of 5 checked.
```

## References

- [Using APIs to configure Red Hat Lightspeed services](https://docs.redhat.com/en/documentation/red_hat_lightspeed/1-latest/html-single/using_apis_to_configure_red_hat_lightspeed_services/index)
- [How to query the Red Hat Insights API for RHEL Application Streams lifecycle information](https://access.redhat.com/articles/7129267)
- [RHEL Application Streams Life Cycle policy](https://access.redhat.com/support/policy/updates/rhel-app-streams-life-cycle)
- [Planning API OpenAPI specification](https://console.redhat.com/api/roadmap/v1/openapi.json)
- [Red Hat Satellite REST API — Using the API](https://docs.redhat.com/en/documentation/red_hat_satellite/6.19/html-single/using_the_satellite_rest_api/index)
