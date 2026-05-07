# Lightspeed API — Out-of-Support AppStream Queries

Sample scripts that use the **Red Hat Lightspeed (Insights) Planning API** to identify
RHEL systems in your Hybrid Cloud Console inventory that have Application Streams installed
with a support status of **Retired** (out of support).

This is a proof of concept. Continue at your own risk. Feedback and comments are welcome, please use the issue tracker for it.

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
| `query_out_of_support_appstreams.sh` | Bash script (requires `curl` and `jq`) |
| `query_out_of_support_appstreams.py` | Python 3 script (requires `requests`) |

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

## References

- [Using APIs to configure Red Hat Lightspeed services](https://docs.redhat.com/en/documentation/red_hat_lightspeed/1-latest/html-single/using_apis_to_configure_red_hat_lightspeed_services/index)
- [How to query the Red Hat Insights API for RHEL Application Streams lifecycle information](https://access.redhat.com/articles/7129267)
- [RHEL Application Streams Life Cycle policy](https://access.redhat.com/support/policy/updates/rhel-app-streams-life-cycle)
- [Planning API OpenAPI specification](https://console.redhat.com/api/roadmap/v1/openapi.json)
