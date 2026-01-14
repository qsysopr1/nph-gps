#!/usr/bin/env python3.11
"""
Read GPSLogger smartphone app output (CGI), log locally, and forward to HA GPSLogger integration.

Known-working behavior:
- Log incoming params to CSV
- Forward ONLY device+latitude+longitude to HA GPSLogger webhook

Enhancement behavior (optional, can be disabled instantly):
- Also forward selected optional fields that HA can use:
  battery, accuracy, altitude, speed, direction, provider, activity

Controls:
- debug = 1                -> writes verbose debug to DEBUG_LOG_PATH
- enable_ha = 0|1          -> enable/disable HA forwarding entirely
- ha_send_extras = 0|1     -> 0 = known-working minimal payload (default)
                              1 = include optional whitelisted extras

Notes:
- device is derived from 'ser' (fallback to 'aid', else 'gpslogger_unknown')
- HA GPSLogger webhook is strict: do NOT forward arbitrary keys
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*cgi.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*cgi.*")

import os
import cgi
import cgitb
import datetime
import sys
import csv
import urllib.request
import urllib.error
import urllib.parse

cgitb.enable(display=0, logdir=None)

# ------------------------------ Flags ------------------------------
debug = 1
enable_ha = 1

# QUICK TOGGLE:
# 0 = known-working minimal HA payload (device,latitude,longitude)
# 1 = include optional HA fields (battery,accuracy,altitude,speed,direction,provider,activity)
ha_send_extras = 1

# ------------------------------ Paths / URLs ------------------------------
CSV_FILE_PATH = "/var/www/stat/mailtmp/obd/gps.csv"
DEBUG_LOG_PATH = "/var/www/stat/mailtmp/obd/gps.debug.log"

HA_WEBHOOK_URL = (
    "https://ha.example.com:8123/api/webhook/"
    "a3c04d4ed17f100b896e8417ca8ba72f4c03345daabab6ff0fea6acd2d5c41be"
)
HA_TIMEOUT_SECONDS = 5


def dlog(msg: str) -> None:
    """Write debug to a file when debug=1 (does not depend on Apache log routing)."""
    if debug != 1:
        return
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(DEBUG_LOG_PATH, "a") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        # last resort
        try:
            sys.stderr.write(msg + "\n")
        except Exception:
            pass


def parse_params(form: cgi.FieldStorage) -> dict:
    """
    Safely build params from FieldStorage.list.
    Avoids edge cases where form.keys() can yield non-hashable objects.
    """
    params = {}
    items = getattr(form, "list", None) or []
    for item in items:
        name = getattr(item, "name", None)
        if not isinstance(name, str) or not name:
            continue
        params[name] = getattr(item, "value", "")
    return params


def write_csv_row(params: dict, remote_addr: str) -> None:
    """Stable CSV summary (full fidelity still in debug log if enabled)."""
    row = [
        datetime.datetime.now().strftime("%H:%M:%S %m/%d/%y"),
        params.get("timestamp", ""),
        params.get("batt", ""),
        remote_addr,
        params.get("lon", ""),
        params.get("lat", ""),
        params.get("acc", ""),
        params.get("desc", ""),
    ]
    with open(CSV_FILE_PATH, "a", newline="") as f:
        csv.writer(f).writerow(row)


def build_ha_payload(params: dict) -> dict:
    """Build HA payload. Minimal by default; optional extras behind ha_send_extras."""
    device = (params.get("ser") or "").strip() or (params.get("aid") or "").strip() or "gpslogger_unknown"
    lat = (params.get("lat") or "").strip()
    lon = (params.get("lon") or "").strip()

    payload = {
        "device": device,
        "latitude": lat,
        "longitude": lon,
    }

    if ha_send_extras != 1:
        return payload

    # Optional whitelisted extras (send ONLY if present and non-empty)
    # GPSLogger -> HA key mapping:
    # batt -> battery
    # acc  -> accuracy
    # alt  -> altitude
    # spd  -> speed
    # dir  -> direction
    # prov -> provider
    # act  -> activity
    mapping = {
        "batt": "battery",
        "acc": "accuracy",
        "alt": "altitude",
        "spd": "speed",
        "dir": "direction",
        "prov": "provider",
        "act": "activity",
    }

    for src, dst in mapping.items():
        v = (params.get(src) or "").strip()
        if v != "":
            payload[dst] = v

    return payload


def send_to_home_assistant_form(payload: dict) -> tuple[bool, str]:
    """POST form-encoded payload to HA GPSLogger webhook and return (ok, info)."""
    if enable_ha != 1:
        return False, "ha_disabled"

    encoded = urllib.parse.urlencode(payload, doseq=False).encode("utf-8")
    req = urllib.request.Request(
        HA_WEBHOOK_URL,
        data=encoded,
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": "gpslogger-intake/1.0",
            "Content-Length": str(len(encoded)),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=HA_TIMEOUT_SECONDS) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
            return 200 <= int(status) < 300, f"{status} {body}".strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, f"{e.code} {body}".strip()
    except Exception as e:
        return False, f"exc {e}"


def main() -> None:
    # Always return 200 so GPSLogger doesn’t stop sending while you debug.
    print("HTTP/1.1 200 OK")
    print("Content-Type: text/plain")
    print("Connection: close")
    print()
    print("OK")

    remote_addr = (os.environ.get("REMOTE_ADDR") or "").strip()

    try:
        dlog(f"HIT remote_addr={remote_addr} qs={os.environ.get('QUERY_STRING','')!r}")

        form = cgi.FieldStorage()
        params = parse_params(form)

        dlog(f"PARAMS keys={sorted(params.keys())}")

        # Require timestamp to treat as valid GPSLogger telemetry
        ts = (params.get("timestamp") or "").strip()
        if not ts:
            dlog("DROP missing timestamp")
            return

        # Local CSV write first (HA failures won't prevent logging)
        write_csv_row(params, remote_addr)
        dlog("CSV wrote row")

        # Build HA payload (minimal or enriched based on ha_send_extras)
        ha_payload = build_ha_payload(params)

        # Require coordinates before forwarding
        if not ha_payload.get("latitude") or not ha_payload.get("longitude"):
            dlog("SKIP HA missing lat/lon")
            return

        # Show exactly what sending to HA
        dlog(f"HA payload minimal={ha_send_extras == 0} keys={sorted(ha_payload.keys())} device={ha_payload.get('device')}")

        ok, info = send_to_home_assistant_form(ha_payload)
        dlog(f"HA ok={ok} info={info}")

        if debug == 1:
            dlog(f"logged=1 ha_enabled={enable_ha} extras={ha_send_extras} ha_forwarded={1 if ok else 0}")

    except Exception as e:
        dlog(f"FATAL {e}")


if __name__ == "__main__":
    main()
