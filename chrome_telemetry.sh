#!/usr/bin/env bash
# Chrome Management Telemetry API query script
# Docs: https://developers.google.com/chrome/management/guides/telemetry_api
#
# USAGE:
#   ./chrome_telemetry.sh [command] [options]
#
# COMMANDS:
#   monitor              Pull device health + key events in one report (default since: 24h)
#   devices              List telemetry for all devices
#   device <NAME>        Get telemetry for a single device (name = customers/*/telemetry/devices/DEVICE_ID)
#   users                List telemetry for all users
#   events               List telemetry events
#
# GLOBAL OPTIONS:
#   -c, --customer       Customer ID or "my_customer" (default: C044xpmyc)
#   -k, --key-file       Path to service account JSON key file
#   -t, --token          Raw OAuth access token (overrides --key-file / gcloud)
#   -m, --read-mask      Comma-separated readMask fields
#   -f, --filter         API filter string
#   -s, --since          Events since timestamp (ISO 8601) or shorthand: 1h, 24h, 7d, 30d
#   -p, --page-size      Results per page (default: 100, max: 1000)
#   --all-pages          Automatically fetch all pages
#   -o, --output         Output file (default: stdout)
#   -q, --quiet          Suppress progress messages
#   -h, --help           Show this help
#
# EXAMPLES:
#   # Full health + event monitor report (last 24 hours)
#   ./chrome_telemetry.sh monitor
#
#   # Monitor report for last 7 days, saved to file
#   ./chrome_telemetry.sh monitor --since 7d --output report.json
#
#   # List devices with CPU and memory info
#   ./chrome_telemetry.sh devices \
#     --read-mask "name,serialNumber,cpuStatusReport,memoryStatusReport"
#
#   # List devices with battery info, all pages
#   ./chrome_telemetry.sh devices --all-pages \
#     --read-mask "name,serialNumber,batteryStatusReport"
#
#   # Get a single device
#   ./chrome_telemetry.sh device "customers/my_customer/telemetry/devices/DEVICE_ID" \
#     --read-mask "name,serialNumber,cpuInfo,batteryInfo"
#
#   # List network state change events since a date
#   ./chrome_telemetry.sh events \
#     --read-mask "device,user,networkStateChangeEvent" \
#     --filter 'event_type=NETWORK_STATE_CHANGE AND timestamp>="2026-01-01T00:00:00Z"'

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# Can be overridden by environment variables or --customer / --key-file flags
# ---------------------------------------------------------------------------
CHROME_CUSTOMER_ID="${CHROME_CUSTOMER_ID:-C044xpmyc}"
CHROME_KEY_FILE="${CHROME_KEY_FILE:-$(dirname "$0")/chromedevicemanagement-491916-387cd746f5a6.json}"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
CUSTOMER="$CHROME_CUSTOMER_ID"
KEY_FILE="$CHROME_KEY_FILE"
TOKEN=""
READ_MASK=""
FILTER=""
SINCE=""
PAGE_SIZE="100"
ALL_PAGES=false
OUTPUT_FILE=""
QUIET=false
COMMAND=""
DEVICE_NAME=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { [[ "$QUIET" == "false" ]] && echo "[INFO] $*" >&2 || true; }
err() { echo "[ERROR] $*" >&2; }
die() { err "$*"; exit 1; }

usage() {
  grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \?//'
  exit 0
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
parse_args() {
  if [[ $# -eq 0 ]]; then usage; fi

  case "$1" in
    monitor|devices|users|events) COMMAND="$1"; shift ;;
    device)
      COMMAND="device"
      [[ $# -ge 2 ]] || die "Command 'device' requires a device name argument"
      DEVICE_NAME="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) die "Unknown command: $1. Run with --help for usage." ;;
  esac

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -c|--customer)    CUSTOMER="$2";    shift 2 ;;
      -k|--key-file)    KEY_FILE="$2";    shift 2 ;;
      -t|--token)       TOKEN="$2";       shift 2 ;;
      -m|--read-mask)   READ_MASK="$2";   shift 2 ;;
      -f|--filter)      FILTER="$2";      shift 2 ;;
      -s|--since)       SINCE="$2";       shift 2 ;;
      -p|--page-size)   PAGE_SIZE="$2";   shift 2 ;;
      -o|--output)      OUTPUT_FILE="$2"; shift 2 ;;
      --all-pages)      ALL_PAGES=true;   shift ;;
      -q|--quiet)       QUIET=true;       shift ;;
      -h|--help)        usage ;;
      *) die "Unknown option: $1" ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# Convert --since shorthand (1h, 24h, 7d, 30d) or passthrough ISO 8601
# ---------------------------------------------------------------------------
resolve_since() {
  local since="$1"
  # If already looks like ISO 8601, pass through
  if [[ "$since" == *"T"* ]]; then
    echo "$since"
    return
  fi
  require_cmd python3
  python3 - "$since" <<'PYEOF'
import sys, datetime, re
val = sys.argv[1]
m = re.fullmatch(r'(\d+)(h|d)', val)
if not m:
    print(val); sys.exit(0)
n, unit = int(m.group(1)), m.group(2)
delta = datetime.timedelta(hours=n) if unit == 'h' else datetime.timedelta(days=n)
ts = datetime.datetime.now(datetime.timezone.utc) - delta
print(ts.strftime('%Y-%m-%dT%H:%M:%SZ'))
PYEOF
}

# ---------------------------------------------------------------------------
# Auth: resolve an access token with the correct Chrome Management scope
# ---------------------------------------------------------------------------
get_token() {
  if [[ -n "$TOKEN" ]]; then
    echo "$TOKEN"
    return
  fi

  local key="${KEY_FILE}"
  [[ -f "$key" ]] || die "Key file not found: $key. Use --key-file to specify path."

  require_cmd python3
  log "Obtaining scoped access token from $key"
  python3 - <<EOF
from google.oauth2 import service_account
import google.auth.transport.requests, sys

try:
    creds = service_account.Credentials.from_service_account_file(
        '$key',
        scopes=['https://www.googleapis.com/auth/chrome.management.telemetry.readonly']
    )
    creds.refresh(google.auth.transport.requests.Request())
    print(creds.token)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
EOF
}

# ---------------------------------------------------------------------------
# Build request URL
# ---------------------------------------------------------------------------
build_url() {
  local base="https://chromemanagement.googleapis.com/v1/customers/${CUSTOMER}"
  local endpoint="$1"
  local params=""

  if [[ -n "$READ_MASK" ]]; then
    params+="&readMask=$(urlencode "$READ_MASK")"
  fi
  if [[ -n "$FILTER" ]]; then
    params+="&filter=$(urlencode "$FILTER")"
  fi
  params+="&pageSize=${PAGE_SIZE}"
  params="${params#&}"

  echo "${base}/${endpoint}${params:+?${params}}"
}

# ---------------------------------------------------------------------------
# URL encoding helper (pure bash)
# ---------------------------------------------------------------------------
urlencode() {
  local string="$1"
  local encoded=""
  local i c hex
  for (( i=0; i<${#string}; i++ )); do
    c="${string:$i:1}"
    case "$c" in
      [a-zA-Z0-9.~_,-]) encoded+="$c" ;;
      *) printf -v hex '%02X' "'$c"; encoded+="%${hex}" ;;
    esac
  done
  echo "$encoded"
}

# ---------------------------------------------------------------------------
# Single API request — writes JSON to stdout
# ---------------------------------------------------------------------------
api_get() {
  local url="$1"
  local tok="$2"
  require_cmd curl
  local http_code body tmp
  tmp=$(mktemp)
  http_code=$(curl -sk -o "$tmp" -w "%{http_code}" \
    -H "Authorization: Bearer ${tok}" \
    -H "Accept: application/json" \
    "$url")
  body=$(cat "$tmp"); rm -f "$tmp"
  if [[ "$http_code" -lt 200 || "$http_code" -ge 300 ]]; then
    err "HTTP ${http_code}: $body"
    return 1
  fi
  echo "$body"
}

# ---------------------------------------------------------------------------
# Paginated fetch — collects all pages and merges the array field
# ---------------------------------------------------------------------------
fetch_all_pages() {
  local base_url="$1"
  local tok="$2"
  local array_key="$3"

  require_cmd python3

  local page_token=""
  local page=0
  local combined
  combined=$(mktemp)
  echo "[]" > "$combined"

  while true; do
    page=$(( page + 1 ))
    log "Fetching page ${page}…"

    local url="$base_url"
    if [[ -n "$page_token" ]]; then
      if [[ "$url" == *"?"* ]]; then
        url+="&pageToken=${page_token}"
      else
        url+="?pageToken=${page_token}"
      fi
    fi

    local response_file
    response_file=$(mktemp)
    api_get "$url" "$tok" > "$response_file" || { rm -f "$response_file" "$combined"; die "API request failed"; }

    page_token=$(python3 - "$response_file" "$combined" "$array_key" <<'PYEOF'
import json, sys
resp_file, combined_file, key = sys.argv[1], sys.argv[2], sys.argv[3]
with open(resp_file) as f:
    response = json.load(f)
with open(combined_file) as f:
    accumulated = json.load(f)
accumulated.extend(response.get(key, []))
with open(combined_file, 'w') as f:
    json.dump(accumulated, f)
print(response.get('nextPageToken', ''))
PYEOF
)
    rm -f "$response_file"
    [[ -z "$page_token" ]] && break
  done

  python3 - "$combined" "$array_key" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    items = json.load(f)
print(json.dumps({sys.argv[2]: items, 'totalCount': len(items)}, indent=2))
PYEOF
  rm -f "$combined"
}

# ---------------------------------------------------------------------------
# Fetch events for a single event_type — writes JSON array to a temp file
# Prints the temp file path to stdout
# ---------------------------------------------------------------------------
fetch_events_for_type() {
  local event_type="$1"
  local read_mask="$2"
  local tok="$3"
  local since_ts="$4"

  local base="https://chromemanagement.googleapis.com/v1/customers/${CUSTOMER}/telemetry/events"
  local filter="event_type=${event_type}"
  [[ -n "$since_ts" ]] && filter+=" AND timestamp>=\"${since_ts}\""

  local url="${base}?readMask=$(urlencode "$read_mask")&filter=$(urlencode "$filter")&pageSize=${PAGE_SIZE}"

  local out
  out=$(mktemp)

  local resp_file
  resp_file=$(mktemp)
  if api_get "$url" "$tok" > "$resp_file" 2>/dev/null; then
    python3 - "$resp_file" "$out" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
with open(sys.argv[2], 'w') as f:
    json.dump(d.get('events', []), f)
PYEOF
  else
    echo "[]" > "$out"
  fi
  rm -f "$resp_file"
  echo "$out"
}

# ---------------------------------------------------------------------------
# Monitor: device health + key events combined report
# ---------------------------------------------------------------------------
run_monitor() {
  local tok="$1"
  local since_ts="$2"

  log "Fetching device health (CPU, memory, storage, battery, network, OS)…"
  local device_file
  device_file=$(mktemp)
  local device_url
  device_url="https://chromemanagement.googleapis.com/v1/customers/${CUSTOMER}/telemetry/devices"
  device_url+="?readMask=$(urlencode "name,serialNumber,cpuInfo,cpuStatusReport,memoryInfo,memoryStatusReport,storageInfo,storageStatusReport,batteryInfo,batteryStatusReport,networkInfo,networkStatusReport,osUpdateStatus")&pageSize=${PAGE_SIZE}"
  api_get "$device_url" "$tok" > "$device_file" || die "Failed to fetch device health"

  local since_label="${since_ts:-all time}"
  log "Fetching events since ${since_label}…"

  log "  → NETWORK_STATE_CHANGE";      local f_network;      f_network=$(fetch_events_for_type      "NETWORK_STATE_CHANGE"       "device,user,networkStateChangeEvent"  "$tok" "$since_ts")
  log "  → NETWORK_HTTPS_LATENCY_CHANGE"; local f_latency;   f_latency=$(fetch_events_for_type      "NETWORK_HTTPS_LATENCY_CHANGE" "device,user,httpsLatencyChangeEvent" "$tok" "$since_ts")
  log "  → WIFI_SIGNAL_STRENGTH_LOW";  local f_wifi_low;     f_wifi_low=$(fetch_events_for_type     "WIFI_SIGNAL_STRENGTH_LOW"    "device,user,wifiSignalStrengthEvent"  "$tok" "$since_ts")
  log "  → WIFI_SIGNAL_STRENGTH_RECOVERED"; local f_wifi_ok; f_wifi_ok=$(fetch_events_for_type      "WIFI_SIGNAL_STRENGTH_RECOVERED" "device,user,wifiSignalStrengthEvent" "$tok" "$since_ts")
  log "  → OS_CRASH";                  local f_crash;        f_crash=$(fetch_events_for_type        "OS_CRASH"                    "device,user,osCrashEvent"             "$tok" "$since_ts")
  log "  → USB_ADDED";                 local f_usb_add;      f_usb_add=$(fetch_events_for_type      "USB_ADDED"                   "device,user,usbPeripheralsEvent"      "$tok" "$since_ts")
  log "  → USB_REMOVED";               local f_usb_rm;       f_usb_rm=$(fetch_events_for_type       "USB_REMOVED"                 "device,user,usbPeripheralsEvent"      "$tok" "$since_ts")
  log "  → APP_INSTALLED";             local f_app_install;  f_app_install=$(fetch_events_for_type  "APP_INSTALLED"               "device,user,appInstallEvent"          "$tok" "$since_ts")
  log "  → APP_UNINSTALLED";           local f_app_uninstall; f_app_uninstall=$(fetch_events_for_type "APP_UNINSTALLED"           "device,user,appUninstallEvent"        "$tok" "$since_ts")

  python3 - "$device_file" "$f_network" "$f_latency" "$f_wifi_low" "$f_wifi_ok" \
             "$f_crash" "$f_usb_add" "$f_usb_rm" "$f_app_install" "$f_app_uninstall" \
             "${CUSTOMER}" "${since_ts:-all time}" <<'PYEOF'
import json, sys, datetime

def load(path):
    with open(path) as fh:
        return json.load(fh)

device_resp   = load(sys.argv[1])
ev_network    = load(sys.argv[2])
ev_latency    = load(sys.argv[3])
ev_wifi_low   = load(sys.argv[4])
ev_wifi_ok    = load(sys.argv[5])
ev_crash      = load(sys.argv[6])
ev_usb_add    = load(sys.argv[7])
ev_usb_rm     = load(sys.argv[8])
ev_app_in     = load(sys.argv[9])
ev_app_out    = load(sys.argv[10])
customer      = sys.argv[11]
since         = sys.argv[12]

report = {
    "generatedAt": datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    "customer": customer,
    "since": since,
    "deviceHealth": {
        "totalDevices": len(device_resp.get('devices', [])),
        "devices": device_resp.get('devices', [])
    },
    "events": {
        "networkStateChanges":  {"count": len(ev_network),  "items": ev_network},
        "httpsLatencyChanges":  {"count": len(ev_latency),  "items": ev_latency},
        "wifiSignalLow":        {"count": len(ev_wifi_low), "items": ev_wifi_low},
        "wifiSignalRecovered":  {"count": len(ev_wifi_ok),  "items": ev_wifi_ok},
        "osCrashes":            {"count": len(ev_crash),    "items": ev_crash},
        "usbAdded":             {"count": len(ev_usb_add),  "items": ev_usb_add},
        "usbRemoved":           {"count": len(ev_usb_rm),   "items": ev_usb_rm},
        "appsInstalled":        {"count": len(ev_app_in),   "items": ev_app_in},
        "appsUninstalled":      {"count": len(ev_app_out),  "items": ev_app_out},
    }
}
print(json.dumps(report, indent=2))
PYEOF

  rm -f "$device_file" "$f_network" "$f_latency" "$f_wifi_low" "$f_wifi_ok" \
        "$f_crash" "$f_usb_add" "$f_usb_rm" "$f_app_install" "$f_app_uninstall"
}

# ---------------------------------------------------------------------------
# Output result
# ---------------------------------------------------------------------------
output_result() {
  local data="$1"
  if command -v python3 >/dev/null 2>&1; then
    data=$(echo "$data" | python3 -m json.tool 2>/dev/null || echo "$data")
  fi
  if [[ -n "$OUTPUT_FILE" ]]; then
    echo "$data" > "$OUTPUT_FILE"
    log "Output written to $OUTPUT_FILE"
  else
    echo "$data"
  fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  parse_args "$@"

  local tok
  tok=$(get_token)
  log "Token obtained. Querying as customer: ${CUSTOMER}"

  local result

  case "$COMMAND" in
    monitor)
      local since_ts=""
      [[ -n "$SINCE" ]] && since_ts=$(resolve_since "$SINCE") || since_ts=$(resolve_since "24h")
      result=$(run_monitor "$tok" "$since_ts")
      ;;

    devices)
      local url
      url=$(build_url "telemetry/devices")
      if [[ "$ALL_PAGES" == "true" ]]; then
        result=$(fetch_all_pages "$url" "$tok" "devices")
      else
        result=$(api_get "$url" "$tok") || die "API request failed"
      fi
      ;;

    device)
      local url="https://chromemanagement.googleapis.com/v1/${DEVICE_NAME}"
      if [[ -n "$READ_MASK" ]]; then
        url+="?readMask=$(urlencode "$READ_MASK")"
      fi
      result=$(api_get "$url" "$tok") || die "API request failed"
      ;;

    users)
      local url
      url=$(build_url "telemetry/users")
      if [[ "$ALL_PAGES" == "true" ]]; then
        result=$(fetch_all_pages "$url" "$tok" "users")
      else
        result=$(api_get "$url" "$tok") || die "API request failed"
      fi
      ;;

    events)
      local url
      url=$(build_url "telemetry/events")
      if [[ "$ALL_PAGES" == "true" ]]; then
        result=$(fetch_all_pages "$url" "$tok" "events")
      else
        result=$(api_get "$url" "$tok") || die "API request failed"
      fi
      ;;

    *)
      die "No command specified. Run with --help for usage."
      ;;
  esac

  output_result "$result"
}

main "$@"
