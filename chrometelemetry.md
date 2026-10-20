# Chrome Telemetry Script — Setup & Overview

---

## Prerequisites

### Google Cloud Console

1. Create or select a GCP project
2. Enable the **Chrome Management API**: APIs & Services → Library → search "Chrome Management API" → Enable
3. Create a **Service Account**: APIs & Services → Credentials → Create Credentials → Service Account
4. Download a **JSON key file** for the service account

### Google Workspace Admin Console (admin.google.com)

1. Admin Roles → create or find a role with: Services → Chrome Management → Manage ChromeOS Devices (read only)
2. Assign that role to the service account email
3. Verify device reporting policies are enabled: Devices → Chrome → Settings → Device reporting

### Local Dependencies

```bash
# gcloud CLI (for activating the service account)
brew install google-cloud-sdk   # Mac
# or see Linux install options at cloud.google.com/sdk/docs/install

# Python packages
pip3 install google-auth requests
```

---

## Variables to Change

In `chrome_telemetry.sh`, the two config lines at the top are the only things that need updating per environment:

```bash
CHROME_CUSTOMER_ID="${CHROME_CUSTOMER_ID:-C044xpmyc}"
CHROME_KEY_FILE="${CHROME_KEY_FILE:-$(dirname "$0")/chromedevicemanagement-491916-387cd746f5a6.json}"
```

These can be changed three ways (highest to lowest priority):

1. **CLI flags**: `--customer`, `--key-file`
2. **Environment variables**: `export CHROME_CUSTOMER_ID=...` before running
3. **Edit the defaults** directly in the script

Your customer ID is found in Google Admin Console → Account → Account Settings.

---

## Known Environment Quirk

This environment has **SSL inspection** on outbound traffic — curl would return HTTP `000` without the `-k` flag. The script has `-sk` on all curl calls to handle this. If deploying elsewhere without SSL inspection, `-k` can safely be removed from the `api_get` function.

---

## Troubleshooting

`chrome_flex.sh` writes all HTTP errors to a log file instead of stderr, keeping error text out of nri-flex's output stream.

**Log location:** `$TMPDIR/chrome_flex_errors.log` (typically `/tmp/chrome_flex_errors.log`)

**Log format:**
```
[2026-10-19T15:17:32Z] HTTP 400 URL=https://chromemanagement.googleapis.com/...: {"error": {...}}
```

The URL field tells you exactly which API call failed and with what parameters, making it straightforward to identify whether the issue is a readMask field, a filter expression, or an event type not available for the account.

**Common commands:**

```bash
# Watch for errors in real time during a test run
tail -f /tmp/chrome_flex_errors.log

# Check for unexpected errors, filtering out known-benign 400s
grep -v "EXTERNAL_DISPLAY" /tmp/chrome_flex_errors.log

# Clear the log before a test run
> /tmp/chrome_flex_errors.log
```

### Expected 400 Errors

The `EXTERNAL_DISPLAY_CONNECTED` and `EXTERNAL_DISPLAY_DISCONNECTED` event types will produce HTTP 400 errors on accounts or Chrome OS versions where the `externalDisplayEvent` readMask field is not yet supported. These errors are **expected and benign**:

- The script uses `|| continue` for each event type, so a 400 on display events does not interrupt collection of any other event type.
- All other telemetry (network state changes, WiFi signal, OS crashes, USB, app events, etc.) continues to be collected normally.
- Display events will automatically begin appearing once Google enables this feature for the account.

To confirm that only the expected 400s are present:

```bash
grep -v "EXTERNAL_DISPLAY" /tmp/chrome_flex_errors.log
```

If this returns any output, there is an unexpected error that needs investigation.

---

## How the Script Works

### Authentication

The script uses `google-auth` (Python) to exchange the service account JSON key for a short-lived OAuth 2.0 access token scoped specifically to `chrome.management.telemetry.readonly`. This is necessary because `gcloud auth print-access-token` uses the wrong default scopes for this API.

### API Calls

All requests go to `https://chromemanagement.googleapis.com/v1/customers/{CUSTOMER}/telemetry/...` via `curl`, with the Bearer token in the `Authorization` header. Responses are JSON.

### Commands

| Command | What it queries |
|---|---|
| `monitor` | Device health + 9 event types in one report |
| `devices` | Raw device telemetry (CPU, memory, storage, battery, etc.) |
| `device <name>` | Single device by full resource name |
| `users` | Per-user telemetry |
| `events` | Raw events with custom filter |

### `monitor` Command Flow

1. Fetches all device health fields in one call (CPU, memory, storage, battery, network, OS update status)
2. Makes 9 separate event-type calls (network state, HTTPS latency, WiFi signal, OS crashes, USB add/remove, app install/uninstall), each filtered by `--since` timestamp (default: last 24 hours)
3. Combines everything into a single structured JSON report with counts per event type

### Pagination

The `--all-pages` flag loops through `nextPageToken` responses automatically, merging results into a single output. Default page size is 100 (max 1000).

### Data Flow

To avoid shell quoting/escaping issues with JSON, all data is passed between bash and Python via temp files rather than embedded in heredocs.

---

## Usage Examples

```bash
# Full health + event monitor report (last 24 hours)
./chrome_telemetry.sh monitor

# Monitor report for last 7 days, saved to file
./chrome_telemetry.sh monitor --since 7d --output report.json

# Monitor report for last 30 days
./chrome_telemetry.sh monitor --since 30d

# List devices with CPU and memory info
./chrome_telemetry.sh devices \
  --read-mask "name,serialNumber,cpuStatusReport,memoryStatusReport"

# Fetch all device pages and save to file
./chrome_telemetry.sh devices --all-pages \
  --read-mask "name,serialNumber,cpuStatusReport,memoryStatusReport,storageInfo" \
  --output devices.json

# Get a single device by resource name
./chrome_telemetry.sh device "customers/C044xpmyc/telemetry/devices/DEVICE_ID" \
  --read-mask "name,serialNumber,cpuInfo,batteryInfo"

# List network state change events since a date
./chrome_telemetry.sh events \
  --read-mask "device,user,networkStateChangeEvent" \
  --filter 'event_type=NETWORK_STATE_CHANGE AND timestamp>="2026-01-01T00:00:00Z"'

# Override customer and key file via environment variables
export CHROME_CUSTOMER_ID="C044xpmyc"
export CHROME_KEY_FILE="/path/to/sa-key.json"
./chrome_telemetry.sh monitor
```

---

## Event Types Reference

| event_type value | Trigger |
|---|---|
| `NETWORK_STATE_CHANGE` | Any network connection change |
| `NETWORK_HTTPS_LATENCY_CHANGE` | HTTPS latency issue detected or recovered |
| `WIFI_SIGNAL_STRENGTH_LOW` | WiFi drops below -70dBm |
| `WIFI_SIGNAL_STRENGTH_RECOVERED` | WiFi signal recovered |
| `OS_CRASH` | Crash occurred |
| `USB_ADDED` | USB device plugged in |
| `USB_REMOVED` | USB device removed |
| `APP_INSTALLED` | App installed |
| `APP_UNINSTALLED` | App uninstalled |
| `APP_LAUNCHED` | App launched |
| `AUDIO_SEVERE_UNDERRUN` | Audio buffer runs dry >5 seconds |
| `VPN_CONNECTION_STATE_CHANGE` | VPN connection change |
| `EXTERNAL_DISPLAY_CONNECTED` | External display connected *(may return HTTP 400 on accounts where this event type is unsupported — see Troubleshooting)* |
| `EXTERNAL_DISPLAY_DISCONNECTED` | External display disconnected *(may return HTTP 400 on accounts where this event type is unsupported — see Troubleshooting)* |
