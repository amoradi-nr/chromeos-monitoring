#!/usr/bin/env python3
"""
Deploy or update the Chrome Device Telemetry dashboard and alert policy.

Idempotent — safe to run multiple times:
  - Dashboard:         creates if not found, updates if it already exists
  - Alert policy:      creates if not found, reuses if it already exists
  - Alert conditions:  queries existing conditions and only creates missing ones

USAGE:
    NR_API_KEY=NRAK-... python3 chrome_nr_deploy.py

OPTIONAL ENV VARS:
    DASHBOARD_GUID   target a specific dashboard GUID instead of searching by name
    POLICY_ID        target a specific alert policy ID instead of searching by name
"""

import json, os, sys, urllib.request, urllib.error, base64

ACCOUNT_ID     = 7176602
API_ENDPOINT   = "https://api.newrelic.com/graphql"
NR_API_KEY     = os.environ.get("NR_API_KEY", "")
DASHBOARD_GUID = os.environ.get("DASHBOARD_GUID", "")
POLICY_ID      = os.environ.get("POLICY_ID", "")

# ---------------------------------------------------------------------------
# NerdGraph helper
# ---------------------------------------------------------------------------
def graphql(query, variables=None):
    if not NR_API_KEY:
        print("ERROR: NR_API_KEY not set.", file=sys.stderr)
        print("       export NR_API_KEY=NRAK-...", file=sys.stderr)
        sys.exit(1)
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(API_ENDPOINT, data=payload,
          headers={"Content-Type": "application/json", "API-Key": NR_API_KEY})
    try:
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}", file=sys.stderr); sys.exit(1)
    if "errors" in data:
        print(f"GraphQL errors:\n{json.dumps(data['errors'], indent=2)}", file=sys.stderr)
        sys.exit(1)
    return data

# ---------------------------------------------------------------------------
# Dashboard widget helpers
# ---------------------------------------------------------------------------
def widget(title, viz, query, col, row, width, height, extra_config=None):
    cfg = {
        "nrqlQueries": [{"accountIds": [ACCOUNT_ID], "query": query}],
        "platformOptions": {"ignoreTimeRange": False},
    }
    if extra_config:
        cfg.update(extra_config)
    return {
        "title": title,
        "layout": {"column": col, "row": row, "width": width, "height": height},
        "linkedEntityGuids": None,
        "visualization": {"id": viz},
        "rawConfiguration": cfg,
    }

def markdown_widget(text, col, row, width, height):
    return {
        "title": "",
        "layout": {"column": col, "row": row, "width": width, "height": height},
        "linkedEntityGuids": None,
        "visualization": {"id": "viz.markdown"},
        "rawConfiguration": {"text": text},
    }

# ---------------------------------------------------------------------------
# Architecture diagram
# ---------------------------------------------------------------------------
def _mermaid_url(definition):
    encoded = base64.b64encode(definition.encode("utf-8")).decode("ascii")
    return f"https://mermaid.ink/img/{encoded}"

ARCH_DIAGRAM = _mermaid_url("""%%{init: {
  'theme': 'base',
  'flowchart': { 'nodeSpacing': 60, 'rankSpacing': 90, 'padding': 24 },
  'themeVariables': {
    'fontFamily': 'trebuchet ms, sans-serif',
    'fontSize': '14px',
    'lineColor': '#5f6368',
    'edgeLabelBackground': '#ffffff'
  }
}}%%
flowchart LR
    subgraph fleet["🖥️   ChromeOS Fleet"]
        d1(["ChromeBox 1"])
        d2(["ChromeBox 2"])
        d3(["ChromeBox N"])
    end

    subgraph gws["🔐   Google Workspace"]
        admin(["Admin Console"])
    end

    subgraph gcp["☁️   Google Cloud"]
        api(["Chrome Management\\nTelemetry API"])
    end

    subgraph col["⚙️   Collection Host"]
        sa(["Service Account\\nKey"])
        flex(["chrome_flex.sh\\nnri-flex · 5 min"])
        agent(["NR Infra Agent"])
    end

    subgraph nr["📊   New Relic"]
        nrdb[("NRDB")]
        dash(["Dashboard"])
        alrt(["Alerts"])
    end

    admin -. "policies" .-> d1
    admin -. "policies" .-> d2
    admin -. "policies" .-> d3
    d1 -- "HTTPS" --> api
    d2 -- "HTTPS" --> api
    d3 -- "HTTPS" --> api
    sa -- "OAuth 2.0" --> flex
    flex -- "REST API poll" --> api
    flex --> agent
    agent -- "HTTPS" --> nrdb
    nrdb --> dash
    nrdb --> alrt

    classDef chromeos   fill:#4285f4,color:#ffffff,stroke:#1a73e8,stroke-width:2px
    classDef gworkspace fill:#fbbc04,color:#3c4043,stroke:#f29900,stroke-width:2px
    classDef gcloud     fill:#34a853,color:#ffffff,stroke:#1e8e3e,stroke-width:2px
    classDef sakey      fill:#ea4335,color:#ffffff,stroke:#c5221f,stroke-width:2px
    classDef collection fill:#ff6d00,color:#ffffff,stroke:#e65100,stroke-width:2px
    classDef newrelic   fill:#00ac69,color:#ffffff,stroke:#007a4b,stroke-width:2px

    class d1,d2,d3 chromeos
    class admin gworkspace
    class api gcloud
    class sa sakey
    class flex,agent collection
    class nrdb,dash,alrt newrelic

    style fleet fill:#e8f0fe,stroke:#4285f4,stroke-width:2px,color:#1a73e8
    style gws   fill:#fef7e0,stroke:#fbbc04,stroke-width:2px,color:#3c4043
    style gcp   fill:#e6f4ea,stroke:#34a853,stroke-width:2px,color:#1e8e3e
    style col   fill:#fff3e0,stroke:#ff6d00,stroke-width:2px,color:#e65100
    style nr    fill:#e6f9f0,stroke:#00ac69,stroke-width:2px,color:#007a4b
""")

# ===========================================================================
# Dashboard pages
# ===========================================================================

def page_fleet_summary():
    header = """## 🖥️ Chrome Device Fleet — Health Overview

Real-time status for all enrolled ChromeOS devices. Dashboard auto-refreshes on the page time picker interval.

| Metric | 🟡 Warning | 🔴 Critical |
|---|---|---|
| CPU Utilization | > 70% | > 90% |
| Memory Used | > 75% | > 85% |
| Disk Used | > 80% | > 90% |
| Battery Charge | < 20% | < 10% |
| CPU Temperature | > 75°C | > 85°C |
| Boot Time | > 60 s | — |
| Download Speed | < 1 Mbps | — |

> **Offline Detection:** *Last Seen* shows the UNIX epoch ms of the most recent health report. Values older than your poll interval indicate a potentially offline device.
> **Kiosk Alert:** Any device not in `APP_STATUS_STARTED` within the last 30 minutes needs attention."""

    return {
        "name": "Fleet Summary",
        "description": "At-a-glance KPIs, fleet scorecard, kiosk status, and offline detection",
        "widgets": [
            # Row 1-5 — Architecture diagram
            markdown_widget(f"""## 🔄 ChromeOS → Chrome Management → New Relic

![Data flow architecture]({ARCH_DIAGRAM})

**Key points:**
- ChromeOS devices report telemetry **automatically** — no agent runs on the devices themselves
- `chrome_flex.sh` polls the Chrome Management Telemetry API every **5 minutes** using a Google Service Account (OAuth 2.0)
- Two event types land in New Relic: `ChromeDeviceHealthSample` (one per device per poll) and `ChromeTelemetryEventSample` (one per captured telemetry event)""",
                   col=1, row=1, width=12, height=5),

            # Row 6-8 — Threshold reference
            markdown_widget(header, col=1, row=6, width=12, height=3),

            # Row 9-11 — Health KPIs row 1
            widget("Devices Active (30 min)", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT uniqueCount(serialNumber) AS 'Active' SINCE 30 minutes ago",
                   col=1, row=9, width=3, height=3),
            widget("Total Events (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' SINCE 1 day ago",
                   col=4, row=9, width=3, height=3),
            widget("OS Crashes (7d)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Crashes' WHERE telemetryCategory = 'OS_CRASH' SINCE 7 days ago",
                   col=7, row=9, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "CRITICAL", "value": 1}]}),
            widget("WiFi Signal Low (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'WIFI_SIGNAL_STRENGTH_LOW' SINCE 1 day ago",
                   col=10, row=9, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 1}]}),

            # Row 12-14 — Health KPIs row 2
            widget("Kiosk App Running", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT uniqueCount(serialNumber) AS 'Running' WHERE kioskAppState = 'APP_STATUS_STARTED' SINCE 30 minutes ago",
                   col=1, row=12, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 0}]}),
            widget("Avg CPU Temp (°C)", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(cpuTemperatureCelsius) AS 'Temp °C' SINCE 1 hour ago",
                   col=4, row=12, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING",  "value": 75},
                                                {"alertSeverity": "CRITICAL", "value": 85}]}),
            widget("Avg Download Speed (Kbps)", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(netDownloadSpeedKbps) AS 'Kbps' SINCE 1 hour ago",
                   col=7, row=12, width=3, height=3),
            widget("Avg Boot Time (sec)", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(bootDurationSeconds) AS 'Seconds' SINCE 1 day ago",
                   col=10, row=12, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 60}]}),

            # Row 15-17 — Event KPIs row 3
            widget("HTTPS Latency Issues (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'NETWORK_HTTPS_LATENCY_CHANGE' SINCE 1 day ago",
                   col=1, row=15, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 1}]}),
            widget("VPN Changes (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'VPN_CONNECTION_STATE_CHANGE' SINCE 1 day ago",
                   col=4, row=15, width=3, height=3),
            widget("Network State Changes (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'NETWORK_STATE_CHANGE' SINCE 1 day ago",
                   col=7, row=15, width=3, height=3),
            widget("Audio Underruns (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'AUDIO_SEVERE_UNDERRUN' SINCE 1 day ago",
                   col=10, row=15, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 1}]}),

            # Row 18-22 — Full device health scorecard
            widget("Device Health Scorecard", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(cpuUtilizationPct) AS 'CPU %', "
                    "       latest(cpuTemperatureCelsius) AS 'CPU °C', "
                    "       latest(memoryUsedPct) AS 'Memory %', "
                    "       latest(diskUsedPct) AS 'Disk %', "
                    "       latest(batteryChargePct) AS 'Battery %', "
                    "       latest(batteryHealth) AS 'Batt Health', "
                    "       latest(kioskAppState) AS 'Kiosk', "
                    "       latest(heartbeatState) AS 'Heartbeat', "
                    "       latest(networkType) AS 'Network', "
                    "       latest(timestamp) AS 'Last Seen (ms)' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=1, row=18, width=12, height=5),

            # Row 23-26 — Per-device bar charts
            widget("CPU % by Device", "viz.bar",
                   "FROM ChromeDeviceHealthSample SELECT latest(cpuUtilizationPct) FACET serialNumber SINCE 1 hour ago",
                   col=1, row=23, width=4, height=4),
            widget("Memory % by Device", "viz.bar",
                   "FROM ChromeDeviceHealthSample SELECT latest(memoryUsedPct) FACET serialNumber SINCE 1 hour ago",
                   col=5, row=23, width=4, height=4),
            widget("Disk % by Device", "viz.bar",
                   "FROM ChromeDeviceHealthSample SELECT latest(diskUsedPct) FACET serialNumber SINCE 1 hour ago",
                   col=9, row=23, width=4, height=4),

            # Row 27-30 — Offline detection table
            widget("Device Last Seen — Offline Detection", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(timestamp) AS 'Last Seen (ms)', "
                    "       latest(heartbeatState) AS 'Heartbeat', "
                    "       latest(kioskAppState) AS 'Kiosk', "
                    "       latest(cpuUtilizationPct) AS 'CPU %', "
                    "       latest(networkType) AS 'Network', "
                    "       latest(netDownloadSpeedKbps) AS 'DL (Kbps)' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=1, row=27, width=12, height=4),
        ]
    }


def page_device_health():
    header = """## 📊 Device Health — Metric Trends

Per-device CPU, memory, disk, battery, temperature, network, bandwidth, and boot performance over time.
Data collected every **5 minutes** via the Chrome Management Telemetry API."""

    thresholds = """**Alert Thresholds**

| Metric | 🟡 Warn | 🔴 Crit |
|---|---|---|
| CPU | 70% | 90% |
| CPU Temp | 75°C | 85°C |
| Memory | 75% | 85% |
| Disk | 80% | 90% |
| Battery | 20%↓ | 10%↓ |
| Boot Time | 60s | — |"""

    return {
        "name": "Device Health",
        "description": "CPU, memory, disk, battery, temperature, bandwidth, boot, and hardware detail",
        "widgets": [
            markdown_widget(header,     col=1, row=1, width=8, height=3),
            markdown_widget(thresholds, col=9, row=1, width=4, height=3),

            # Row 4-6 — Billboards
            widget("Avg CPU %", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(cpuUtilizationPct) AS 'CPU %' SINCE 1 hour ago",
                   col=1, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING",  "value": 70},
                                                {"alertSeverity": "CRITICAL", "value": 90}]}),
            widget("Avg CPU Temp", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(cpuTemperatureCelsius) AS '°C' SINCE 1 hour ago",
                   col=3, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING",  "value": 75},
                                                {"alertSeverity": "CRITICAL", "value": 85}]}),
            widget("Avg Memory %", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(memoryUsedPct) AS 'Memory %' SINCE 1 hour ago",
                   col=5, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING",  "value": 75},
                                                {"alertSeverity": "CRITICAL", "value": 85}]}),
            widget("Avg Disk %", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(diskUsedPct) AS 'Disk %' SINCE 1 hour ago",
                   col=7, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING",  "value": 80},
                                                {"alertSeverity": "CRITICAL", "value": 90}]}),
            widget("Avg Battery %", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(batteryChargePct) AS 'Battery %' SINCE 1 hour ago",
                   col=9, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING",  "value": 20},
                                                {"alertSeverity": "CRITICAL", "value": 10}]}),
            widget("Avg Boot Time", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(bootDurationSeconds) AS 'Seconds' SINCE 1 day ago",
                   col=11, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 60}]}),

            # Row 7-10 — CPU
            widget("CPU Utilization % by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(cpuUtilizationPct) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=7, width=6, height=4),
            widget("CPU Temperature (°C) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(cpuTemperatureCelsius) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=7, width=6, height=4),

            # Row 11-14 — Memory + Disk
            widget("Memory Used % by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(memoryUsedPct) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=11, width=6, height=4),
            widget("Disk Used % by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(diskUsedPct) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=11, width=6, height=4),

            # Row 15-18 — Battery + Gateway ping
            widget("Battery Charge % by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(batteryChargePct) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=15, width=6, height=4),
            widget("Gateway Ping Latency (ms) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(networkGatewayPing) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=15, width=6, height=4),

            # Row 19-22 — Bandwidth
            widget("Download Speed (Kbps) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(netDownloadSpeedKbps) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=19, width=6, height=4),
            widget("Upload Speed (Kbps) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(netUploadSpeedKbps) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=19, width=6, height=4),

            # Row 23-26 — Boot + page faults
            widget("Boot Duration (seconds) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(bootDurationSeconds) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=23, width=6, height=4),
            widget("Memory Page Faults by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(memoryPageFaults) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=23, width=6, height=4),

            # Row 27-30 — Disk I/O
            widget("Disk Bytes Read / Session", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(diskBytesReadThisSession) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=27, width=6, height=4),
            widget("Disk Bytes Written / Session", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(diskBytesWrittenThisSession) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=27, width=6, height=4),

            # Row 31-33 — OS update + Network tables
            widget("OS Update Status", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(osLastRebootTime) AS 'Last Reboot', "
                    "       latest(osLastCheckTime) AS 'Last Check', "
                    "       latest(osLastUpdateTime) AS 'Last Update', "
                    "       latest(osUpdateState) AS 'State' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=1, row=31, width=6, height=3),
            widget("Network Info", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(networkType) AS 'Type', "
                    "       latest(networkMacAddress) AS 'MAC', "
                    "       latest(networkSignalStrength) AS 'Signal (dBm)', "
                    "       latest(networkTransmitRate) AS 'TX Rate (Mbps)', "
                    "       latest(networkGatewayPing) AS 'Ping (ms)', "
                    "       latest(networkDnsLatency) AS 'DNS (ms)' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=7, row=31, width=6, height=3),

            # Row 34-36 — Battery detail + Hardware tables
            widget("Battery Detail", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(batteryChargePct) AS 'Charge %', "
                    "       latest(batteryHealth) AS 'Health', "
                    "       latest(batteryVoltage) AS 'Voltage (mV)', "
                    "       latest(batteryDesignCapacity) AS 'Design Cap (mAh)', "
                    "       latest(batteryFullCapacity) AS 'Full Cap (mAh)', "
                    "       latest(batteryManufacturer) AS 'Manufacturer' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=1, row=34, width=6, height=3),
            widget("CPU & Storage Hardware", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(cpuModel) AS 'CPU Model', "
                    "       latest(cpuArchitecture) AS 'Architecture', "
                    "       latest(diskModel) AS 'Disk Model', "
                    "       latest(diskType) AS 'Disk Type', "
                    "       latest(memoryTotalBytes) AS 'RAM (bytes)', "
                    "       latest(diskTotalBytes) AS 'Disk Total (bytes)' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=7, row=34, width=6, height=3),
        ]
    }


def page_kiosk_app():
    header = """## 🖥️ Kiosk & App Status

Kiosk application health, device connectivity heartbeat, audio/display hardware status, boot performance, and app events.
This page is particularly important for devices running in **unattended kiosk mode**.

**Kiosk App States:** `APP_STATUS_STARTED` ✅ | `APP_STATUS_NOT_STARTED` 🔴 | `APP_STATUS_UNKNOWN` ⚠️"""

    boot_ref = """**Boot Performance Reference**

| Metric | Good | Investigate |
|---|---|---|
| Boot Duration | < 30s | > 60s |
| Shutdown Duration | < 15s | > 30s |
| Wake Up Duration | < 5s | > 15s |

Slow boot times may indicate storage degradation, excessive startup apps, or hardware issues."""

    return {
        "name": "Kiosk & App Status",
        "description": "Kiosk app health, heartbeat, audio, display, boot performance, and app events",
        "widgets": [
            markdown_widget(header,   col=1, row=1, width=8, height=3),
            markdown_widget(boot_ref, col=9, row=1, width=4, height=3),

            # Row 4-6 — Billboards
            widget("Kiosk App Running", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT uniqueCount(serialNumber) AS 'Running' WHERE kioskAppState = 'APP_STATUS_STARTED' SINCE 30 minutes ago",
                   col=1, row=4, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 0}]}),
            widget("Kiosk App NOT Running", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT uniqueCount(serialNumber) AS 'Not Running' WHERE kioskAppState != 'APP_STATUS_STARTED' AND kioskAppState IS NOT NULL SINCE 30 minutes ago",
                   col=4, row=4, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "CRITICAL", "value": 1}]}),
            widget("Devices Online (Heartbeat)", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT uniqueCount(serialNumber) AS 'Online' WHERE heartbeatState = 'ONLINE' SINCE 30 minutes ago",
                   col=7, row=4, width=3, height=3),
            widget("Avg Boot Time (sec)", "viz.billboard",
                   "FROM ChromeDeviceHealthSample SELECT average(bootDurationSeconds) AS 'Seconds' SINCE 1 day ago",
                   col=10, row=4, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 60}]}),

            # Row 7-11 — Kiosk status table
            widget("Kiosk App Status per Device", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(kioskAppState) AS 'App State', "
                    "       latest(kioskAppId) AS 'App ID', "
                    "       latest(kioskAppVersion) AS 'Version', "
                    "       latest(heartbeatState) AS 'Heartbeat', "
                    "       latest(kioskReportTime) AS 'Report Time', "
                    "       latest(heartbeatReportTime) AS 'Heartbeat Time' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=1, row=7, width=12, height=5),

            # Row 12-15 — Boot + Shutdown trends
            widget("Boot Duration (sec) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(bootDurationSeconds) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=12, width=6, height=4),
            widget("Shutdown Duration (sec) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(shutdownDurationSeconds) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=12, width=6, height=4),

            # Row 16-19 — Wake + Sleep trends
            widget("Wake Up Duration (sec) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(wakeUpDurationSeconds) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=16, width=6, height=4),
            widget("Sleep Duration (sec) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(sleepDurationSeconds) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=16, width=6, height=4),

            # Row 20-23 — Audio + Display tables
            widget("Audio Device Status", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(audioOutputCount) AS 'Outputs', "
                    "       latest(audioOutput0Name) AS 'Output 0', "
                    "       latest(audioOutput0Active) AS 'Active', "
                    "       latest(audioOutput0Muted) AS 'Muted', "
                    "       latest(audioOutput0Volume) AS 'Volume', "
                    "       latest(audioInputCount) AS 'Inputs', "
                    "       latest(audioInput0Name) AS 'Input 0', "
                    "       latest(audioInput0Active) AS 'Input Active' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=1, row=20, width=6, height=4),
            widget("Display / Graphics Status", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(displayCount) AS 'Displays', "
                    "       latest(display0Name) AS 'Display 0', "
                    "       latest(display0ResolutionWidth) AS 'Width', "
                    "       latest(display0ResolutionHeight) AS 'Height', "
                    "       latest(display0RefreshRate) AS 'Hz', "
                    "       latest(display0IsInternal) AS 'Internal', "
                    "       latest(display1Name) AS 'Display 1' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=7, row=20, width=6, height=4),

            # Row 24-26 — Boot performance detail table
            widget("Boot Performance Detail", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(bootDurationSeconds) AS 'Boot (s)', "
                    "       latest(shutdownDurationSeconds) AS 'Shutdown (s)', "
                    "       latest(wakeUpDurationSeconds) AS 'Wake (s)', "
                    "       latest(sleepDurationSeconds) AS 'Sleep (s)', "
                    "       latest(bootReportTime) AS 'Report Time' "
                    "FACET serialNumber SINCE 24 hours ago LIMIT 100"),
                   col=1, row=24, width=12, height=3),

            # Row 27-30 — App launches + installs/uninstalls
            widget("Recent App Launches (24h)", "viz.table",
                   ("FROM ChromeTelemetryEventSample "
                    "SELECT serialNumber, appId, reportTime "
                    "WHERE telemetryCategory = 'APP_LAUNCHED' "
                    "SINCE 1 day ago LIMIT 50"),
                   col=1, row=27, width=6, height=4),
            widget("App Install / Uninstall Events (7d)", "viz.table",
                   ("FROM ChromeTelemetryEventSample "
                    "SELECT serialNumber, telemetryCategory, appId, reportTime "
                    "WHERE telemetryCategory IN ('APP_INSTALLED', 'APP_UNINSTALLED') "
                    "SINCE 7 days ago LIMIT 50"),
                   col=7, row=27, width=6, height=4),
        ]
    }


def page_network():
    header = """## 🌐 Network & Connectivity

Network performance metrics, connectivity state changes, WiFi signal health, HTTPS latency diagnostics, and VPN activity.
Use this page to investigate connectivity issues at specific devices or locations."""

    reference = """**Diagnostic Reference**

| `deviceConnectionState` | `httpStatusCode` | Meaning |
|---|---|---|
| `PORTAL` | 403 | Captive portal — device blocked |
| `PORTAL` | 503 | Upstream service unavailable |
| `OFFLINE` | — | Device lost network |
| `ONLINE` | — | Connectivity restored |

**HTTPS `problem` values:**
`FAILED_HTTP_REQUESTS` · `FAILED_DNS_RESOLUTIONS`
`HIGH_LATENCY` · `VERY_HIGH_LATENCY`"""

    return {
        "name": "Network & Connectivity",
        "description": "Gateway ping, DNS, WiFi signal, bandwidth, and all network/VPN events",
        "widgets": [
            markdown_widget(header,    col=1, row=1, width=7, height=3),
            markdown_widget(reference, col=8, row=1, width=5, height=3),

            # Row 4-6 — Billboards
            widget("Network State Changes (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'NETWORK_STATE_CHANGE' SINCE 1 day ago",
                   col=1, row=4, width=3, height=3),
            widget("HTTPS Latency Issues (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'NETWORK_HTTPS_LATENCY_CHANGE' SINCE 1 day ago",
                   col=4, row=4, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 1}]}),
            widget("VPN State Changes (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'VPN_CONNECTION_STATE_CHANGE' SINCE 1 day ago",
                   col=7, row=4, width=3, height=3),
            widget("WiFi Signal Low (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'WIFI_SIGNAL_STRENGTH_LOW' SINCE 1 day ago",
                   col=10, row=4, width=3, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 1}]}),

            # Row 7-10 — Gateway ping + DNS latency
            widget("Gateway Ping Latency (ms) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(networkGatewayPing) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=7, width=6, height=4),
            widget("DNS Latency (ms) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(networkDnsLatency) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=7, width=6, height=4),

            # Row 11-14 — WiFi signal + TX rate
            widget("WiFi Signal Strength (dBm) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(networkSignalStrength) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=11, width=6, height=4),
            widget("Network TX Rate (Mbps) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(networkTransmitRate) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=11, width=6, height=4),

            # Row 15-18 — Download / upload
            widget("Download Speed (Kbps) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(netDownloadSpeedKbps) FACET serialNumber TIMESERIES AUTO",
                   col=1, row=15, width=6, height=4),
            widget("Upload Speed (Kbps) by Device", "viz.line",
                   "FROM ChromeDeviceHealthSample SELECT average(netUploadSpeedKbps) FACET serialNumber TIMESERIES AUTO",
                   col=7, row=15, width=6, height=4),

            # Row 19-22 — Network state changes table
            widget("Network State Changes", "viz.table",
                   ("FROM ChromeTelemetryEventSample "
                    "SELECT serialNumber, deviceConnectionState, httpStatusCode, reportTime "
                    "WHERE telemetryCategory = 'NETWORK_STATE_CHANGE' "
                    "SINCE 1 day ago LIMIT 100"),
                   col=1, row=19, width=12, height=4),

            # Row 23-26 — HTTPS latency table
            widget("HTTPS Latency Change Events", "viz.table",
                   ("FROM ChromeTelemetryEventSample "
                    "SELECT serialNumber, problem, httpStatusCode, "
                    "       routineData_verdict, routineData_latency, reportTime "
                    "WHERE telemetryCategory = 'NETWORK_HTTPS_LATENCY_CHANGE' "
                    "SINCE 7 days ago LIMIT 100"),
                   col=1, row=23, width=12, height=4),

            # Row 27-30 — WiFi + VPN tables
            widget("WiFi Signal Low / Recovered Events", "viz.table",
                   ("FROM ChromeTelemetryEventSample "
                    "SELECT serialNumber, telemetryCategory, signalStrengthDbm, reportTime "
                    "WHERE telemetryCategory IN ('WIFI_SIGNAL_STRENGTH_LOW', 'WIFI_SIGNAL_STRENGTH_RECOVERED') "
                    "SINCE 7 days ago LIMIT 50"),
                   col=1, row=27, width=6, height=4),
            widget("VPN Connection State Changes", "viz.table",
                   ("FROM ChromeTelemetryEventSample "
                    "SELECT serialNumber, reportTime "
                    "WHERE telemetryCategory = 'VPN_CONNECTION_STATE_CHANGE' "
                    "SINCE 7 days ago LIMIT 50"),
                   col=7, row=27, width=6, height=4),

            # Row 31-33 — Network device snapshot
            widget("Network Device Snapshot", "viz.table",
                   ("FROM ChromeDeviceHealthSample "
                    "SELECT latest(networkType) AS 'Type', "
                    "       latest(networkMacAddress) AS 'MAC', "
                    "       latest(networkSignalStrength) AS 'Signal (dBm)', "
                    "       latest(networkTransmitRate) AS 'TX (Mbps)', "
                    "       latest(networkGatewayPing) AS 'GW Ping (ms)', "
                    "       latest(networkDnsLatency) AS 'DNS (ms)', "
                    "       latest(netDownloadSpeedKbps) AS 'DL (Kbps)', "
                    "       latest(netUploadSpeedKbps) AS 'UL (Kbps)' "
                    "FACET serialNumber SINCE 1 hour ago LIMIT 100"),
                   col=1, row=31, width=12, height=3),
        ]
    }


def page_events():
    header = """## 📡 Telemetry Events

All 14 Chrome Management telemetry event types. Use the time picker to zoom into specific incident windows.
Each event type has its own detail table below for investigation."""

    event_reference = """## 📋 Event Type Reference

| Event Type | Trigger | Severity |
|---|---|---|
| `OS_CRASH` | Kernel or browser crash | 🔴 Critical |
| `WIFI_SIGNAL_STRENGTH_LOW` | WiFi dropped below –70 dBm | 🟡 Warning |
| `WIFI_SIGNAL_STRENGTH_RECOVERED` | WiFi signal restored above threshold | ✅ Recovery |
| `NETWORK_STATE_CHANGE` | Network connection state changed | ℹ️ Info |
| `NETWORK_HTTPS_LATENCY_CHANGE` | HTTPS latency problem detected or resolved | 🟡 Warning |
| `USB_ADDED` | USB peripheral connected | ℹ️ Info |
| `USB_REMOVED` | USB peripheral disconnected | ℹ️ Info |
| `APP_INSTALLED` | Application installed | ℹ️ Info |
| `APP_UNINSTALLED` | Application removed | ℹ️ Info |
| `APP_LAUNCHED` | Application launched | ℹ️ Info |
| `VPN_CONNECTION_STATE_CHANGE` | VPN tunnel state changed | ℹ️ Info |
| `AUDIO_SEVERE_UNDERRUN` | Audio buffer ran dry for > 5 seconds | 🟡 Warning |
| `EXTERNAL_DISPLAY_CONNECTED` | External display connected | ℹ️ Info |
| `EXTERNAL_DISPLAY_DISCONNECTED` | External display disconnected | ℹ️ Info |

---

### 🔍 Diagnostic Fields

**`NETWORK_STATE_CHANGE` — `httpStatusCode`**
When `deviceConnectionState = PORTAL`, the device detected a captive portal:
- **403** — Portal intercepting traffic, blocking unauthenticated devices (common in retail/guest networks)
- **503** — Network or upstream service temporarily unavailable

**`NETWORK_HTTPS_LATENCY_CHANGE` — `problem`**

| `problem` value | Meaning |
|---|---|
| `FAILED_HTTP_REQUESTS` | HTTP requests to Google's connectivity endpoint failed (firewall, 403, 503) |
| `FAILED_DNS_RESOLUTIONS` | DNS lookups failing — device cannot resolve hostnames |
| `HIGH_LATENCY` | HTTPS responses slower than threshold but requests succeeding |
| `VERY_HIGH_LATENCY` | Severe latency — device connectivity severely degraded |

The `routineData_verdict` (`PASS` / `FAIL`) and `routineData_latency` (measured ms) provide additional context.

---

Data source: [Chrome Management Telemetry API](https://developers.google.com/chrome/management/guides/telemetry_api)"""

    return {
        "name": "Events",
        "description": "All 14 Chrome telemetry event types — crashes, network, WiFi, USB, apps, audio, display",
        "widgets": [
            markdown_widget(header, col=1, row=1, width=12, height=3),

            # Row 4-6 — Critical / warning event billboards
            widget("OS Crashes (7d)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Crashes' WHERE telemetryCategory = 'OS_CRASH' SINCE 7 days ago",
                   col=1, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "CRITICAL", "value": 1}]}),
            widget("WiFi Signal Low (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'WIFI_SIGNAL_STRENGTH_LOW' SINCE 1 day ago",
                   col=3, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 1}]}),
            widget("HTTPS Latency Issues (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'NETWORK_HTTPS_LATENCY_CHANGE' SINCE 1 day ago",
                   col=5, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 1}]}),
            widget("Audio Underruns (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'AUDIO_SEVERE_UNDERRUN' SINCE 1 day ago",
                   col=7, row=4, width=2, height=3,
                   extra_config={"thresholds": [{"alertSeverity": "WARNING", "value": 1}]}),
            widget("VPN Changes (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'VPN_CONNECTION_STATE_CHANGE' SINCE 1 day ago",
                   col=9, row=4, width=2, height=3),
            widget("Network Changes (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'NETWORK_STATE_CHANGE' SINCE 1 day ago",
                   col=11, row=4, width=2, height=3),

            # Row 7-9 — Informational event billboards
            widget("USB Events (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory IN ('USB_ADDED', 'USB_REMOVED') SINCE 1 day ago",
                   col=1, row=7, width=2, height=3),
            widget("App Installs (7d)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory IN ('APP_INSTALLED', 'APP_UNINSTALLED') SINCE 7 days ago",
                   col=3, row=7, width=2, height=3),
            widget("App Launches (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'APP_LAUNCHED' SINCE 1 day ago",
                   col=5, row=7, width=2, height=3),
            widget("Display Connected (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'EXTERNAL_DISPLAY_CONNECTED' SINCE 1 day ago",
                   col=7, row=7, width=2, height=3),
            widget("Display Disconnected (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'EXTERNAL_DISPLAY_DISCONNECTED' SINCE 1 day ago",
                   col=9, row=7, width=2, height=3),
            widget("WiFi Recovered (24h)", "viz.billboard",
                   "FROM ChromeTelemetryEventSample SELECT count(*) AS 'Events' WHERE telemetryCategory = 'WIFI_SIGNAL_STRENGTH_RECOVERED' SINCE 1 day ago",
                   col=11, row=7, width=2, height=3),

            # Row 10-14 — Event activity timeline
            widget("Event Activity Over Time", "viz.area",
                   "FROM ChromeTelemetryEventSample SELECT count(*) FACET telemetryCategory TIMESERIES AUTO SINCE 7 days ago",
                   col=1, row=10, width=12, height=5),

            # Row 15-18 — OS crash / WiFi / USB tables
            widget("OS Crash Events", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, reportTime WHERE telemetryCategory = 'OS_CRASH' SINCE 7 days ago LIMIT 50",
                   col=1, row=15, width=4, height=4),
            widget("WiFi Signal Low / Recovered", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, telemetryCategory, signalStrengthDbm, reportTime WHERE telemetryCategory IN ('WIFI_SIGNAL_STRENGTH_LOW', 'WIFI_SIGNAL_STRENGTH_RECOVERED') SINCE 7 days ago LIMIT 50",
                   col=5, row=15, width=4, height=4),
            widget("USB Events", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, telemetryCategory, reportTime WHERE telemetryCategory IN ('USB_ADDED', 'USB_REMOVED') SINCE 1 day ago LIMIT 50",
                   col=9, row=15, width=4, height=4),

            # Row 19-22 — Network / App tables
            widget("Network State Changes", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, deviceConnectionState, httpStatusCode, reportTime WHERE telemetryCategory = 'NETWORK_STATE_CHANGE' SINCE 1 day ago LIMIT 50",
                   col=1, row=19, width=6, height=4),
            widget("App Install / Uninstall Events", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, telemetryCategory, appId, reportTime WHERE telemetryCategory IN ('APP_INSTALLED', 'APP_UNINSTALLED') SINCE 7 days ago LIMIT 50",
                   col=7, row=19, width=6, height=4),

            # Row 23-26 — HTTPS latency / VPN tables
            widget("HTTPS Latency Change Events", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, problem, httpStatusCode, routineData_verdict, routineData_latency, reportTime WHERE telemetryCategory = 'NETWORK_HTTPS_LATENCY_CHANGE' SINCE 7 days ago LIMIT 50",
                   col=1, row=23, width=6, height=4),
            widget("VPN Connection State Changes", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, reportTime WHERE telemetryCategory = 'VPN_CONNECTION_STATE_CHANGE' SINCE 7 days ago LIMIT 50",
                   col=7, row=23, width=6, height=4),

            # Row 27-30 — App launches / audio underruns / display events tables
            widget("App Launch Events", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, appId, reportTime WHERE telemetryCategory = 'APP_LAUNCHED' SINCE 1 day ago LIMIT 50",
                   col=1, row=27, width=4, height=4),
            widget("Audio Severe Underrun Events", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, reportTime WHERE telemetryCategory = 'AUDIO_SEVERE_UNDERRUN' SINCE 7 days ago LIMIT 50",
                   col=5, row=27, width=4, height=4),
            widget("External Display Events", "viz.table",
                   "FROM ChromeTelemetryEventSample SELECT serialNumber, telemetryCategory, reportTime WHERE telemetryCategory IN ('EXTERNAL_DISPLAY_CONNECTED', 'EXTERNAL_DISPLAY_DISCONNECTED') SINCE 7 days ago LIMIT 50",
                   col=9, row=27, width=4, height=4),

            # Row 31-34 — All events table
            widget("All Telemetry Events", "viz.table",
                   ("FROM ChromeTelemetryEventSample "
                    "SELECT serialNumber, telemetryCategory, reportTime "
                    "SINCE 1 day ago LIMIT 100"),
                   col=1, row=31, width=12, height=4),

            # Row 35-39 — Event reference panel
            markdown_widget(event_reference, col=1, row=35, width=12, height=5),
        ]
    }


def _build_dashboard_definition():
    return {
        "name": "Chrome Device Telemetry",
        "description": "ChromeOS device health, kiosk status, network, and telemetry event tracking",
        "permissions": "PUBLIC_READ_WRITE",
        "pages": [
            page_fleet_summary(),
            page_device_health(),
            page_kiosk_app(),
            page_network(),
            page_events(),
        ],
    }

# ===========================================================================
# Dashboard deploy (create or update)
# ===========================================================================

def _find_dashboard_guid():
    """Return (guid, name) of the most recently created Chrome Device Telemetry dashboard,
    or (None, None) if none exists."""
    query = """
    {
      actor {
        entitySearch(query: "name = 'Chrome Device Telemetry' AND type = 'DASHBOARD'") {
          results {
            entities {
              guid
              name
              ... on DashboardEntityOutline { createdAt }
            }
          }
        }
      }
    }
    """
    resp = graphql(query)
    entities = resp["data"]["actor"]["entitySearch"]["results"]["entities"]
    if not entities:
        return None, None
    entities.sort(key=lambda e: e.get("createdAt", ""), reverse=True)
    if len(entities) > 1:
        print(f"  Note: {len(entities)} existing dashboards found — targeting the most recent.")
    return entities[0]["guid"], entities[0]["name"]


def deploy_dashboard():
    """Create the dashboard if it doesn't exist; update it if it does."""
    definition = _build_dashboard_definition()

    if DASHBOARD_GUID:
        guid = DASHBOARD_GUID
        print(f"  Using DASHBOARD_GUID from env: {guid}")
        action = "update"
    else:
        guid, existing_name = _find_dashboard_guid()
        action = "update" if guid else "create"

    if action == "create":
        print("  Dashboard not found — creating...")
        mutation = """
        mutation CreateDashboard($accountId: Int!, $dashboard: DashboardInput!) {
          dashboardCreate(accountId: $accountId, dashboard: $dashboard) {
            entityResult { guid name }
            errors { description type }
          }
        }
        """
        resp = graphql(mutation, {"accountId": ACCOUNT_ID, "dashboard": definition})
        result = resp["data"]["dashboardCreate"]
        if result.get("errors"):
            print(f"  Dashboard errors: {result['errors']}", file=sys.stderr); sys.exit(1)
        entity = result["entityResult"]
        guid = entity["guid"]
        print(f"  Created: {entity['name']}")
    else:
        print(f"  Dashboard found — updating...")
        mutation = """
        mutation UpdateDashboard($guid: EntityGuid!, $dashboard: DashboardInput!) {
          dashboardUpdate(guid: $guid, dashboard: $dashboard) {
            entityResult { guid name }
            errors { description type }
          }
        }
        """
        resp = graphql(mutation, {"guid": guid, "dashboard": definition})
        result = resp["data"]["dashboardUpdate"]
        if result.get("errors"):
            print(f"  Dashboard errors: {result['errors']}", file=sys.stderr); sys.exit(1)
        entity = result["entityResult"]
        print(f"  Updated: {entity['name']}")

    print(f"  URL: https://one.newrelic.com/dashboards/{guid}")
    return guid

# ===========================================================================
# Alert conditions — full set (13 total)
# ===========================================================================

STATIC_CONDITIONS = [
    # --- Base conditions ---
    dict(name="Chrome Device - High CPU Utilization",
         description="CPU utilization exceeds 90% on a Chrome device",
         query="FROM ChromeDeviceHealthSample SELECT average(cpuUtilizationPct) FACET serialNumber",
         threshold=90, duration=300, operator="ABOVE", priority="CRITICAL"),
    dict(name="Chrome Device - High Memory Usage",
         description="Memory usage exceeds 85% on a Chrome device",
         query="FROM ChromeDeviceHealthSample SELECT average(memoryUsedPct) FACET serialNumber",
         threshold=85, duration=300, operator="ABOVE", priority="CRITICAL"),
    dict(name="Chrome Device - Low Disk Space",
         description="Disk usage exceeds 90% on a Chrome device",
         query="FROM ChromeDeviceHealthSample SELECT average(diskUsedPct) FACET serialNumber",
         threshold=90, duration=60, operator="ABOVE", priority="CRITICAL"),
    dict(name="Chrome Device - OS Crash Detected",
         description="An OS crash was detected on a Chrome device",
         query="FROM ChromeTelemetryEventSample SELECT count(*) WHERE telemetryCategory = 'OS_CRASH' FACET serialNumber",
         threshold=0, duration=60, operator="ABOVE", priority="CRITICAL"),
    dict(name="Chrome Device - WiFi Signal Degraded",
         description="WiFi signal dropped below -70dBm on a Chrome device",
         query="FROM ChromeTelemetryEventSample SELECT count(*) WHERE telemetryCategory = 'WIFI_SIGNAL_STRENGTH_LOW' FACET serialNumber",
         threshold=0, duration=60, operator="ABOVE", priority="WARNING"),
    # --- Extended conditions ---
    dict(name="Chrome Device - Kiosk App Not Running",
         description="The kiosk application is not in APP_STATUS_STARTED state on a Chrome device",
         query="FROM ChromeDeviceHealthSample SELECT uniqueCount(serialNumber) WHERE kioskAppState != 'APP_STATUS_STARTED' AND kioskAppState IS NOT NULL FACET serialNumber",
         threshold=0, duration=60, operator="ABOVE", priority="CRITICAL"),
    dict(name="Chrome Device - High CPU Temperature",
         description="CPU temperature exceeded 85°C on a Chrome device, indicating thermal throttling risk",
         query="FROM ChromeDeviceHealthSample SELECT average(cpuTemperatureCelsius) FACET serialNumber",
         threshold=85, duration=300, operator="ABOVE", priority="CRITICAL"),
    dict(name="Chrome Device - Elevated CPU Temperature",
         description="CPU temperature exceeded 75°C on a Chrome device",
         query="FROM ChromeDeviceHealthSample SELECT average(cpuTemperatureCelsius) FACET serialNumber",
         threshold=75, duration=300, operator="ABOVE", priority="WARNING"),
    dict(name="Chrome Device - Slow Boot Time",
         description="Device boot time exceeded 60 seconds, indicating possible storage or software issues",
         query="FROM ChromeDeviceHealthSample SELECT average(bootDurationSeconds) FACET serialNumber",
         threshold=60, duration=60, operator="ABOVE", priority="WARNING"),
    dict(name="Chrome Device - Low Download Bandwidth",
         description="Network download speed fell below 1 Mbps on a Chrome device",
         query="FROM ChromeDeviceHealthSample SELECT average(netDownloadSpeedKbps) FACET serialNumber",
         threshold=1000, duration=300, operator="BELOW", priority="WARNING"),
    dict(name="Chrome Device - Heartbeat Offline",
         description="Device heartbeat status reported OFFLINE",
         query="FROM ChromeDeviceHealthSample SELECT uniqueCount(serialNumber) WHERE heartbeatState = 'OFFLINE' FACET serialNumber",
         threshold=0, duration=60, operator="ABOVE", priority="CRITICAL"),
]

EXPIRATION_CONDITIONS = [
    dict(name="Chrome Device - Not Reporting",
         description="A Chrome device has stopped sending telemetry for more than 2 hours",
         query="FROM ChromeDeviceHealthSample SELECT latest(cpuUtilizationPct) FACET serialNumber",
         expiration_seconds=7200, priority="WARNING"),
    dict(name="Chrome Device - Kiosk App Stopped Reporting",
         description="No kiosk app status received from a Chrome device for more than 1 hour",
         query="FROM ChromeDeviceHealthSample SELECT latest(kioskAppState) FACET serialNumber",
         expiration_seconds=3600, priority="CRITICAL"),
]

# ===========================================================================
# Alert deploy (create policy if needed, add only missing conditions)
# ===========================================================================

def _find_policy_id():
    """Return the ID of the Chrome Device Telemetry Alerts policy, or None."""
    query = """
    {
      actor {
        account(id: %d) {
          alerts {
            policiesSearch(searchCriteria: { name: "Chrome Device Telemetry Alerts" }) {
              policies { id name }
            }
          }
        }
      }
    }
    """ % ACCOUNT_ID
    resp = graphql(query)
    policies = resp["data"]["actor"]["account"]["alerts"]["policiesSearch"]["policies"]
    if not policies:
        return None
    return policies[0]["id"]


def _create_policy():
    mutation = """
    mutation CreatePolicy($accountId: Int!, $policy: AlertsPolicyInput!) {
      alertsPolicyCreate(accountId: $accountId, policy: $policy) {
        id name
      }
    }
    """
    resp = graphql(mutation, {
        "accountId": ACCOUNT_ID,
        "policy": {
            "name": "Chrome Device Telemetry Alerts",
            "incidentPreference": "PER_CONDITION_AND_TARGET",
        }
    })
    policy = resp["data"]["alertsPolicyCreate"]
    print(f"  Created policy: {policy['name']} (ID: {policy['id']})")
    return policy["id"]


def _existing_condition_names(policy_id):
    """Return a set of condition names already in the policy."""
    query = """
    {
      actor {
        account(id: %d) {
          alerts {
            nrqlConditionsSearch(searchCriteria: { policyId: "%s" }) {
              nrqlConditions { name }
            }
          }
        }
      }
    }
    """ % (ACCOUNT_ID, policy_id)
    resp = graphql(query)
    conditions = resp["data"]["actor"]["account"]["alerts"]["nrqlConditionsSearch"]["nrqlConditions"]
    return {c["name"] for c in conditions}


def _create_static_condition(policy_id, cond):
    mutation = """
    mutation CreateCondition($accountId: Int!, $policyId: ID!, $condition: AlertsNrqlConditionStaticInput!) {
      alertsNrqlConditionStaticCreate(accountId: $accountId, policyId: $policyId, condition: $condition) {
        id name
      }
    }
    """
    resp = graphql(mutation, {
        "accountId": ACCOUNT_ID,
        "policyId":  policy_id,
        "condition": {
            "name":        cond["name"],
            "description": cond["description"],
            "enabled":     True,
            "nrql":        {"query": cond["query"]},
            "signal": {
                "aggregationWindow": 60,
                "evaluationOffset":  3,
                "fillOption":        "NONE",
            },
            "terms": [{
                "threshold":            cond["threshold"],
                "thresholdDuration":    cond["duration"],
                "thresholdOccurrences": "ALL",
                "operator":             cond["operator"],
                "priority":             cond["priority"],
            }],
            "violationTimeLimitSeconds": 86400,
        }
    })
    result = resp["data"]["alertsNrqlConditionStaticCreate"]
    print(f"  Created: {result['name']} (ID: {result['id']})")


def _create_expiration_condition(policy_id, cond):
    mutation = """
    mutation CreateCondition($accountId: Int!, $policyId: ID!, $condition: AlertsNrqlConditionStaticInput!) {
      alertsNrqlConditionStaticCreate(accountId: $accountId, policyId: $policyId, condition: $condition) {
        id name
      }
    }
    """
    resp = graphql(mutation, {
        "accountId": ACCOUNT_ID,
        "policyId":  policy_id,
        "condition": {
            "name":        cond["name"],
            "description": cond["description"],
            "enabled":     True,
            "nrql":        {"query": cond["query"]},
            "signal": {
                "aggregationWindow": 60,
                "evaluationOffset":  3,
                "fillOption":        "NONE",
            },
            "expiration": {
                "expirationDuration":         cond["expiration_seconds"],
                "openViolationOnExpiration":  True,
                "closeViolationsOnExpiration": True,
            },
            "terms": [{
                "threshold":            100000,
                "thresholdDuration":    60,
                "thresholdOccurrences": "ALL",
                "operator":             "ABOVE",
                "priority":             cond["priority"],
            }],
            "violationTimeLimitSeconds": 86400,
        }
    })
    result = resp["data"]["alertsNrqlConditionStaticCreate"]
    print(f"  Created: {result['name']} (ID: {result['id']})")


def deploy_alerts():
    """Create the alert policy if missing; add only conditions not already present."""
    if POLICY_ID:
        policy_id = POLICY_ID
        print(f"  Using POLICY_ID from env: {policy_id}")
    else:
        policy_id = _find_policy_id()
        if policy_id:
            print(f"  Policy found (ID: {policy_id}) — checking conditions...")
        else:
            print("  Policy not found — creating...")
            policy_id = _create_policy()

    existing = _existing_condition_names(policy_id)

    created = skipped = 0
    for cond in STATIC_CONDITIONS:
        if cond["name"] in existing:
            skipped += 1
        else:
            _create_static_condition(policy_id, cond)
            created += 1

    for cond in EXPIRATION_CONDITIONS:
        if cond["name"] in existing:
            skipped += 1
        else:
            _create_expiration_condition(policy_id, cond)
            created += 1

    if skipped:
        print(f"  Skipped {skipped} condition(s) already present.")
    print(f"  {created} new condition(s) created. {len(STATIC_CONDITIONS) + len(EXPIRATION_CONDITIONS)} total in full set.")
    return policy_id

# ===========================================================================
# Main
# ===========================================================================

def main():
    if not NR_API_KEY:
        print("ERROR: NR_API_KEY is not set.")
        print("       export NR_API_KEY=NRAK-...")
        sys.exit(1)

    print(f"\nChrome Device Telemetry — Deploy")
    print(f"Account ID: {ACCOUNT_ID}")
    print("─" * 50)

    print("\n[Dashboard]")
    deploy_dashboard()

    print("\n[Alerts]")
    deploy_alerts()

    print("\nDone.")
    print("\nDashboard pages:")
    print("  1. Fleet Summary          — KPI scorecard, kiosk status, all event counts, offline detection")
    print("  2. Device Health          — all metric trends, battery detail, hardware info")
    print("  3. Kiosk & App Status     — kiosk state, heartbeat, audio, display, boot perf, app events")
    print("  4. Network & Connectivity — gateway ping, DNS, WiFi signal, bandwidth, all network events")
    print("  5. Events                 — all 14 event types with detail tables + reference panel")
    print("\nAlert conditions (13 total):")
    print("  High CPU · High Memory · Low Disk · OS Crash · WiFi Degraded")
    print("  Kiosk Not Running · High CPU Temp · Elevated CPU Temp · Slow Boot")
    print("  Low Bandwidth · Heartbeat Offline · Not Reporting · Kiosk Stopped Reporting")

if __name__ == "__main__":
    main()
