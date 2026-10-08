#!/usr/bin/env python3
"""
tilt_logger.py — Log Tilt / Tilt Pro / Tilt Mini Pro hydrometer readings to a JSON Lines file.
v2: runtime-adjustable interval (via settings file) + resilient writes for
network shares.

Listens for the Tilt's iBeacon Bluetooth LE advertisements and appends one JSON
object per received beacon to a log file (JSON Lines format: one object per line).

Message format (Apple iBeacon, manufacturer ID 0x004C):
  - 16-byte proximity UUID identifies the Tilt's colour
  - 'major' (uint16, big-endian) = temperature in °F        (Pro models: °F x 10)
  - 'minor' (uint16, big-endian) = specific gravity x 1000  (Pro models: x 10000)
  - final byte = TX power (int8, dBm) — on battery-reporting firmware, this
    same byte is also used to signal battery age; see below.

Pro / Mini Pro detection follows the community convention (used by TiltBridge):
a raw 'minor' (gravity) value >= 5000 means a high-resolution Pro model.

Battery age is NOT an officially documented Tilt field, but it IS a real,
intentional feature of some Tilt firmware, implemented by the open-source
TiltBridge project (not a stateless per-byte guess): a Tilt that supports it
broadcasts a TX-power byte of exactly -59 dBm (0xC5 / 197 unsigned) as a
one-time "I support battery reporting" marker; once that has been seen from
a given Tilt, every later reading's TX-power byte is no longer a dBm value at
all — it's repurposed to carry the number of *weeks since the battery was
last changed*. A Tilt that doesn't support this just keeps sending its normal
(negative) calibration constant forever and never triggers the switch-over.
This logger mirrors that exact state machine per colour (see
TiltLogger._battery_weeks) rather than guessing from a single reading, and
additionally ignores a post-trigger reading if it comes back negative (which
shouldn't happen per the above, but a negative "weeks" value is never
meaningful, so it's treated as noise rather than displayed). Each record's
"battery_weeks" is null except on Tilts/firmware that actually support this.

Sources:
  - https://github.com/thorrak/tiltbridge — src/tilt/tiltHydrometer.cpp (the
    `m_has_sent_197` / `receives_battery` / `weeks_since_last_battery_change`
    state machine this logger ports) and tiltHydrometer.h (field comments)
  - https://kvurd.com/blog/tilt-hydrometer-ibeacon-data-format/ (iBeacon
    field layout only — this page does not document battery reporting; an
    earlier version of this script mis-cited it for that claim)
  - https://tilthydrometer.com/products/tilt-pro-wireless-hydrometer-and-thermometer

Settings file (default: logger-settings.json beside the log file) is re-read
every ~5 seconds, so the dashboard can change the logging interval without a
service restart:  {"interval": 60}

If the log destination becomes unwritable (e.g. a network share drops),
readings are buffered in memory (up to 20,000) and flushed when it returns.

Requires: Python 3.9+, bleak (pip install bleak). Run on Linux with BlueZ.
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from bleak import BleakScanner
except ImportError:
    sys.exit("The 'bleak' package is required. Install it with:\n"
             "  sudo apt install python3-pip && pip3 install bleak")

APPLE_COMPANY_ID = 0x004C  # iBeacon manufacturer ID

# TX-power byte value (-59 dBm, i.e. unsigned 197 / 0xC5) that battery-reporting
# Tilt firmware sends once as an "I support battery reporting" marker before it
# starts repurposing the TX-power byte for weeks-since-battery-change instead.
# See TiltLogger._battery_weeks and the module docstring.
BATTERY_SENTINEL_DBM = -59

# Colour UUIDs are shared by all Tilts of the same colour (standard and Pro).
TILT_COLOURS = {
    "a495bb10c5b14b44b5121370f02d74de": "Red",
    "a495bb20c5b14b44b5121370f02d74de": "Green",
    "a495bb30c5b14b44b5121370f02d74de": "Black",
    "a495bb40c5b14b44b5121370f02d74de": "Purple",
    "a495bb50c5b14b44b5121370f02d74de": "Orange",
    "a495bb60c5b14b44b5121370f02d74de": "Blue",
    "a495bb70c5b14b44b5121370f02d74de": "Yellow",
    "a495bb80c5b14b44b5121370f02d74de": "Pink",
}

MAX_BUFFER = 20000        # readings held in memory while the log is unwritable
SETTINGS_POLL_S = 5.0     # how often the settings file is re-checked

log = logging.getLogger("tilt_logger")


def parse_tilt(payload: bytes):
    """Parse the Apple manufacturer-data payload of an iBeacon advertisement.

    `payload` is the manufacturer data *without* the 2-byte company ID
    (that's how bleak delivers it). Returns a dict or None if not a Tilt.
    """
    # iBeacon payload: 0x02 0x15 + 16-byte UUID + major(2) + minor(2) + tx(1) = 23 bytes
    if len(payload) < 23 or payload[0] != 0x02 or payload[1] != 0x15:
        return None

    uuid = payload[2:18].hex()
    colour = TILT_COLOURS.get(uuid)
    if colour is None:
        return None  # some other iBeacon, not a Tilt

    raw_major = int.from_bytes(payload[18:20], "big")   # temperature field
    raw_minor = int.from_bytes(payload[20:22], "big")   # gravity field
    tx_power = int.from_bytes(payload[22:23], "big", signed=True)

    # Pro / Mini Pro sends gravity x 10000 and temperature x 10.
    # Convention (TiltBridge): raw gravity >= 5000 => high-resolution Pro.
    is_pro = raw_minor >= 5000
    if is_pro:
        temp_f = raw_major / 10.0
        gravity = raw_minor / 10000.0
    else:
        temp_f = float(raw_major)
        gravity = raw_minor / 1000.0

    # NOTE: battery age is NOT derived here. Telling a normal calibration
    # reading apart from a repurposed "weeks since battery change" reading
    # requires remembering what earlier readings from this same Tilt looked
    # like (see TiltLogger._battery_weeks below) — a single payload in
    # isolation can't tell the two apart.
    return {
        "color": colour,
        "model": "pro" if is_pro else "standard",
        "temp_f": round(temp_f, 1),
        "temp_c": round((temp_f - 32.0) * 5.0 / 9.0, 2),
        "sg": round(gravity, 4 if is_pro else 3),
        "tx_power_dbm": tx_power,
        "raw_major": raw_major,
        "raw_minor": raw_minor,
    }


class TiltLogger:
    def __init__(self, logfile: Path, settings: Path,
                 colour_filter: str | None, min_interval: float):
        self.logfile = logfile
        self.settings = settings
        self.colour_filter = colour_filter.lower() if colour_filter else None
        self.min_interval = min_interval          # seconds; 0 = log every beacon
        # Per-DEVICE state, keyed by the Tilt's Bluetooth address (colour as a
        # fallback if a platform doesn't report one). Two Tilts of the same
        # colour share a colour UUID, so colour alone can't tell them apart.
        self._last_logged: dict[str, float] = {}  # device -> monotonic timestamp
        self._batt_capable: dict[str, bool] = {}  # device -> has ever sent the -59 sentinel
        self._batt_held: dict[str, int] = {}      # device -> battery weeks seen on a throttled beacon
        self._batt_reported: dict[str, int] = {}  # device -> last weeks value written to the journal
        self._count = 0
        self._pending: list[str] = []             # buffered lines while unwritable
        self._last_write_err = 0.0
        self._settings_mtime = None
        self._settings_checked = 0.0
        self._load_settings(startup=True)

    # -- runtime settings ---------------------------------------------------
    def _load_settings(self, startup=False):
        try:
            mtime = os.path.getmtime(self.settings)
        except OSError:
            return
        if mtime == self._settings_mtime:
            return
        self._settings_mtime = mtime
        try:
            with open(self.settings, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            iv = float(cfg.get("interval", self.min_interval))
            if iv < 0:
                iv = 0.0
            if iv != self.min_interval or startup:
                self.min_interval = iv
                log.info("Logging interval set to %s",
                         "every beacon" if iv == 0 else f"{iv:g}s")
        except (OSError, ValueError, TypeError):
            log.warning("Could not parse settings file %s", self.settings)

    def _maybe_reload_settings(self):
        now = time.monotonic()
        if now - self._settings_checked >= SETTINGS_POLL_S:
            self._settings_checked = now
            self._load_settings()

    # -- resilient writing --------------------------------------------------
    def _write_lines(self, lines: list[str]):
        """Append lines to the log; buffer them if the destination is down."""
        try:
            with self.logfile.open("a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line + "\n")
        except OSError as e:
            self._pending.extend(lines)
            if len(self._pending) > MAX_BUFFER:
                del self._pending[: len(self._pending) - MAX_BUFFER]
            now = time.monotonic()
            if now - self._last_write_err > 60:
                self._last_write_err = now
                log.warning("Cannot write %s (%s) — buffering %d reading(s) "
                            "in memory", self.logfile, e, len(self._pending))
            return False
        return True

    # -- battery age (weeks since last battery change) -----------------------
    def _battery_weeks(self, color: str, tx_power_dbm: int):
        """Port of TiltBridge's m_has_sent_197 / receives_battery state machine
        (src/tilt/tiltHydrometer.cpp). `color` here is really a device key:
        the Tilt's Bluetooth address (or its colour if none is reported), so
        two same-colour Tilts each keep their own state.

        A Tilt that supports battery reporting sends TX power == -59 dBm (the
        unsigned byte 197) at least once as a one-time marker; every reading
        after that marker has first been seen has its TX-power byte repurposed
        to carry weeks-since-battery-change instead of a real dBm value. A
        reading that is itself exactly -59 dBm only ever sets/confirms the
        marker and never itself counts as a weeks value (matching the
        upstream if/else — it's indistinguishable from an ordinary
        calibration reading). A Tilt that never deviates from its calibration
        constant simply never produces a weeks value, which is the correct,
        safe default for non-battery-reporting hardware.
        """
        if tx_power_dbm == BATTERY_SENTINEL_DBM:
            self._batt_capable[color] = True
            return None
        if not self._batt_capable.get(color):
            return None
        # Not in the upstream reference, but a negative "weeks" reading is
        # never meaningful — treat it as noise rather than display it.
        return tx_power_dbm if tx_power_dbm >= 0 else None

    # -- beacon handling ----------------------------------------------------
    def handle_advertisement(self, device, adv_data):
        self._maybe_reload_settings()
        payload = adv_data.manufacturer_data.get(APPLE_COMPANY_ID)
        if payload is None:
            return
        reading = parse_tilt(bytes(payload))
        if reading is None:
            return
        if self.colour_filter and reading["color"].lower() != self.colour_filter:
            return

        dev = (device.address or "").upper() or reading["color"]
        reading["battery_weeks"] = self._battery_weeks(dev, reading["tx_power_dbm"])

        now_mono = asyncio.get_event_loop().time()
        last = self._last_logged.get(dev, 0.0)
        # The logging interval applies to EVERY beacon, battery-carrying or
        # not. (Once a Tilt has sent the battery sentinel, every later beacon
        # carries a weeks value, so exempting those readings from the throttle
        # would silently log every beacon no matter what interval is set.) A
        # battery value seen on a throttled beacon is held and attached to the
        # next reading that IS logged, so no battery report is ever lost.
        if self.min_interval and (now_mono - last) < self.min_interval:
            if reading["battery_weeks"] is not None:
                self._batt_held[dev] = reading["battery_weeks"]
            return
        if reading["battery_weeks"] is None and dev in self._batt_held:
            reading["battery_weeks"] = self._batt_held[dev]
        self._batt_held.pop(dev, None)
        has_battery = reading["battery_weeks"] is not None
        self._last_logged[dev] = now_mono

        record = {
            "timestamp": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            **reading,
            "rssi_dbm": adv_data.rssi,
            "address": (device.address or "").upper() or None,
        }
        line = json.dumps(record)

        # flush anything buffered from an outage first, then this reading
        if self._pending:
            backlog, self._pending = self._pending, []
            if not self._write_lines(backlog + [line]):
                return
            log.info("Log destination back — flushed %d buffered reading(s)",
                     len(backlog))
        elif not self._write_lines([line]):
            return

        self._count += 1
        if self._count % 100 == 1:
            log.info("Logged %d readings (latest: %s %.4g SG, %.1f °F, RSSI %d dBm)",
                     self._count, record["color"], record["sg"],
                     record["temp_f"], record["rssi_dbm"])
        if has_battery and self._batt_reported.get(dev) != record["battery_weeks"]:
            self._batt_reported[dev] = record["battery_weeks"]   # journal it once per change
            log.info("%s Tilt reports battery last changed %d week(s) ago",
                     record["color"], record["battery_weeks"])


async def main():
    ap = argparse.ArgumentParser(description="Log Tilt hydrometer iBeacon readings to JSON Lines.")
    ap.add_argument("--logfile", default="/var/log/tilt/tilt.jsonl",
                    help="Path of the JSON Lines log file (default: %(default)s). "
                         "May be on a mounted network share.")
    ap.add_argument("--settings", default=None,
                    help="Settings file the dashboard writes "
                         "(default: logger-settings.json beside the log file)")
    ap.add_argument("--color", default=None,
                    help="Only log this Tilt colour (e.g. Red). Default: all colours.")
    ap.add_argument("--interval", type=float, default=0.0,
                    help="Minimum seconds between logged readings per Tilt. "
                         "0 logs every received beacon (default). "
                         "Overridden by the settings file when present.")
    ap.add_argument("--adapter", default="hci0", help="Bluetooth adapter (default: %(default)s)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    logfile = Path(args.logfile)
    logfile.parent.mkdir(parents=True, exist_ok=True)
    settings = Path(args.settings) if args.settings else logfile.parent / "logger-settings.json"

    tilt = TiltLogger(logfile, settings, args.color, args.interval)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("Scanning for Tilt beacons on %s; logging to %s "
             "(colour=%s, interval=%ss, settings=%s)", args.adapter, logfile,
             args.color or "any", tilt.min_interval, settings)

    scanner = BleakScanner(detection_callback=tilt.handle_advertisement,
                           adapter=args.adapter)
    async with scanner:
        await stop.wait()

    log.info("Stopped. Total readings logged: %d", tilt._count)


if __name__ == "__main__":
    asyncio.run(main())
