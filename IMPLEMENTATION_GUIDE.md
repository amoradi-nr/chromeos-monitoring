# Chrome Device Telemetry — Implementation Guide

End-to-end steps for enrolling ChromeOS devices, querying the Chrome Management Telemetry API, and surfacing device health and events in New Relic.

![chromeos-monitoring-architecture](https://github.com/amoradi-nr/chromeos-monitoring/blob/main/chromeos_architecture.png?raw=true)

---

## Table of Contents

1. [Enroll Devices in Chrome Management](#1-enroll-devices-in-chrome-management)
2. [Enable Telemetry Reporting Policies](#2-enable-telemetry-reporting-policies)
3. [Google Cloud Project Setup](#3-google-cloud-project-setup)
4. [Service Account & Workspace Admin Role](#4-service-account--workspace-admin-role)
5. [Host Setup](#5-host-setup)
6. [Configure the Scripts](#6-configure-the-scripts)
7. [Test the Telemetry Scripts](#7-test-the-telemetry-scripts)
8. [Deploy the New Relic Flex Integration](#8-deploy-the-new-relic-flex-integration)
9. [Deploy Dashboards and Alerts](#9-deploy-dashboards-and-alerts)
10. [Verify Data in New Relic](#10-verify-data-in-new-relic)
11. [Configuration Reference](#11-configuration-reference)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Enroll Devices in Chrome Management

ChromeOS devices must be enrolled in your Google Workspace domain before any telemetry is available.

**Steps:**
1. Sign in to [Google Admin Console](https://admin.google.com) as a super admin
2. Go to **Devices → Chrome → Devices**
3. On a fresh ChromeOS device, power on and reach the sign-in screen
4. Press `Ctrl+Alt+E` to enter Enterprise Enrollment
5. Enter an admin email and password for your Google Workspace domain
6. The device will enroll and appear in **Devices → Chrome → Devices**

> Devices must be enrolled (not just signed in with a Workspace account) for management policies and telemetry reporting to apply.

---

## 2. Enable Telemetry Reporting Policies

Without these policies enabled, the API returns empty telemetry data.

1. Go to **Devices → Chrome → Settings → Device Settings**
2. Select the Organizational Unit (OU) containing your enrolled devices
3. Under **Device Reporting**, enable the following:

| Policy | What it enables |
|---|---|
| Report device state information | OS version, last reboot, enrollment |
| Report hardware status | CPU utilization, temperature, memory, storage |
| Report network configuration | Network type, MAC address, signal strength |
| Report OS and firmware version | OS update status |
| Report battery status | Charge percentage, voltage, health |
| Report crash report info | OS crash events |
| Report app information and activity | App install/uninstall/launch events |
| Report boot performance | Boot, shutdown, sleep, and wake times |
| Report kiosk app status | Kiosk application state and version |
| Report network bandwidth | Download and upload speed test results |
| Report audio/display status | Connected audio and display device details |

4. Click **Save**

> Policy changes propagate to devices within a few minutes. Telemetry data begins appearing in the API after the next device report cycle (typically every 1–24 hours depending on the policy).

---

## 3. Google Cloud Project Setup

The Chrome Management API is accessed via a Google Cloud service account.

1. Go to [Google Cloud Console](https://console.cloud.google.com)
2. Create or select a project
3. Navigate to **APIs & Services → Library**
4. Search for **Chrome Management API** and click **Enable**
5. Navigate to **APIs & Services → Credentials**
6. Click **Create Credentials → Service Account**
   - Give it a descriptive name (e.g., `chromeos-telemetry`)
   - Click **Create and Continue → Done**
7. Click the service account you just created → **Keys → Add Key → Create new key → JSON**
8. Save the downloaded JSON key file securely — this is your **service account key file**
9. Note the service account email address (format: `name@project-id.iam.gserviceaccount.com`)

---

## 4. Service Account & Workspace Admin Role

The service account must be granted read access to Chrome Management in your Workspace admin console.

1. Go to [Google Admin Console](https://admin.google.com)
2. Navigate to **Account → Admin Roles**
3. Click **Create a new role** (or use an existing one)
4. Under **Privileges**, enable:
   - **Services → Chrome Management → Manage ChromeOS Devices (read only)**
5. Click **Save**
6. Click **Assign service accounts** on the role
7. Enter the service account email from Step 3 and assign

> Your **customer ID** is found at: Admin Console → Account → Account Settings → Customer ID.
> Format: `C` followed by alphanumerics (e.g., `C044xpmyc`).

### Optional: Directory API Access for Partition-Level Disk Monitoring

> **This step is optional.** It enables `diskPartitionUsedPct` — a per-device disk usage percentage calculated against the user data partition, matching what Google Admin Console displays. Without it, `diskUsedPct` (whole physical drive) is still collected and fully functional.

The Admin SDK Directory API provides per-volume storage data not available in the Chrome Management Telemetry API. Accessing it requires **Domain-Wide Delegation (DWD)**, which allows the service account to impersonate a Workspace admin.

**Step 1 — Enable Admin SDK API in Google Cloud Console**

1. Go to **APIs & Services → Library** in the GCP project
2. Search for **Admin SDK API** and click **Enable**

**Step 2 — Enable Domain-Wide Delegation on the service account**

1. Go to **IAM & Admin → Service Accounts**
2. Click the service account → **Edit** → expand **Advanced settings**
3. Check **Enable Google Workspace Domain-wide Delegation** → Save
4. Note the **Unique ID** (OAuth Client ID) shown on the service account detail page

**Step 3 — Authorize the scope in Workspace Admin Console**

1. Go to **Security → Access and data control → API controls**
2. Click **Manage Domain Wide Delegation → Add new**
3. Paste the **Client ID** from Step 2
4. Add scope: `https://www.googleapis.com/auth/admin.directory.device.chromeos.readonly`
5. Click **Authorize**

> The impersonated account does not need to be a super-admin. A dedicated delegated admin with only **Services → Chrome Management → Manage ChromeOS Devices (read only)** privileges is sufficient and recommended.

**Step 4 — Configure `CHROME_ADMIN_EMAIL`**

Set `CHROME_ADMIN_EMAIL` to the Workspace admin email to impersonate (see [Section 6](#6-configure-the-scripts)). If this variable is not set, `chrome_flex.sh` skips the Directory API call entirely and the partition fields are simply absent from events — no errors are produced.

---

## 5. Host Setup

The telemetry scripts run on any Mac or Linux host with outbound HTTPS access to `chromemanagement.googleapis.com`.

### Required software

```bash
# macOS
brew install google-cloud-sdk
pip3 install google-auth requests

# Linux (Debian/Ubuntu)
sudo apt-get install google-cloud-cli
sudo apt install python3-google-auth python3-requests
# or if apt packages are unavailable:
sudo pip3 install --break-system-packages google-auth requests
```

### SSL inspection note

If your network performs SSL inspection (common in corporate environments), `curl` will return HTTP `000` without the `-k` flag. The scripts include `-sk` by default to handle this. If deploying in an environment without SSL inspection, replace `-sk` with `-s` in the `api_get()` function in `chrome_flex.sh` and `chrome_telemetry.sh`.

---

## 6. Configure the Scripts

### `chrome_telemetry.sh` and `chrome_flex.sh`

Both scripts share the same configuration variables at the top. Edit these directly or override with environment variables.

| Variable | Required | Description |
|---|---|---|
| `CHROME_CUSTOMER_ID` | Yes | Your Google Workspace customer ID (e.g., `C044xpmyc`) |
| `CHROME_KEY_FILE` | Yes | Absolute path to the service account JSON key file |
| `CHROME_ADMIN_EMAIL` | No | Workspace admin email for Directory API DWD — enables `diskPartitionUsedPct` (see [Section 4 — Optional](#optional-directory-api-access-for-partition-level-disk-monitoring)) |

**Edit directly in the script:**
```bash
CHROME_CUSTOMER_ID="${CHROME_CUSTOMER_ID:-YOUR_CUSTOMER_ID}"
CHROME_KEY_FILE="${CHROME_KEY_FILE:-/path/to/your-sa-key.json}"
```

**Or set as environment variables (overrides the defaults):**
```bash
export CHROME_CUSTOMER_ID="C044xpmyc"
export CHROME_KEY_FILE="/opt/chrome-telemetry/sa-key.json"
```

### `chrome-device-telemetry-flex.yml`

| Setting | Required | Description |
|---|---|---|
| `interval` | Yes | How often the integration runs (e.g., `300s`, `600s`, `900s`) |
| `CHROME_CUSTOMER_ID` (env) | Yes | Customer ID passed to the script |
| `CHROME_KEY_FILE` (env) | Yes | Key file path passed to the script |
| `CHROME_ADMIN_EMAIL` (env) | No | Workspace admin email — add to the health command `env` block to enable `diskPartitionUsedPct` |
| `--since` argument | Yes | Events lookback window — should slightly exceed the poll interval (e.g., `6m` for a `300s` interval) |
| Script path (`/opt/chrome-telemetry/chrome_flex.sh`) | Yes | Path where `chrome_flex.sh` is deployed on the host |

**To enable partition-level disk monitoring**, add `CHROME_ADMIN_EMAIL` to the health command env block in `chrome-device-telemetry-flex.yml`:

```yaml
env:
  CHROME_CUSTOMER_ID: C044xpmyc
  CHROME_KEY_FILE: /opt/chrome-telemetry/chromedevicemanagement-491916-387cd746f5a6.json
  CHROME_ADMIN_EMAIL: admin@yourdomain.com   # optional — enables diskPartitionUsedPct
```

Restart the agent after updating the YAML: `sudo systemctl restart newrelic-infra`

**Poll interval vs. events lookback window:**

| interval | --since |
|---|---|
| 300s (5 min) | `6m` |
| 600s (10 min) | `11m` |
| 900s (15 min) | `16m` |
| 3600s (1 hour) | `62m` |

### `chrome_nr_deploy.py`

| Variable | Description |
|---|---|
| `ACCOUNT_ID` | New Relic account ID |
| `API_ENDPOINT` | `https://api.newrelic.com/graphql` (US) or `https://api.eu.newrelic.com/graphql` (EU) |

---

## 7. Test the Telemetry Scripts

Verify the API connection before deploying to New Relic.

```bash
cd /path/to/CHROMEBOX

# Full monitor report (device health + all event types, last 24h)
./chrome_telemetry.sh monitor

# Single-page device health query
./chrome_telemetry.sh devices \
  --read-mask "name,serialNumber,cpuStatusReport,memoryStatusReport"

# Flex health output (JSON array, one object per device)
./chrome_flex.sh health | python3 -m json.tool

# Flex events output (last 6 minutes)
./chrome_flex.sh events --since 6m | python3 -m json.tool

# Count devices returned
./chrome_flex.sh health | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(f'{len(d)} device(s) found')"
```

**Expected output for `chrome_flex.sh health`** (key fields shown):
```json
[
  {
    "deviceId": "5cdea71d-ccd9-4755-a1e1-23f8f197fd0e",
    "serialNumber": "Flex-d2:48:a8:2b:4a:52",
    "cpuModel": "Intel Core Processor (Skylake)",
    "cpuUtilizationPct": 54,
    "cpuTemperatureCelsius": 48.2,
    "memoryUsedPct": 21.7,
    "diskUsedPct": 7.0,
    "batteryChargePct": 95,
    "bootDurationSeconds": 28.4,
    "netDownloadSpeedKbps": 45000,
    "kioskAppState": "APP_STATUS_STARTED",
    "heartbeatState": "ONLINE"
  }
]
```

---

## 8. Deploy the New Relic Flex Integration

### Install the New Relic Infrastructure Agent

```bash
# Linux (automated installer)
curl -Ls https://download.newrelic.com/install/newrelic-cli/scripts/install.sh | bash
sudo NEW_RELIC_API_KEY=<your-license-key> NEW_RELIC_ACCOUNT_ID=<account-id> \
  /usr/local/bin/newrelic install
```

Or manually:
```bash
echo "license_key: YOUR_LICENSE_KEY_HERE" | sudo tee /etc/newrelic-infra.yml
sudo systemctl start newrelic-infra && sudo systemctl enable newrelic-infra
```

### Deploy the scripts and Flex config

```bash
# Create deployment directory
sudo mkdir -p /opt/chrome-telemetry

# Copy scripts and key file
sudo cp chrome_flex.sh /opt/chrome-telemetry/
sudo cp your-sa-key.json /opt/chrome-telemetry/
sudo chmod +x /opt/chrome-telemetry/chrome_flex.sh

# Install Python dependencies
sudo apt install python3-google-auth python3-requests
# or: sudo pip3 install --break-system-packages google-auth requests

# Deploy Flex config
sudo cp chrome-device-telemetry-flex.yml /etc/newrelic-infra/integrations.d/

# Restart agent to load the new Flex config
sudo systemctl restart newrelic-infra
```

### After updating `chrome_flex.sh`

When `chrome_flex.sh` is updated, copy it to the host — no agent restart is required. The infrastructure agent executes the script fresh on each poll cycle, so changes take effect automatically at the next interval.

```bash
sudo cp chrome_flex.sh /opt/chrome-telemetry/chrome_flex.sh
```

> A restart is only needed if you change `chrome-device-telemetry-flex.yml` (the Flex config itself).

---

## 9. Deploy Dashboards and Alerts

### Obtain a New Relic User API Key

> This is different from the license key used for data ingestion.
> Location: [one.newrelic.com](https://one.newrelic.com) → (your avatar, top right) → **API keys** → **Create a key** → Type: **User**
> Format: starts with `NRAK-`

### Run the deploy script

`chrome_nr_deploy.py` is the single script for both initial setup and ongoing updates. It is fully idempotent — safe to run multiple times:

- **Dashboard**: creates if not found, updates if it already exists
- **Alert policy**: creates if not found, reuses if it already exists
- **Alert conditions**: checks what's already in the policy and only creates the missing ones — never duplicates

```bash
export NR_API_KEY=NRAK-xxxxxxxxxxxxxxxxxxxx
python3 chrome_nr_deploy.py
```

> To target a specific existing dashboard: `export DASHBOARD_GUID=<guid>` before running.
> To target a specific alert policy: `export POLICY_ID=<id>` before running.

### Alert conditions — full set (13 total)

| Condition | Threshold | Severity |
|---|---|---|
| High CPU Utilization | avg > 90% for 5 min | Critical |
| High Memory Usage | avg > 85% for 5 min | Critical |
| Low Disk Space | avg > 90% | Critical |
| OS Crash Detected | any crash event | Critical |
| Kiosk App Not Running | app state ≠ STARTED | Critical |
| High CPU Temperature | avg > 85°C for 5 min | Critical |
| Heartbeat Offline | state = OFFLINE | Critical |
| WiFi Signal Degraded | any low signal event | Warning |
| Elevated CPU Temperature | avg > 75°C for 5 min | Warning |
| Slow Boot Time | avg > 60 seconds | Warning |
| Low Download Bandwidth | avg < 1 Mbps | Warning |
| Device Not Reporting | no data for 2 hours (lost signal) | Warning |
| Kiosk App Stopped Reporting | no data for 1 hour (lost signal) | Critical |

To receive alert notifications, configure a notification channel:
> Alerts → Alert Policies → Chrome Device Telemetry Alerts → Notification channels

---

## 10. Verify Data in New Relic

After the first Flex poll completes (wait one `interval` period), run these NRQL queries in [New Relic Query Builder](https://one.newrelic.com/data-exploration):

```sql
-- Confirm device health data is arriving
FROM ChromeDeviceHealthSample SELECT * SINCE 1 hour ago LIMIT 5

-- List all devices with key metrics
FROM ChromeDeviceHealthSample
SELECT latest(cpuUtilizationPct), latest(memoryUsedPct), latest(diskUsedPct),
       latest(cpuTemperatureCelsius), latest(kioskAppState), latest(heartbeatState)
FACET serialNumber SINCE 1 hour ago

-- Confirm event data is arriving, broken down by event type
FROM ChromeTelemetryEventSample SELECT count(*) FACET telemetryCategory SINCE 1 day ago

-- Check for HTTP status codes from network events
FROM ChromeTelemetryEventSample
SELECT httpStatusCode, problem, deviceConnectionState, serialNumber, reportTime
WHERE httpStatusCode IS NOT NULL OR problem IS NOT NULL
SINCE 7 days ago LIMIT 50

-- Check Flex integration health
FROM flexStatusSample SELECT * SINCE 30 minutes ago
```

**Also check the error log on the collection host** to confirm no unexpected API errors:

```bash
# Filter out the known-benign 400s for external display events
grep -v "EXTERNAL_DISPLAY" /tmp/chrome_flex_errors.log
```

If this command returns output, see [Section 12 — Troubleshooting](#12-troubleshooting).

**Note on event data volume:** Most telemetry event types (OS crashes, app installs, USB connections, WiFi degradation) are low-frequency. The events dashboard may appear sparse immediately after deployment — this is normal. Use a wider time window (`SINCE 7 days ago`) or run `./chrome_flex.sh events --since 7d` on the collection host to verify events are being captured over a longer horizon.

---

## 11. Configuration Reference

### Environment Variables

| Variable | Used in | Description |
|---|---|---|
| `CHROME_CUSTOMER_ID` | `chrome_telemetry.sh`, `chrome_flex.sh` | Google Workspace customer ID |
| `CHROME_KEY_FILE` | `chrome_telemetry.sh`, `chrome_flex.sh` | Absolute path to service account JSON key file |
| `CHROME_ADMIN_EMAIL` | `chrome_flex.sh` | **Optional.** Workspace admin email for Directory API DWD — enables `diskPartitionUsedPct`. See [Section 4 — Optional](#optional-directory-api-access-for-partition-level-disk-monitoring). |
| `NR_API_KEY` | `chrome_nr_deploy.py` | New Relic User API key (`NRAK-...`) |
| `DASHBOARD_GUID` | `chrome_nr_deploy.py` | Optional: target a specific dashboard GUID instead of searching by name |
| `POLICY_ID` | `chrome_nr_deploy.py` | Optional: target a specific alert policy ID instead of searching by name |

### Script Quick Reference

| Script | Purpose | Key flags / notes |
|---|---|---|
| `chrome_telemetry.sh monitor` | Manual health + event report | `--since 7d`, `--output file.json` |
| `chrome_telemetry.sh devices` | Query raw device health | `--read-mask`, `--filter`, `--all-pages` |
| `chrome_telemetry.sh events` | Query raw telemetry events | `--filter 'event_type=OS_CRASH'` |
| `chrome_flex.sh health` | Flex-formatted device health (JSON array) | One flat object per device |
| `chrome_flex.sh events` | Flex-formatted events (JSON array) | `--since 6m` |
| `chrome_nr_deploy.py` | Deploy or update dashboard + all 13 alert conditions | `NR_API_KEY` required; idempotent — safe to re-run |

### Dashboard Pages

| Page | Contents |
|---|---|
| **Fleet Summary** | Architecture diagram, threshold reference, 3 rows of KPI billboards (health + event counts), full device health scorecard, per-device CPU/memory/disk bar charts, offline detection table |
| **Device Health** | CPU utilization + temperature trends, memory, disk, battery, gateway ping, download/upload bandwidth, boot duration, memory page faults, disk I/O, OS update status, network info, battery detail, CPU/storage hardware info |
| **Kiosk & App Status** | Kiosk app state per device, boot/shutdown/wake/sleep duration trends, audio device status, display info, boot performance detail, app launch events, app install/uninstall events |
| **Network & Connectivity** | Gateway ping, DNS latency, WiFi signal strength, TX rate, download/upload bandwidth trends, network state changes table, HTTPS latency events table, WiFi signal events, VPN events, network device snapshot |
| **Events** | All 14 event types — 2 rows of count billboards, 7-day activity timeline, individual detail tables per event type, event type reference panel |

### `ChromeDeviceHealthSample` Fields

| Field | Description |
|---|---|
| `serialNumber`, `deviceId` | Device identifiers |
| `cpuModel`, `cpuArchitecture` | CPU hardware info |
| `cpuUtilizationPct` | CPU usage percentage |
| `cpuTemperatureCelsius`, `cpuTemperatureMaxCelsius` | Average and peak CPU temperature |
| `memoryTotalBytes`, `memoryFreeBytes`, `memoryUsedPct` | RAM stats |
| `memoryPageFaults` | Memory page fault count |
| `diskTotalBytes`, `diskAvailableBytes`, `diskUsedPct` | Whole physical drive capacity stats — `diskUsedPct` = (total − available) ÷ total |
| `diskPartitionTotalBytes`, `diskPartitionFreeBytes`, `diskPartitionUsedPct` | **Optional** — user data partition stats sourced from the Directory API. `diskPartitionUsedPct` matches what Google Admin Console displays. Only present when `CHROME_ADMIN_EMAIL` is configured. |
| `diskModel`, `diskType`, `diskBytesReadThisSession`, `diskBytesWrittenThisSession` | Disk I/O detail |
| `batteryChargePct`, `batteryVoltage`, `batteryHealth` | Battery status |
| `batteryManufacturer`, `batteryDesignCapacity`, `batteryFullCapacity` | Battery hardware detail |
| `networkType`, `networkMacAddress` | Network interface info |
| `networkGatewayPing`, `networkDnsLatency`, `networkSignalStrength`, `networkTransmitRate` | Network performance |
| `netDownloadSpeedKbps`, `netUploadSpeedKbps`, `netSpeedTestAgeMs` | Bandwidth test results |
| `bootDurationSeconds`, `shutdownDurationSeconds`, `wakeUpDurationSeconds`, `sleepDurationSeconds` | Boot performance |
| `bootReportTime` | Timestamp of the boot performance report |
| `osLastUpdateTime`, `osLastCheckTime`, `osLastRebootTime`, `osUpdateState` | OS update status |
| `kioskAppState`, `kioskAppId`, `kioskAppVersion`, `kioskReportTime` | Kiosk application status |
| `heartbeatState`, `heartbeatReportTime` | Device connectivity heartbeat (`ONLINE` / `OFFLINE`) |
| `displayCount`, `display0Name`, `display0ResolutionWidth`, `display0ResolutionHeight`, `display0RefreshRate`, `display0IsInternal` | Primary display info |
| `display1Name` | Secondary display name (if connected) |
| `audioOutputCount`, `audioOutput0Name`, `audioOutput0Active`, `audioOutput0Muted`, `audioOutput0Volume` | Audio output device |
| `audioInputCount`, `audioInput0Name`, `audioInput0Active` | Audio input device |

### `ChromeTelemetryEventSample` Fields

| Field | Description |
|---|---|
| `telemetryCategory` | Chrome telemetry event category (see Event Types below) |
| `deviceId`, `serialNumber`, `orgUnitId` | Device identifiers |
| `userEmail` | Associated user (if applicable) |
| `reportTime` | Timestamp of the event |
| `httpStatusCode` | HTTP response code from connectivity probe (403 = captive portal, 503 = service unavailable) |
| `problem` | Root cause for HTTPS latency events: `FAILED_HTTP_REQUESTS`, `FAILED_DNS_RESOLUTIONS`, `HIGH_LATENCY`, `VERY_HIGH_LATENCY` |
| `deviceConnectionState` | Network state: `ONLINE`, `OFFLINE`, `PORTAL`, `CONNECTING` |
| `signalStrengthDbm` | WiFi signal strength in dBm (WiFi events) |
| `appId` | Application identifier (app install/uninstall/launch events) |

### Event Types Reference

| Event Type | Trigger | Key diagnostic fields |
|---|---|---|
| `OS_CRASH` | Kernel or browser crash | `serialNumber`, `reportTime` |
| `WIFI_SIGNAL_STRENGTH_LOW` | WiFi dropped below –70 dBm | `signalStrengthDbm` |
| `WIFI_SIGNAL_STRENGTH_RECOVERED` | WiFi signal restored | `signalStrengthDbm` |
| `NETWORK_STATE_CHANGE` | Network connection state changed | `deviceConnectionState`, `httpStatusCode` |
| `NETWORK_HTTPS_LATENCY_CHANGE` | HTTPS latency problem detected/resolved | `problem`, `httpStatusCode`, `routineData_verdict`, `routineData_latency` |
| `USB_ADDED` | USB peripheral connected | `serialNumber` |
| `USB_REMOVED` | USB peripheral disconnected | `serialNumber` |
| `APP_INSTALLED` | Application installed | `appId` |
| `APP_UNINSTALLED` | Application removed | `appId` |
| `APP_LAUNCHED` | Application launched | `appId` |
| `VPN_CONNECTION_STATE_CHANGE` | VPN tunnel state changed | `serialNumber` |
| `AUDIO_SEVERE_UNDERRUN` | Audio buffer ran dry for > 5 seconds | `serialNumber`, `reportTime` |
| `EXTERNAL_DISPLAY_CONNECTED` | External display connected | `serialNumber`, `reportTime` |
| `EXTERNAL_DISPLAY_DISCONNECTED` | External display disconnected | `serialNumber`, `reportTime` |

### Known Environment Quirks

**SSL inspection:** The `-sk` flag on all `curl` calls handles environments where outbound HTTPS traffic is inspected. If deploying on a host without SSL inspection, change `-sk` to `-s` in the `api_get()` function in `chrome_flex.sh`.

**Attribute name sanitization:** The `run_events()` function in `chrome_flex.sh` uses a recursive `flatten()` with `sanitize_key()` that replaces any non-alphanumeric character in JSON field names with underscores. This prevents New Relic from splitting attributes on `/`, `.`, or `:`. HTTP response-line keys (e.g., `HTTP/1.1`) are explicitly mapped to `httpStatusCode`.

**Token scoping:** `gcloud auth print-access-token` does not work for this integration — it uses the wrong OAuth scopes. All scripts use `google.oauth2.service_account` via Python to obtain a token scoped to `chrome.management.telemetry.readonly`.

**Reserved attribute names:** The telemetry event category is stored as `telemetryCategory`. Both `eventType` and any attribute whose name ends in `EventType` are reserved by the New Relic infrastructure agent — `eventType` names the NRDB table, and attributes ending in `EventType` are silently dropped at ingest. `telemetryCategory` avoids both conflicts. All NRQL queries and dashboard widgets use `telemetryCategory` to filter and facet on event category.

**Expected HTTP 400 errors for external display events:** The `EXTERNAL_DISPLAY_CONNECTED` and `EXTERNAL_DISPLAY_DISCONNECTED` event types query the `externalDisplayEvent` readMask field, which is only available on newer Chrome OS versions or certain Workspace editions. On accounts where this field is not supported, the API returns HTTP 400 for those two event types. This is **expected and benign** — `chrome_flex.sh` uses `|| continue` per event type, so a 400 on display events does not affect the collection of any other event type. These events will automatically begin populating once Google enables the feature for the account. See [Section 12 — Troubleshooting](#12-troubleshooting) for how to confirm only these expected 400s are present.

**Error log instead of stderr:** `chrome_flex.sh` writes all HTTP errors to `$TMPDIR/chrome_flex_errors.log` (typically `/tmp/chrome_flex_errors.log`) rather than stderr. This prevents error text from leaking into nri-flex's output stream, which would produce garbled events in NRDB. Each log entry includes the full URL of the failing request, making it straightforward to identify which API call failed and why.

---

## 12. Troubleshooting

### Error Log

`chrome_flex.sh` writes all HTTP errors from the Chrome Management API to a persistent log file on the collection host.

**Location:** `/tmp/chrome_flex_errors.log` (or `$TMPDIR/chrome_flex_errors.log`)

**Format:**
```
[2026-10-19T15:17:32Z] HTTP 400 URL=https://chromemanagement.googleapis.com/v1/customers/C044xpmyc/telemetry/events?readMask=device,user,externalDisplayEvent&filter=...: {"error": {"code": 400, "message": "read_mask is invalid", "status": "INVALID_ARGUMENT"}}
```

Each entry contains:
- **Timestamp** (UTC)
- **HTTP status code**
- **Full URL** of the failing request — including the readMask, filter, and pageSize parameters
- **Full API error body** — the `message` field identifies the root cause

**Useful commands:**

```bash
# Watch for errors in real time
tail -f /tmp/chrome_flex_errors.log

# Check for unexpected errors (filter out known-benign 400s)
grep -v "EXTERNAL_DISPLAY" /tmp/chrome_flex_errors.log

# Clear the log before a clean test run
> /tmp/chrome_flex_errors.log

# Run health manually and immediately check the log
./chrome_flex.sh health > /dev/null && echo "OK" || cat /tmp/chrome_flex_errors.log

# Run events manually over a wider window and check the log
./chrome_flex.sh events --since 7d > /dev/null && echo "OK" || cat /tmp/chrome_flex_errors.log
```

---

### Expected 400 Errors

Some HTTP 400 errors are **expected and do not indicate a problem** with the integration.

| Failing URL contains | Why it 400s | Impact |
|---|---|---|
| `EXTERNAL_DISPLAY_CONNECTED` or `EXTERNAL_DISPLAY_DISCONNECTED` | The `externalDisplayEvent` readMask field is only available on newer Chrome OS versions or certain Workspace editions | None — `|| continue` skips this event type; all others are collected normally |

To confirm only these expected 400s are present:

```bash
grep -v "EXTERNAL_DISPLAY" /tmp/chrome_flex_errors.log
```

If the output is empty, the integration is working correctly. If there is any other output, use the URL and error message in the log entry to diagnose — common causes are listed below.

---

### Common Errors and Fixes

| Error message | Likely cause | Fix |
|---|---|---|
| `read_mask is invalid` | A field name in the readMask does not exist for this account, Workspace edition, or Chrome OS version | Remove the offending field from the readMask in `chrome_flex.sh`. The URL in the log shows the exact readMask. |
| `Request contains an invalid argument` | The filter expression is malformed, or the event type in the filter is not a valid enum value | Check the filter parameter in the URL. Verify the event type name matches the Chrome Management API enum exactly. |
| `PERMISSION_DENIED` (403) | The service account does not have the Chrome Management read role, or domain-wide delegation is missing | Re-check [Section 4](#4-service-account--workspace-admin-role). |
| HTTP `000` | SSL inspection on the network is stripping the TLS certificate | Confirm `-sk` is present on all `curl` calls in `api_get()` in `chrome_flex.sh`. |
| `invalid_grant` in token fetch | The service account JSON key has expired or been revoked | Generate a new key in Google Cloud Console → IAM & Admin → Service Accounts. |

---

### Dashboard Shows No Data

1. **Check the error log first:** `grep -v "EXTERNAL_DISPLAY" /tmp/chrome_flex_errors.log` — any output here is blocking data collection.
2. **Confirm the script produces output:** Run `./chrome_flex.sh health | python3 -m json.tool` on the collection host. A non-empty JSON array means health data is flowing.
3. **Check event volume with a wide window:** Run `./chrome_flex.sh events --since 7d | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{len(d)} event(s)')"`. Most event types are low-frequency — an empty result over 7 days means no qualifying events occurred, not a script failure.
4. **Verify nri-flex is running:** `sudo systemctl status newrelic-infra` and `FROM flexStatusSample SELECT * SINCE 30 minutes ago` in New Relic Query Builder.
5. **Check telemetry policies:** If the health script returns devices but key fields (e.g., `cpuUtilizationPct`) are empty, the relevant Device Reporting policies may not be enabled — see [Section 2](#2-enable-telemetry-reporting-policies).
