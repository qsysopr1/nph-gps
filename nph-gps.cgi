#!/usr/bin/env python3.11
"""Read gpslogger smartphone app output (CGI), log locally, and forward to HA GPSLogger integration.

Key points:
- Keep gpslogger on this host.
- Log ALL incoming params to CSV (forensics / full fidelity).
- Forward ONLY what HA GPSLogger webhook accepts:
    device, latitude, longitude
- device is derived from 'ser' (fallback to 'aid', then 'gpslogger_unknown')
- Uses application/x-www-form-urlencoded (GPSLogger compatible)

Options:
- debug = 1        -> verbose debug output (includes HA response)
- enable_ha = 0|1 -> enable/disable HA webhook forwarding
"""

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*cgi.*")
warnings.filterwarnings("ignore", category=DeprecationWarning, message=".*cgitb.*")

import os
import cgi
import cgitb
import datetime
import sys
import csv
import urllib.request
import urllib.error
import urllib.parse

# Enable CGI traceback (hidden from clients)
cgitb.enable(display=0, logdir=None)

# ------------------------------ Options ------------------------------
debug = 0        # 1 = verbose debug output
enable_ha = 1    # 1 = forward to Home Assistant webhook

# ------------------------------ Configuration ------------------------------
CSV_FILE_PATH = "/var/www/stat/mailtmp/obd/gps.csv"
HA_WEBHOOK_URL = (
    "https://ha.example.com:8123/api/webhook/"
    "a3c04d4fd177100b896f8417ca8bf72d4c03345daafab6ff0aea6afd2d5c41bf"
)
HA_TIMEOUT_SECONDS = 5


def log_debug(msg: str) -> None:
    """Emit debug output only when debug=1."""
    if debug == 1:
        warnings.warn(msg)


def send_to_home_assistant_form(payload: dict) -> bool:
    """POST form-encoded payload to HA GPSLogger webhook."""
    if enable_ha != 1:
        log_debug("HA webhook disabled (enable_ha=0)")
        return False

    try:
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

        log_debug(f"HA POST URL: {HA_WEBHOOK_URL}")
        log_debug(f"HA POST payload: {payload}")
        log_debug(f"HA POST body: {encoded!r}")

        with urllib.request.urlopen(req, timeout=HA_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")

            log_debug(f"HA response status: {status}")
            log_debug(f"HA response headers: {dict(resp.headers)}")
            log_debug(f"HA response body: {body}")

            return 200 <= int(status) < 300

    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = "<unreadable>"

        log_debug(f"HA HTTPError status: {e.code}")
        log_debug(f"HA HTTPError headers: {dict(e.headers)}")
        log_debug(f"HA HTTPError body: {err_body}")
        return False

    except urllib.error.URLError as e:
        log_debug(f"HA URLError: {e.reason}")
        return False

    except Exception as e:
        log_debug(f"HA unexpected exception: {e}")
        return False


def write_csv_row(params: dict, remote_addr: str) -> None:
    """Write a stable, human-readable CSV row (does not affect HA forwarding)."""
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


def main() -> None:
    try:
        form = cgi.FieldStorage()

        # Preserve gpslogger semantics: single-value keys
        params = {key: form.getfirst(key) for key in form.keys()}

        # Proper CGI response
        print("Status: 200 OK")
        print("Content-Type: text/plain; charset=utf-8")
        print()

        # gpslogger always has timestamp; if not, ignore silently
        if "timestamp" not in params or not params.get("timestamp"):
            log_debug("Missing or empty timestamp; ignoring request")
            return

        remote_addr = (os.environ.get("REMOTE_ADDR") or "").strip()
        log_debug(f"REMOTE_ADDR: {remote_addr}")
        log_debug(f"Incoming gpslogger params: {params}")

        # 1) Local CSV logging (full fidelity is preserved elsewhere; CSV is stable summary)
        write_csv_row(params, remote_addr)

        # 2) HA GPSLogger integration is STRICT: only send accepted keys
        device = (params.get("ser") or "").strip()
        if not device:
            device = (params.get("aid") or "").strip()
        if not device:
            device = "gpslogger_unknown"

        lat = (params.get("lat") or "").strip()
        lon = (params.get("lon") or "").strip()

        # If GPSLogger didn't send lat/lon, don't bother HA
        if not lat or not lon:
            log_debug("Missing lat/lon; skipping HA forward")
            return

        ha_payload = {
            "device": device,
            "latitude": lat,
            "longitude": lon,
        }

        ha_ok = send_to_home_assistant_form(ha_payload)

        if debug == 1:
            print(f"logged=1 ha_enabled={enable_ha} ha_forwarded={1 if ha_ok else 0}")

    except Exception as e:
        log_debug(f"Fatal intake exception: {e}")
        sys.exit(0)


if __name__ == "__main__":
    main()
