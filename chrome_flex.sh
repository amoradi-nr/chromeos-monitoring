#!/usr/bin/env bash
# Flex-optimized output script for New Relic nri-flex integration.
# Outputs a flat JSON array — each element becomes one New Relic event.
#
# USAGE:
#   chrome_flex.sh health
#   chrome_flex.sh events [--since <duration>]
#
# MODES:
#   health   One event per ChromeOS device with flattened health metrics
#   events   One event per telemetry event across all monitored event types
#
# --since accepts shorthand (6m, 1h, 24h, 7d) or ISO 8601 timestamp.
# Defaults to 6m for events (designed to overlap slightly with a 5m poll interval).
#
# CONFIGURATION (env vars or edit defaults below):
#   CHROME_CUSTOMER_ID   Google Workspace customer ID
#   CHROME_KEY_FILE      Path to service account JSON key file
#   CHROME_ADMIN_EMAIL   Workspace super-admin email for Directory API (optional)
#                        Required for diskPartitionUsedPct — see IMPLEMENTATION_GUIDE.md §12

set -euo pipefail

CHROME_CUSTOMER_ID="${CHROME_CUSTOMER_ID:-C044xpmyc}"
CHROME_KEY_FILE="${CHROME_KEY_FILE:-$(dirname "$0")/chromedevicemanagement-491916-387cd746f5a6.json}"
CHROME_ADMIN_EMAIL="${CHROME_ADMIN_EMAIL:-}"

MODE=""
SINCE="6m"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
[[ $# -eq 0 ]] && { echo "Usage: $0 health|events [--since <duration>]" >&2; exit 1; }
MODE="$1"; shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# ---------------------------------------------------------------------------
# Resolve --since to ISO 8601 timestamp
# ---------------------------------------------------------------------------
resolve_since() {
  local val="$1"
  [[ "$val" == *"T"* ]] && { echo "$val"; return; }
  python3 - "$val" <<'PYEOF'
import sys, datetime, re
val = sys.argv[1]
m = re.fullmatch(r'(\d+)(m|h|d)', val)
if not m:
    print(val); sys.exit(0)
n, unit = int(m.group(1)), m.group(2)
if unit == 'm':
    delta = datetime.timedelta(minutes=n)
elif unit == 'h':
    delta = datetime.timedelta(hours=n)
else:
    delta = datetime.timedelta(days=n)
ts = datetime.datetime.now(datetime.timezone.utc) - delta
print(ts.strftime('%Y-%m-%dT%H:%M:%SZ'))
PYEOF
}

# ---------------------------------------------------------------------------
# Auth: scoped OAuth token via service account key
# ---------------------------------------------------------------------------
get_token() {
  python3 - "$CHROME_KEY_FILE" <<'PYEOF'
from google.oauth2 import service_account
import google.auth.transport.requests, requests as _requests, sys, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    creds = service_account.Credentials.from_service_account_file(
        sys.argv[1],
        scopes=['https://www.googleapis.com/auth/chrome.management.telemetry.readonly']
    )
    session = _requests.Session()
    session.verify = False
    creds.refresh(google.auth.transport.requests.Request(session=session))
    print(creds.token)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
}

# ---------------------------------------------------------------------------
# Auth: Directory API token — uses domain-wide delegation to impersonate admin
# Only called when CHROME_ADMIN_EMAIL is set.
# ---------------------------------------------------------------------------
get_directory_token() {
  python3 - "$CHROME_KEY_FILE" "$CHROME_ADMIN_EMAIL" <<'PYEOF'
from google.oauth2 import service_account
import google.auth.transport.requests, requests as _requests, sys, urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

key_file, subject = sys.argv[1], sys.argv[2]
try:
    creds = service_account.Credentials.from_service_account_file(
        key_file,
        scopes=['https://www.googleapis.com/auth/admin.directory.device.chromeos.readonly']
    ).with_subject(subject)
    session = _requests.Session()
    session.verify = False
    creds.refresh(google.auth.transport.requests.Request(session=session))
    print(creds.token)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
}

# ---------------------------------------------------------------------------
# Fetch per-volume disk data from Admin SDK Directory API.
# Returns path to a temp file containing a JSON array of
# { serialNumber, volumeInfo: [{storageTotal, storageFree}] } objects.
# ---------------------------------------------------------------------------
fetch_directory_disk() {
  local tok="$1"
  local base="https://admin.googleapis.com/admin/directory/v1/customer/${CHROME_CUSTOMER_ID}/devices/chromeos"
  # fields uses literal parentheses — urlencode would break the partial-response selector syntax
  local fields="?fields=nextPageToken,chromeosdevices(serialNumber,diskVolumeReports)&maxResults=500"

  local combined
  combined=$(mktemp)
  echo "[]" > "$combined"

  local page_token=""
  while true; do
    local url="${base}${fields}"
    [[ -n "$page_token" ]] && url+="&pageToken=${page_token}"

    local resp_file
    resp_file=$(api_get "$url" "$tok") || break

    page_token=$(python3 - "$resp_file" "$combined" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    resp = json.load(f)
with open(sys.argv[2]) as f:
    acc = json.load(f)
acc.extend(resp.get('chromeosdevices', []))
with open(sys.argv[2], 'w') as f:
    json.dump(acc, f)
print(resp.get('nextPageToken', ''))
PYEOF
)
    rm -f "$resp_file"
    [[ -z "$page_token" ]] && break
  done

  echo "$combined"
}

# ---------------------------------------------------------------------------
# Single API GET — writes response JSON to a temp file, returns its path
# ---------------------------------------------------------------------------
FLEX_LOG="${TMPDIR:-/tmp}/chrome_flex_errors_$(id -u).log"

api_get() {
  local url="$1" tok="$2"
  local tmp http_code
  tmp=$(mktemp)
  http_code=$(curl -sk -o "$tmp" -w "%{http_code}" \
    -H "Authorization: Bearer ${tok}" \
    -H "Accept: application/json" \
    "$url")
  if [[ "$http_code" -lt 200 || "$http_code" -ge 300 ]]; then
    echo "[$(date -u +%FT%TZ)] HTTP ${http_code} URL=${url}: $(cat "$tmp")" >> "$FLEX_LOG"
    rm -f "$tmp"; return 1
  fi
  echo "$tmp"
}

urlencode() {
  local s="$1" encoded="" i c hex
  for (( i=0; i<${#s}; i++ )); do
    c="${s:$i:1}"
    case "$c" in
      [a-zA-Z0-9.~_,-]) encoded+="$c" ;;
      *) printf -v hex '%02X' "'$c"; encoded+="%${hex}" ;;
    esac
  done
  echo "$encoded"
}

BASE="https://chromemanagement.googleapis.com/v1/customers/${CHROME_CUSTOMER_ID}"

# ---------------------------------------------------------------------------
# HEALTH mode — one flat event per device
# ---------------------------------------------------------------------------
run_health() {
  local tok="$1"
  local url="${BASE}/telemetry/devices?pageSize=1000"
  url+="&readMask=$(urlencode "name,serialNumber,cpuInfo,cpuStatusReport,memoryInfo,memoryStatusReport,storageInfo,storageStatusReport,batteryInfo,batteryStatusReport,networkInfo,networkStatusReport,osUpdateStatus,bootPerformanceReport,kioskAppStatusReport,heartbeatStatusReport")"

  local resp_file
  resp_file=$(api_get "$url" "$tok") || exit 1

  # Fetch Directory API per-partition disk data if admin email is configured
  local dir_file=""
  if [[ -n "$CHROME_ADMIN_EMAIL" ]]; then
    local dir_tok
    if dir_tok=$(get_directory_token 2>>"$FLEX_LOG"); then
      dir_file=$(fetch_directory_disk "$dir_tok") || dir_file=""
    fi
  fi

  python3 - "$resp_file" "${dir_file:-}" <<'PYEOF'
import json, sys

def parse_duration(d):
    if not d:
        return None
    try:
        return round(float(str(d).rstrip('s')), 2)
    except (ValueError, AttributeError):
        return None

# Build serial → largest-volume lookup from Directory API data (optional)
dir_lookup = {}
dir_file = sys.argv[2] if len(sys.argv) > 2 else ''
if dir_file:
    try:
        with open(dir_file) as f:
            for dd in json.load(f):
                sn = dd.get('serialNumber', '')
                volumes = [
                    {'total': int(vi.get('storageTotal', 0) or 0),
                     'free':  int(vi.get('storageFree',  0) or 0),
                     'path':  vi.get('volumeId', '')}
                    for report in dd.get('diskVolumeReports', [])
                    for vi in report.get('volumeInfo', [])
                    if int(vi.get('storageTotal', 0) or 0) > 0
                ]
                if sn and volumes:
                    # Target /home/chronos — the user data (stateful) partition
                    # This matches what Google Admin Console shows
                    # Exclude virtual filesystems (/media/fuse/) and USB mounts
                    user_vols = [v for v in volumes if v['path'].startswith('/home/chronos')]
                    if not user_vols:
                        user_vols = [v for v in volumes
                                     if '/fuse/' not in v['path']
                                     and '/media/removable/' not in v['path']]
                    if user_vols:
                        dir_lookup[sn] = max(user_vols, key=lambda v: v['total'])
    except Exception:
        pass

with open(sys.argv[1]) as f:
    data = json.load(f)

events = []
for dev in data.get('devices', []):
    name = dev.get('name', '')
    device_id = name.split('/')[-1] if name else ''
    ev = {
        'deviceId':     device_id,
        'serialNumber': dev.get('serialNumber', ''),
    }

    # CPU info
    for ci in dev.get('cpuInfo', [])[:1]:
        ev['cpuModel']        = ci.get('model', '')
        ev['cpuArchitecture'] = ci.get('architecture', '')

    # CPU status — utilization + temperature
    for cr in dev.get('cpuStatusReport', [])[:1]:
        ev['cpuUtilizationPct'] = cr.get('cpuUtilizationPct', '')
        ev['cpuReportTime']     = cr.get('reportTime', '')
        temps = [t.get('temperatureCelsius') for t in cr.get('cpuTemperatureInfo', [])
                 if isinstance(t, dict) and t.get('temperatureCelsius') is not None]
        if temps:
            ev['cpuTemperatureCelsius']    = round(sum(temps) / len(temps), 1)
            ev['cpuTemperatureMaxCelsius'] = max(temps)

    # Memory info
    mi = dev.get('memoryInfo', {})
    ev['memoryTotalBytes'] = int(mi.get('totalRamBytes', 0) or 0)

    # Memory status
    for mr in dev.get('memoryStatusReport', [])[:1]:
        ev['memoryFreeBytes']  = int(mr.get('systemRamFreeBytes', 0) or 0)
        ev['memoryPageFaults'] = mr.get('pageFaults', '')
        ev['memoryReportTime'] = mr.get('reportTime', '')

    if ev.get('memoryTotalBytes') and ev.get('memoryFreeBytes') is not None:
        used = ev['memoryTotalBytes'] - ev['memoryFreeBytes']
        ev['memoryUsedPct'] = round(used / ev['memoryTotalBytes'] * 100, 1)

    # Storage info
    si = dev.get('storageInfo', {})
    ev['diskTotalBytes']     = int(si.get('totalDiskBytes', 0) or 0)
    ev['diskAvailableBytes'] = int(si.get('availableDiskBytes', 0) or 0)
    if ev.get('diskTotalBytes'):
        used = ev['diskTotalBytes'] - ev['diskAvailableBytes']
        ev['diskUsedPct'] = round(used / ev['diskTotalBytes'] * 100, 1)

    # Storage status
    for sr in dev.get('storageStatusReport', [])[:1]:
        for disk in sr.get('disk', [])[:1]:
            ev['diskModel']                   = disk.get('model', '')
            ev['diskType']                    = disk.get('type', '')
            ev['diskBytesReadThisSession']    = int(disk.get('bytesReadThisSession', 0) or 0)
            ev['diskBytesWrittenThisSession'] = int(disk.get('bytesWrittenThisSession', 0) or 0)

    # Battery info
    for bi in dev.get('batteryInfo', [])[:1]:
        ev['batteryManufacturer']    = bi.get('manufacturer', '')
        ev['batteryDesignCapacity']  = bi.get('designCapacity', '')
        ev['batteryFullCapacity']    = bi.get('fullChargeCapacity', '')

    # Battery status
    for br in dev.get('batteryStatusReport', [])[:1]:
        ev['batteryChargePct']  = br.get('chargeRate', '')
        ev['batteryVoltage']    = br.get('voltage', '')
        ev['batteryHealth']     = br.get('batteryHealth', '')
        ev['batteryReportTime'] = br.get('reportTime', '')

    # Network info
    for nd in dev.get('networkInfo', {}).get('networkDevices', [])[:1]:
        ev['networkType']       = nd.get('type', '')
        ev['networkMacAddress'] = nd.get('macAddress', '')

    # Network status — latency + signal strength
    for nr in dev.get('networkStatusReport', [])[:1]:
        ev['networkGatewayPing']    = nr.get('gatewayPing', {}).get('averageLatencyMs', '')
        ev['networkDnsLatency']     = nr.get('dnsLatencyMs', '')
        ev['networkSignalStrength'] = nr.get('signalStrengthDbm', '')
        ev['networkTransmitRate']   = nr.get('transmissionBitRateMbps', '')
        ev['networkReportTime']     = nr.get('reportTime', '')

    # OS update status
    for os_s in dev.get('osUpdateStatus', [])[:1]:
        ev['osLastUpdateTime'] = os_s.get('lastUpdateTime', '')
        ev['osLastCheckTime']  = os_s.get('lastUpdateCheckTime', '')
        ev['osLastRebootTime'] = os_s.get('lastRebootTime', '')
        ev['osUpdateState']    = os_s.get('updateState', '')

    # Boot performance
    for bp in dev.get('bootPerformanceReport', [])[:1]:
        ev['bootDurationSeconds']     = parse_duration(bp.get('bootUpDuration'))
        ev['shutdownDurationSeconds'] = parse_duration(bp.get('shutDownDuration'))
        ev['sleepDurationSeconds']    = parse_duration(bp.get('sleepDuration'))
        ev['wakeUpDurationSeconds']   = parse_duration(bp.get('wakeUpDuration'))
        ev['bootReportTime']          = bp.get('reportTime', '')

    # Network bandwidth
    for nb in dev.get('networkBandwidthReport', [])[:1]:
        ev['netDownloadSpeedKbps'] = nb.get('downloadSpeedKbps', '')
        ev['netUploadSpeedKbps']   = nb.get('uploadSpeedKbps', '')
        ev['netSpeedTestAgeMs']    = nb.get('timeSinceLastSpeedTestMs', '')

    # Graphics / display status
    for gs in dev.get('graphicsStatusReport', [])[:1]:
        displays = gs.get('displays', [])
        ev['displayCount'] = len(displays)
        for i, d in enumerate(displays[:2]):
            if not isinstance(d, dict):
                continue
            p = f'display{i}'
            ev[f'{p}Name']             = d.get('displayName', '')
            ev[f'{p}ResolutionWidth']  = d.get('resolutionWidth', '')
            ev[f'{p}ResolutionHeight'] = d.get('resolutionHeight', '')
            ev[f'{p}RefreshRate']      = d.get('refreshRate', '')
            ev[f'{p}IsInternal']       = d.get('isInternal', '')

    # Audio status
    for ar in dev.get('audioStatusReport', [])[:1]:
        inputs  = ar.get('inputDevice', [])
        outputs = ar.get('outputDevice', [])
        ev['audioInputCount']  = len(inputs)
        ev['audioOutputCount'] = len(outputs)
        for i, d in enumerate(inputs[:2]):
            if not isinstance(d, dict):
                continue
            p = f'audioInput{i}'
            ev[f'{p}Name']   = d.get('name', '')
            ev[f'{p}Active'] = d.get('active', '')
            ev[f'{p}Muted']  = d.get('muted', '')
        for i, d in enumerate(outputs[:2]):
            if not isinstance(d, dict):
                continue
            p = f'audioOutput{i}'
            ev[f'{p}Name']   = d.get('name', '')
            ev[f'{p}Active'] = d.get('active', '')
            ev[f'{p}Muted']  = d.get('muted', '')
            ev[f'{p}Volume'] = d.get('volume', '')

    # Kiosk app status
    for ks in dev.get('kioskAppStatusReport', [])[:1]:
        app = ks.get('app', {})
        ev['kioskAppId']      = app.get('appId', '')    or ks.get('appId', '')
        ev['kioskAppVersion'] = app.get('appVersion', '') or ks.get('appVersion', '')
        ev['kioskAppState']   = ks.get('appState', '')
        ev['kioskReportTime'] = ks.get('reportTime', '')

    # Heartbeat / connectivity status
    for hs in dev.get('heartbeatStatusReport', [])[:1]:
        ev['heartbeatState']      = hs.get('state', '')
        ev['heartbeatReportTime'] = hs.get('reportTime', '')

    # Directory API — user data partition disk usage (matches Google Admin Console)
    sn = ev.get('serialNumber', '')
    if sn and sn in dir_lookup:
        v = dir_lookup[sn]
        ev['diskPartitionTotalBytes'] = v['total']
        ev['diskPartitionFreeBytes']  = v['free']
        used = v['total'] - v['free']
        ev['diskPartitionUsedPct'] = round(used / v['total'] * 100, 1)

    events.append(ev)

print(json.dumps(events))
PYEOF

  rm -f "$resp_file"
  if [[ -n "$dir_file" ]]; then rm -f "$dir_file"; fi
}

# ---------------------------------------------------------------------------
# EVENTS mode — one flat event per telemetry event across all types
# Phase 1: all 14 curl calls run in parallel
# Phase 2: process responses sequentially with Python
# ---------------------------------------------------------------------------
run_events() {
  local tok="$1" since_ts="$2"

  declare -A EVENT_TYPES=(
    [NETWORK_STATE_CHANGE]="networkStateChangeEvent"
    [NETWORK_HTTPS_LATENCY_CHANGE]="httpsLatencyChangeEvent"
    [WIFI_SIGNAL_STRENGTH_LOW]="wifiSignalStrengthEvent"
    [WIFI_SIGNAL_STRENGTH_RECOVERED]="wifiSignalStrengthEvent"
    [OS_CRASH]="osCrashEvent"
    [USB_ADDED]="usbPeripheralsEvent"
    [USB_REMOVED]="usbPeripheralsEvent"
    [APP_INSTALLED]="appInstallEvent"
    [APP_UNINSTALLED]="appUninstallEvent"
    [APP_LAUNCHED]="appLaunchEvent"
    [VPN_CONNECTION_STATE_CHANGE]="vpnConnectionStateChangeEvent"
    [AUDIO_SEVERE_UNDERRUN]="audioSevereUnderrunEvent"
  )

  local combined tmp_dir
  combined=$(mktemp)
  tmp_dir=$(mktemp -d)
  echo "[]" > "$combined"

  # Phase 1: launch all API calls in parallel
  for event_type in "${!EVENT_TYPES[@]}"; do
    local payload_key="${EVENT_TYPES[$event_type]}"
    local filter="event_type=${event_type} AND timestamp>=\"${since_ts}\""
    local url="${BASE}/telemetry/events"
    url+="?readMask=$(urlencode "device,user,${payload_key}")"
    url+="&filter=$(urlencode "$filter")"
    url+="&pageSize=1000"
    (
      code=$(curl -sk -o "${tmp_dir}/${event_type}.json" -w "%{http_code}" \
        -H "Authorization: Bearer ${tok}" \
        -H "Accept: application/json" \
        "$url" 2>/dev/null) || code="000"
      echo "$code" > "${tmp_dir}/${event_type}.code"
    ) &
  done
  wait || true  # wait for all parallel curl jobs; ignore individual failures

  # Phase 2: process responses sequentially
  for event_type in "${!EVENT_TYPES[@]}"; do
    local payload_key="${EVENT_TYPES[$event_type]}"
    local body_file="${tmp_dir}/${event_type}.json"
    local code_file="${tmp_dir}/${event_type}.code"

    [[ -f "$code_file" ]] || continue
    local http_code
    http_code=$(cat "$code_file")
    http_code="${http_code:-000}"

    if [[ "$http_code" -lt 200 || "$http_code" -ge 300 ]]; then
      echo "[$(date -u +%FT%TZ)] HTTP ${http_code} eventType=${event_type}: $(cat "$body_file" 2>/dev/null)" >> "$FLEX_LOG" 2>/dev/null || true
      continue
    fi

    [[ -f "$body_file" ]] || continue

    python3 - "$body_file" "$combined" "$event_type" "$payload_key" <<'PYEOF'
import json, sys, re

resp_file, combined_file, event_type, payload_key = sys.argv[1:5]

def sanitize_key(k):
    s = str(k)
    if re.match(r'^HTTP/', s, re.IGNORECASE):
        return 'httpStatusCode'
    clean = re.sub(r'[^a-zA-Z0-9]', '_', s)
    clean = re.sub(r'_+', '_', clean).strip('_')
    return clean or 'unknown'

def flatten(d, prefix=''):
    result = {}
    for k, v in d.items():
        full_k = f'{prefix}_{sanitize_key(k)}' if prefix else sanitize_key(k)
        if isinstance(v, dict):
            result.update(flatten(v, full_k))
        elif isinstance(v, list):
            for i, elem in enumerate(v):
                if isinstance(elem, dict):
                    result.update(flatten(elem, f'{full_k}_{i}'))
                else:
                    result[f'{full_k}_{i}'] = elem
        else:
            result[full_k] = v
    return result

try:
    with open(resp_file) as f:
        resp = json.load(f)
except (json.JSONDecodeError, ValueError):
    sys.exit(0)

if not isinstance(resp, dict) or 'events' not in resp:
    sys.exit(0)

with open(combined_file) as f:
    accumulated = json.load(f)

for item in resp.get('events', []):
    ev = {'telemetryCategory': event_type}

    dev = item.get('device', {})
    ev['deviceId']     = dev.get('deviceId', '')
    ev['serialNumber'] = dev.get('serialNumber', dev.get('deviceId', ''))
    ev['orgUnitId']    = dev.get('orgUnitId', '')

    user = item.get('user', {})
    ev['userEmail'] = user.get('email', '') if user else ''

    ev['reportTime'] = item.get('reportTime', '')

    payload = item.get(payload_key, {})
    if isinstance(payload, dict):
        ev.update(flatten(payload))
    elif isinstance(payload, list):
        for i, elem in enumerate(payload):
            if isinstance(elem, dict):
                ev.update(flatten(elem, str(i)))
            else:
                ev[str(i)] = elem

    accumulated.append(ev)

with open(combined_file, 'w') as f:
    json.dump(accumulated, f)
PYEOF
  done

  python3 -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))" "$combined"
  rm -f "$combined"
  rm -rf "$tmp_dir"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
TOKEN=$(get_token)

case "$MODE" in
  health)
    run_health "$TOKEN"
    ;;
  events)
    SINCE_TS=$(resolve_since "$SINCE")
    run_events "$TOKEN" "$SINCE_TS"
    ;;
  *)
    echo "Unknown mode: $MODE. Use 'health' or 'events'." >&2
    exit 1
    ;;
esac
