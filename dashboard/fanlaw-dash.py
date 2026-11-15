#!/usr/bin/env python3
"""Read-only dashboard for gpu-fan-curve.sh (Fanlaw v3).

Follows the script's journal for every fan decision. The live gauges come
from sources that never touch the host IPMI channel the fan script reads:

  CPU   coretemp under /sys/class/hwmon, every second
  GPU   one long-lived SSH session streaming nvidia-smi every 2 seconds
  Fans  iDRAC Redfish over the network: right after every logged fan change,
        then every 30 seconds while the fans are steady
  VM    running while the GPU stream answers; otherwise whatever the fan
        script last logged (it already runs qm status itself)

Never runs ipmitool or qm, never writes a fan duty, never edits the script.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import html
import json
import math
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# Curve defaults. At startup these are replaced by the values in the fan
# script itself, so retuning the script retunes the dashboard.
INTERVAL = 15
STATUS_INTERVAL = 300
MIN_FAN = 10
MAX_FAN = 50
ABS_MAX = 100
DEADBAND = 3
STEP_DOWN = 2
OFF_FAN = 10
BLIND_FAN = 25
GPU_MIN, KNEE, GPU_GOAL, GPU_MAX = 35, 65, 70, 83
CPU_MIN, CPU_START, CPU_GOAL, CPU_MAX = 35, 55, 75, 87

# Feed timing (seconds unless noted)
CPU_SEC = 1            # coretemp read
GPU_LOOP_MS = 2000     # nvidia-smi -lms
GPU_STALE = 10         # a GPU reading older than this counts as unreadable
GPU_SILENT = 20        # restart the stream if it says nothing for this long
GPU_RETRY_MAX = 15     # longest wait between reconnect attempts (VM off, booting)
REDFISH_SEC = 30       # steady-state Redfish read
REDFISH_BURST = (3, 8, 15)  # extra reads this long after a logged fan change
REDFISH_GAP = 3        # minimum gap between Redfish requests
REDFISH_TIMEOUT = 10
HIST_SEC = 5           # one history row every 5 s
LIVE_FRESH = 5         # the live feed counts as fresh while coretemp is this recent

SCRIPT_PATH = Path("/usr/local/bin/gpu-fan-curve.sh")
CONFIG_PATH = Path("/etc/fanlaw/fanlaw.conf")
HWMON_ROOT = Path("/sys/class/hwmon")
HISTORY_PATH = Path("/var/lib/fanlaw/history.jsonl")
PERSIST = True  # --demo turns this off so fake samples never reach the real history file

VM_CFG = {"vmid": "", "host": "", "user": "", "key": "/root/.ssh/gpu_fan_key"}
IDRAC_CFG: dict = {"host": "", "user": "", "password": "", "verify_tls": False}
GPU_FIELDS = "name,temperature.gpu,utilization.gpu,power.draw,clocks.gr,memory.used,memory.total"

LINE_RE = re.compile(
    r"^(?P<dow>[A-Za-z]{3})\s+(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+"
    r"(?P<hms>\d{1,2}:\d{2}:\d{2})(?:\s+(?P<ampm>[AaPp][Mm]))?\s+\S+\s+(?P<year>\d{4}):\s*(?P<msg>.*)$"
)
FAN_RE = re.compile(r"Fans=(\d+)%")
GPU_RE = re.compile(r"GPU=(\d+)C")
CPU_RE = re.compile(r"CPU=(\d+)C")
UNIT_RE = re.compile(r"^[A-Za-z0-9:@._-]+$")
SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
ASSIGN_RE = re.compile(r"""^\s*([A-Z_]+)=(?:"([^"]*)"|'([^']*)'|([^\s#]*))""")

SCRIPT_NUMS = {
    "INTERVAL": "INTERVAL", "STATUS_INTERVAL": "STATUS_INTERVAL",
    "MIN_FAN": "MIN_FAN", "MAX_FAN": "MAX_FAN", "ABS_MAX_FAN": "ABS_MAX",
    "DEADBAND": "DEADBAND", "STEP_DOWN": "STEP_DOWN", "OFF_FAN": "OFF_FAN", "BLIND_FAN": "BLIND_FAN",
    "GPU_MIN_TEMP": "GPU_MIN", "KNEE": "KNEE", "GPU_GOAL": "GPU_GOAL", "GPU_MAX_TEMP": "GPU_MAX",
    "CPU_MIN_TEMP": "CPU_MIN", "CPU_START": "CPU_START", "CPU_GOAL": "CPU_GOAL", "CPU_MAX_TEMP": "CPU_MAX",
}
SCRIPT_VM = {"VMID": "vmid", "VM_IP": "host", "VM_USER": "user", "SSH_KEY": "key"}
SETUP_NOTES: list[str] = []  # shown in Diagnostics: where settings came from, what was ignored


def gpu_curve(temp: int) -> int:
    t = int(temp)
    if t <= GPU_MIN:
        return MIN_FAN
    if t <= KNEE:
        return MIN_FAN + (t - GPU_MIN) * (35 - MIN_FAN) // (KNEE - GPU_MIN)
    if t <= GPU_GOAL:
        return 35 + (t - KNEE) * (MAX_FAN - 35) // (GPU_GOAL - KNEE)
    if t < GPU_MAX:
        return MAX_FAN + (t - GPU_GOAL) * (ABS_MAX - MAX_FAN) // (GPU_MAX - GPU_GOAL)
    return ABS_MAX


def cpu_curve(temp: int) -> int:
    c = int(temp)
    if c <= CPU_MIN:
        return MIN_FAN
    if c < CPU_START:
        return MIN_FAN + (c - CPU_MIN) * (15 - MIN_FAN) // (CPU_START - CPU_MIN)
    if c <= CPU_GOAL:
        return 15 + (c - CPU_START) * (MAX_FAN - 15) // (CPU_GOAL - CPU_START)
    if c < CPU_MAX:
        return MAX_FAN + (c - CPU_GOAL) * (ABS_MAX - MAX_FAN) // (CPU_MAX - CPU_GOAL)
    return ABS_MAX


def gpu_zone(temp: int) -> str:
    if temp <= GPU_MIN:
        return "Floor"
    if temp <= KNEE:
        return "Approach"
    if temp <= GPU_GOAL:
        return "Goal"
    if temp < GPU_MAX:
        return "Release"
    return "Limit"


def cpu_zone(temp: int) -> str:
    if temp <= CPU_MIN:
        return "Floor"
    if temp < CPU_START:
        return "Approach"
    if temp <= CPU_GOAL:
        return "Goal"
    if temp < CPU_MAX:
        return "Release"
    return "Limit"


def vote_want(event: dict, gpu_law: int | None, cpu_law: int | None) -> int | None:
    msg = event["msg"]
    fan = event["fan"] if event["fan"] is not None else 0
    if "(holding)" in msg and "driving" not in msg:
        return None
    if "GPU unreadable" in msg and "(holding)" not in msg and "driving" not in msg:
        return max(BLIND_FAN, fan, cpu_law or 0)
    if event["vm"] == "stopped":
        return max(OFF_FAN, cpu_law if cpu_law is not None else OFF_FAN)
    if "driving" in msg and cpu_law is not None:
        base = gpu_law if gpu_law is not None else 0
        if "GPU unreadable" in msg and "(holding)" not in msg:
            base = max(BLIND_FAN, fan)
        return max(base, cpu_law)
    votes = [law for law in (gpu_law, cpu_law) if law is not None]
    return max(votes) if votes else None


def describe_plan(current: int | None, want: int | None) -> str:
    if current is None:
        return "No fan command in the log yet."
    if want is None:
        return "Grace hold. Fans stay put unless the CPU vote rises."
    if want > current:
        return f"Next tick climbs straight to {want}%."
    gap = current - want
    floor_special = want <= OFF_FAN and current > want
    if gap >= DEADBAND or floor_special:
        drop = gap // 3
        if drop < STEP_DOWN:
            drop = STEP_DOWN
        nxt = current - drop
        if nxt < want:
            nxt = want
        return f"Next tick sheds {current - nxt}% toward {want}% → {nxt}%."
    if current == want:
        return "On the vote. The next tick holds."
    return f"Deadband. {gap}% above the vote, under the {DEADBAND}% step."


def phase_of(event: dict, want: int | None) -> str:
    fan = event["fan"]
    msg = event["msg"]
    if fan is None:
        return "No command"
    if want is None or ("(holding)" in msg and "driving" not in msg):
        return "Grace hold"
    if "GPU unreadable" in msg and "(holding)" not in msg and "driving" not in msg:
        return "Blind hold"
    if fan < want:
        return "Climbing"
    if fan > want:
        floor_special = want <= OFF_FAN and fan > want
        if fan - want >= DEADBAND or floor_special:
            return "Settling"
        return "Deadband"
    if event["vm"] == "stopped" and fan == OFF_FAN:
        return "VM floor"
    return "On target"


def driver_label(driver: str) -> str:
    return {"gpu": "GPU curve", "cpu": "CPU curve", "hold": "Holding", "floor": "VM floor"}.get(driver, driver)


def decorate(event: dict) -> dict:
    row = dict(event)
    gpu_law = gpu_curve(row["gpu"]) if row["gpu"] is not None else None
    cpu_law = cpu_curve(row["cpu"]) if row["cpu"] is not None else None
    want = vote_want(row, gpu_law, cpu_law) if row["fan"] is not None else None
    row["gpu_law"] = gpu_law
    row["cpu_law"] = cpu_law
    row["want"] = want
    row["gpu_zone"] = gpu_zone(row["gpu"]) if row["gpu"] is not None else None
    row["cpu_zone"] = cpu_zone(row["cpu"]) if row["cpu"] is not None else None
    row["driver_label"] = driver_label(row["driver"])
    row["phase"] = phase_of(row, want)
    row["plan"] = describe_plan(row["fan"], want)
    return row


def curve_table() -> list[dict]:
    return [{"t": t, "gpu": gpu_curve(t), "cpu": cpu_curve(t)} for t in range(30, 96)]


def build_law() -> dict:
    return {
        "gpu": [GPU_MIN, KNEE, GPU_GOAL, GPU_MAX],
        "cpu": [CPU_MIN, CPU_START, CPU_GOAL, CPU_MAX],
        "min_fan": MIN_FAN,
        "max_fan": MAX_FAN,
        "abs_max": ABS_MAX,
        "deadband": DEADBAND,
        "step_down": STEP_DOWN,
        "off_fan": OFF_FAN,
        "blind_fan": BLIND_FAN,
        "poll": CPU_SEC,
    }


CURVE = curve_table()
LAW = build_law()


# ---------- settings: the fan script, then /etc/fanlaw/fanlaw.conf ----------

def read_script_settings(path: Path) -> tuple[dict, dict, str]:
    """Pull the curve numbers and the VM connection details out of the fan script."""
    try:
        text = path.read_text(errors="replace")
    except OSError as exc:
        return {}, {}, f"can't read {path} ({exc.strerror or exc}); using built-in curve values"
    nums: dict = {}
    vm: dict = {}
    for line in text.splitlines():
        match = ASSIGN_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        value = next(group for group in match.groups()[1:] if group is not None)
        if name in SCRIPT_NUMS and SCRIPT_NUMS[name] not in nums and re.fullmatch(r"\d+", value):
            nums[SCRIPT_NUMS[name]] = int(value)
        elif name in SCRIPT_VM and SCRIPT_VM[name] not in vm and value and value != "CHANGE_ME":
            vm[SCRIPT_VM[name]] = value
    return nums, vm, ""


def apply_script_numbers(nums: dict) -> str:
    merged = {name: nums.get(name, globals()[name]) for name in SCRIPT_NUMS.values()}
    gpu_ok = merged["GPU_MIN"] < merged["KNEE"] < merged["GPU_GOAL"] < merged["GPU_MAX"]
    cpu_ok = merged["CPU_MIN"] < merged["CPU_START"] < merged["CPU_GOAL"] < merged["CPU_MAX"]
    if not (gpu_ok and cpu_ok):
        return "curve anchors in the script aren't in rising order; using built-in curve values"
    globals().update(merged)
    global CURVE, LAW
    CURVE = curve_table()
    LAW = build_law()
    return ""


def read_config(path: Path) -> str:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        if not parser.read(path):
            return f"no {path}: Redfish fan readings are off"
    except configparser.Error as exc:
        return f"{path}: {exc}"
    if parser.has_section("vm"):
        for key in VM_CFG:
            if parser.has_option("vm", key):
                VM_CFG[key] = parser.get("vm", key).strip()
    if parser.has_section("idrac"):
        for key in ("host", "user", "password"):
            IDRAC_CFG[key] = parser.get("idrac", key, fallback="").strip()
        try:
            IDRAC_CFG["verify_tls"] = parser.getboolean("idrac", "verify_tls", fallback=False)
        except ValueError:
            IDRAC_CFG["verify_tls"] = False
    return ""


def load_settings(script: Path, config: Path) -> None:
    nums, vm, err = read_script_settings(script)
    if err:
        SETUP_NOTES.append(err)
    else:
        note = apply_script_numbers(nums)
        if note:
            SETUP_NOTES.append(note)
        else:
            SETUP_NOTES.append(f"curve read from {script}")
    VM_CFG.update(vm)
    err = read_config(config)
    if err:
        SETUP_NOTES.append(err)


def redfish_ready() -> bool:
    return bool(IDRAC_CFG["host"] and IDRAC_CFG["user"]) and "CHANGE_ME" not in (IDRAC_CFG["host"], IDRAC_CFG["user"])


# ---------- journal ----------

def parse_line(raw: str) -> dict | None:
    text = raw.strip()
    if not text:
        return None
    match = LINE_RE.match(text)
    msg = match.group("msg") if match else text
    ts = None
    if match:
        # `date` prints 24h ("09:42:25 CST") or, in en_US locales, 12h ("09:42:25 AM CST").
        hour, minute, second = (int(part) for part in match.group("hms").split(":"))
        ampm = (match.group("ampm") or "").upper()
        if ampm:
            hour = hour % 12 + (12 if ampm == "PM" else 0)
        stamp = f"{match.group('dow')} {match.group('mon')} {int(match.group('day')):02d} {hour:02d}:{minute:02d}:{second:02d} {match.group('year')}"
        try:
            ts = datetime.strptime(stamp, "%a %b %d %H:%M:%S %Y").isoformat()
        except ValueError:
            ts = None
    fan = FAN_RE.search(msg)
    gpu = GPU_RE.search(msg)
    cpu = CPU_RE.search(msg)
    if fan is None and "WARNING" not in msg and "failing safe" not in msg and "Fan control starting" not in msg:
        return None
    if msg.startswith("[status]"):
        kind = "status"
    elif msg.startswith("[temp]"):
        kind = "temp"
    elif "WARNING" in msg or "failing safe" in msg:
        kind = "warn"
    elif "Fan control starting" in msg or "Restoring iDRAC" in msg:
        kind = "warn"
    else:
        kind = "change"
    if "VM off" in msg:
        vm = "stopped"
    elif "GPU=" in msg or "GPU unreadable" in msg or "unreadable" in msg:
        vm = "running"
    else:
        vm = "unknown"
    if "driving" in msg and "CPU=" in msg:
        driver = "cpu"
    elif "VM off" in msg:
        driver = "floor"
    elif "unreadable (holding)" in msg:
        driver = "hold"
    elif "GPU unreadable" in msg:
        driver = "hold"
    elif gpu:
        driver = "gpu"
    else:
        driver = "hold"
    return {
        "ts": ts,
        "kind": kind,
        "msg": msg,
        "fan": int(fan.group(1)) if fan else None,
        "gpu": int(gpu.group(1)) if gpu else None,
        "cpu": int(cpu.group(1)) if cpu else None,
        "vm": vm,
        "driver": driver,
        "gpu_blind": "GPU unreadable" in msg and "(holding)" not in msg,
    }


class Buffer:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.source = ""
        self.error = ""
        self.following = False  # False while the backlog loads, so old changes don't trigger Redfish reads
        self.last_fan: int | None = None
        self.last_vm = "unknown"  # VM state in the script's latest decision line
        self.last_at: float | None = None  # when the latest line arrived (monotonic)

    def add(self, line: str) -> None:
        event = parse_line(line)
        if event is None:
            return
        changed = False
        with self.lock:
            self.events.append(event)
            del self.events[:-4000]
            self.last_at = time.monotonic()
            if event["fan"] is not None:
                changed = self.following and event["fan"] != self.last_fan
                self.last_fan = event["fan"]
                self.last_vm = event["vm"]
        if changed:
            REDFISH.kick()

    def snapshot(self, since: str | None = None) -> dict:
        with self.lock:
            events = list(self.events)
            source = self.source
            error = self.error
        samples = [event for event in events if event["fan"] is not None]
        logged = decorate(samples[-1]) if samples else None
        live = LIVE.view()
        if live["fresh"]:
            latest = assess(live["gpu"], live["cpu"], live["vm"], logged["fan"] if logged else None)
            latest["ts"] = live["ts"]
            age = live["age"]
            stale = False
        else:
            latest = logged
            age = None
            stale = True
            if latest and latest["ts"]:
                then = datetime.fromisoformat(latest["ts"])
                age = max(0, int((datetime.now() - then).total_seconds()))
                stale = age > STATUS_INTERVAL + 60
            elif latest:
                stale = False
                age = 0
        merged: dict[str, dict] = {}
        cutoff = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
        for item in samples[-2000:]:
            if item["ts"] and item["ts"] >= cutoff and (since is None or item["ts"] > since):
                merged[item["ts"]] = {"ts": item["ts"], "gpu": item["gpu"], "cpu": item["cpu"], "fan": item["fan"]}
        for item in live["history"]:
            if item.get("ts") and (since is None or item["ts"] > since):
                merged[item["ts"]] = item
        history = [merged[key] for key in sorted(merged)]
        problems = [text for text in (live["gpu_error"], live["cpu_error"], live["redfish_error"]) if text]
        tape = [decorate(item) for item in reversed(events[-40:])]
        return {
            "ok": not error,
            "error": error,
            "source": source,
            "stale": stale if latest else True,
            "age_s": age,
            "live": live["fresh"],
            "sensor_error": " · ".join(problems),
            "latest": latest,
            "board": live["board"],
            "feeds": live["feeds"],
            "setup": SETUP_NOTES,
            "events": tape,
            "history": history,
            "history_full": since is None,
            "curve": CURVE,
            "law": LAW,
        }


# ---------- live state ----------

def empty_board() -> dict:
    return {
        "cpus": [],
        "idrac_cpus": [],
        "inlet": None,
        "exhaust": None,
        "gpu_name": "",
        "util": None,
        "power": None,
        "clock": None,
        "mem_used": None,
        "mem_total": None,
        "fans": [],
        "fan_error": "",
        "ipmi_error": "",
    }


def assess(gpu: int | None, cpu: int | None, vm: str, fan: int | None, msg: str | None = None) -> dict:
    gpu_law = gpu_curve(gpu) if gpu is not None else None
    cpu_law = cpu_curve(cpu) if cpu is not None else None
    if vm == "stopped":
        want = max(OFF_FAN, cpu_law if cpu_law is not None else OFF_FAN)
        driver = "cpu" if cpu_law is not None and cpu_law > OFF_FAN else "floor"
        built = "VM off" + (f", CPU={cpu}C" if cpu is not None else "")
        if driver == "cpu":
            built += " driving"
    elif gpu is None:
        want = cpu_law
        driver = "cpu" if cpu_law is not None else "hold"
        built = "GPU unreadable" + (f", CPU={cpu}C" if cpu is not None else "")
        if driver == "cpu":
            built += " driving"
    else:
        want = gpu_law if gpu_law is not None else 0
        driver = "gpu"
        built = f"GPU={gpu}C"
        if cpu is not None:
            built += f", CPU={cpu}C"
            if cpu_law is not None and cpu_law > want:
                want = cpu_law
                driver = "cpu"
                built += " driving"
    if fan is not None:
        built += f" -> Fans={fan}%"
    if msg:
        built = msg
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "kind": "live",
        "msg": built,
        "fan": fan,
        "gpu": gpu,
        "cpu": cpu,
        "vm": vm,
        "driver": driver,
        "gpu_blind": gpu is None and vm != "stopped",
        "gpu_law": gpu_law,
        "cpu_law": cpu_law,
        "want": want,
        "gpu_zone": gpu_zone(gpu) if gpu is not None else None,
        "cpu_zone": cpu_zone(cpu) if cpu is not None else None,
        "driver_label": driver_label(driver),
        "live": True,
    }
    row["phase"] = phase_of(row, want)
    row["plan"] = describe_plan(fan, want)
    return row


def compact_history(rows: list[dict]) -> list[dict]:
    """Full resolution for the last hour, one row a minute back to 24 hours."""
    now = datetime.now()
    kept: list[dict] = []
    last_old: datetime | None = None
    for row in rows:
        try:
            ts = datetime.fromisoformat(row["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        age = (now - ts).total_seconds()
        if age > 86400 or age < -120:
            continue
        if age > 3600:
            if last_old is not None and (ts - last_old).total_seconds() < 60:
                continue
            last_old = ts
        kept.append({"ts": row["ts"], "gpu": row.get("gpu"), "cpu": row.get("cpu"), "fan": row.get("fan"), "util": row.get("util"), "rpm": row.get("rpm")})
    return kept[-2500:]


class Live:
    """Latest value from each feed, stamped with when it arrived."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.cpus: list[dict] = []
        self.cpu_at: float | None = None
        self.cpu_error = ""
        self.gpu: dict | None = None
        self.gpu_at: float | None = None
        self.gpu_error = ""
        self.redfish = {"fans": [], "inlet": None, "exhaust": None, "idrac_cpus": []}
        self.redfish_at: float | None = None
        self.redfish_error = ""
        self.history: list[dict] = []
        self.trimmed_at = time.monotonic()

    # --- history file ---

    def load(self) -> None:
        """Read the tail of the history file and, unless in demo mode, rewrite it trimmed to 24 h."""
        if not HISTORY_PATH.exists():
            return
        try:
            with HISTORY_PATH.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 4_000_000))
                raw = handle.read().decode("utf-8", errors="replace")
        except OSError:
            return
        lines = raw.splitlines()
        if size > 4_000_000 and lines:
            lines = lines[1:]  # first line is probably cut in half
        rows: list[dict] = []
        for line in lines:
            try:
                rows.append(json.loads(line))
            except (TypeError, ValueError):
                continue
        with self.lock:
            self.history = compact_history(rows)
        if PERSIST:
            self.trim_file()

    def trim_file(self) -> None:
        with self.lock:
            rows = list(self.history)
        tmp = HISTORY_PATH.with_suffix(".jsonl.tmp")
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            os.replace(tmp, HISTORY_PATH)
        except OSError:
            pass
        self.trimmed_at = time.monotonic()

    def add_history(self, row: dict) -> None:
        with self.lock:
            self.history.append(row)
            if len(self.history) > 2600:
                self.history = compact_history(self.history)
        if not PERSIST:
            return
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with HISTORY_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row) + "\n")
        except OSError:
            pass
        if time.monotonic() - self.trimmed_at > 3600:
            with self.lock:
                self.history = compact_history(self.history)
            self.trim_file()

    # --- feeds ---

    def set_cpu(self, cpus: list[dict], error: str) -> None:
        with self.lock:
            if cpus:
                self.cpus = cpus
                self.cpu_at = time.monotonic()
            self.cpu_error = error

    def set_gpu(self, reading: dict) -> None:
        with self.lock:
            self.gpu = reading
            self.gpu_at = time.monotonic()
            self.gpu_error = ""

    def set_gpu_error(self, error: str) -> None:
        with self.lock:
            self.gpu_error = error

    def set_redfish(self, data: dict | None, error: str) -> None:
        with self.lock:
            if data is not None:
                self.redfish = data
                self.redfish_at = time.monotonic()
            self.redfish_error = error

    def view(self) -> dict:
        now = time.monotonic()

        def age(at: float | None) -> int | None:
            return None if at is None else max(0, int(now - at))

        with BUFFER.lock:
            logged_vm = BUFFER.last_vm
            log_at = BUFFER.last_at
        with self.lock:
            cpu_age = age(self.cpu_at)
            cpu_ok = cpu_age is not None and cpu_age <= LIVE_FRESH
            cpus = list(self.cpus) if cpu_ok else []
            gpu_ok = self.gpu is not None and self.gpu_at is not None and now - self.gpu_at <= GPU_STALE
            # A GPU line means the VM is up. Otherwise go by the script's last decision,
            # which comes from its own qm status (so no second qm call here).
            vm = "running" if gpu_ok else logged_vm
            fresh = cpu_ok or gpu_ok
            live_ages = [a for a in (cpu_age if cpu_ok else None, age(self.gpu_at) if gpu_ok else None) if a is not None]
            gpu = dict(self.gpu) if gpu_ok and self.gpu else {}
            board = empty_board()
            board.update({
                "cpus": cpus,
                "idrac_cpus": list(self.redfish["idrac_cpus"]),
                "inlet": self.redfish["inlet"],
                "exhaust": self.redfish["exhaust"],
                "fans": list(self.redfish["fans"]),
                "fan_error": self.redfish_error,
                "ipmi_error": self.redfish_error,
            })
            if gpu:
                board.update({key: gpu.get(key) for key in ("util", "power", "clock", "mem_used", "mem_total")})
                board["gpu_name"] = gpu.get("name") or ""
            elif self.gpu:
                board["gpu_name"] = self.gpu.get("name") or ""
            return {
                "fresh": fresh,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "age": min(live_ages) if live_ages else None,
                "cpu": max((item["c"] for item in cpus), default=None),
                "gpu": gpu.get("temp") if gpu else None,
                "vm": vm,
                "board": board,
                "feeds": {"cpu": cpu_age, "gpu": age(self.gpu_at), "redfish": age(self.redfish_at), "log": age(log_at)},
                "gpu_error": self.gpu_error if vm != "stopped" else "",
                "cpu_error": self.cpu_error,
                "redfish_error": self.redfish_error,
                "history": list(self.history),
            }


LIVE = Live()
BUFFER = Buffer()


def journal_fan() -> int | None:
    with BUFFER.lock:
        return BUFFER.last_fan


# ---------- feed: CPU (coretemp) ----------

def find_coretemp(root: Path | None = None) -> list[tuple[str, Path]]:
    """[(CPU1, .../temp1_input), ...] for every 'Package id N' coretemp sensor."""
    found: list[tuple[int, str, Path]] = []
    try:
        dirs = sorted((root or HWMON_ROOT).iterdir())
    except OSError:
        return []
    for folder in dirs:
        try:
            if (folder / "name").read_text().strip() != "coretemp":
                continue
        except OSError:
            continue
        for label in sorted(folder.glob("temp*_label")):
            try:
                match = re.match(r"Package id (\d+)", label.read_text().strip())
            except OSError:
                continue
            if match:
                package = int(match.group(1))
                found.append((package, f"CPU{package + 1}", label.with_name(label.name.replace("_label", "_input"))))
    found.sort()
    return [(name, path) for _, name, path in found]


def read_coretemp(sensors: list[tuple[str, Path]]) -> tuple[list[dict], str]:
    cpus: list[dict] = []
    for name, path in sensors:
        try:
            cpus.append({"name": name, "c": round(int(path.read_text().strip()) / 1000)})
        except (OSError, ValueError):
            return cpus, f"can't read {path}"
    return cpus, ""


def cpu_loop() -> None:
    sensors: list[tuple[str, Path]] = []
    while True:
        started = time.monotonic()
        if not sensors:
            sensors = find_coretemp()
        if sensors:
            cpus, err = read_coretemp(sensors)
            if err:
                sensors = []  # rescan next second
        else:
            cpus, err = [], "no coretemp sensors in /sys/class/hwmon (try: modprobe coretemp)"
        LIVE.set_cpu(cpus, err)
        time.sleep(max(0.2, CPU_SEC - (time.monotonic() - started)))


# ---------- feed: GPU (one streaming SSH session; also tells us the VM is up) ----------

def tool(name: str, candidates: list[str]) -> str:
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return name



def parse_gpu_csv(text: str) -> dict | None:
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not line:
        return None

    def as_int(raw: str) -> int | None:
        try:
            return int(float(raw))
        except ValueError:
            return None

    def as_float(raw: str) -> float | None:
        try:
            return float(raw)
        except ValueError:
            return None

    if "," not in line:
        temp = as_int(line)
        if temp is None:
            return None
        return {"name": "", "temp": temp, "util": None, "power": None, "clock": None, "mem_used": None, "mem_total": None}
    parts = [part.strip() for part in line.split(",")]
    temp = as_int(parts[1]) if len(parts) > 1 else None
    if temp is None:
        return None
    return {
        "name": parts[0] if as_int(parts[0]) is None else "",
        "temp": temp,
        "util": as_int(parts[2]) if len(parts) > 2 else None,
        "power": as_float(parts[3]) if len(parts) > 3 else None,
        "clock": as_int(parts[4]) if len(parts) > 4 else None,
        "mem_used": as_int(parts[5]) if len(parts) > 5 else None,
        "mem_total": as_int(parts[6]) if len(parts) > 6 else None,
    }


def ssh_base() -> list[str]:
    return [
        tool("ssh", ["/usr/bin/ssh"]),
        "-T",
        "-i", VM_CFG["key"],
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=no",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        f"{VM_CFG['user']}@{VM_CFG['host']}",
    ]


def gpu_command() -> list[str]:
    return ssh_base() + [f"nvidia-smi --query-gpu={GPU_FIELDS} --format=csv,noheader,nounits -lms {GPU_LOOP_MS}"]


def stream_cleanup_script(image: str = "nvidia-smi.exe") -> str:
    """PowerShell that ends looping nvidia-smi processes (-l / -lms) left behind in the VM.

    Windows OpenSSH doesn't end the remote command when the connection drops, so every
    reconnect would otherwise leave another nvidia-smi looping with nobody reading it.
    One-shot queries (like the fan script's own) have no -l flag and are never touched.
    """
    return (
        "$ProgressPreference = 'SilentlyContinue'; "
        f"Get-CimInstance Win32_Process -Filter \"Name='{image}'\" | "
        "Where-Object { $_.CommandLine -match '--query-gpu=' -and $_.CommandLine -match ' -l(ms)? \\d' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
    )


def encoded_powershell(script: str) -> str:
    token = base64.b64encode(script.encode("utf-16-le")).decode()
    return f"powershell -NoProfile -NonInteractive -EncodedCommand {token}"


def cleanup_gpu_streams() -> tuple[bool, str]:
    """Returns (ran, note). ran is False if the VM couldn't be reached."""
    try:
        proc = subprocess.run(ssh_base() + [encoded_powershell(stream_cleanup_script())], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()[:200]
    ended = [line.strip() for line in proc.stdout.splitlines() if line.strip().isdigit()]
    return True, f"ended {len(ended)} leftover nvidia-smi stream(s) in the VM" if ended else ""


def run_gpu_stream(command: list[str]) -> tuple[int, str]:
    """Run one streaming session until it ends, goes quiet, or the VM stops. Returns (lines read, reason)."""
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except OSError as exc:
        return 0, str(exc)
    state = {"last": time.monotonic(), "lines": 0, "junk": ""}
    err_tail: list[str] = []

    def read_out() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            parsed = parse_gpu_csv(line)
            if parsed and parsed["temp"] is not None:
                state["last"] = time.monotonic()
                state["lines"] += 1
                LIVE.set_gpu(parsed)
            elif line.strip():
                state["junk"] = line.strip()[:200]

    def read_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if line.strip():
                err_tail.append(line.strip()[:200])
                del err_tail[:-3]

    out_thread = threading.Thread(target=read_out, daemon=True)
    err_thread = threading.Thread(target=read_err, daemon=True)
    out_thread.start()
    err_thread.start()
    reason = ""
    while proc.poll() is None:
        time.sleep(1)
        if time.monotonic() - state["last"] > GPU_SILENT:
            reason = f"no GPU reading for {GPU_SILENT}s"
            break
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    out_thread.join(2)
    err_thread.join(2)
    if not reason:
        reason = "; ".join(err_tail) or state["junk"] or f"ssh exited ({proc.returncode})"
    return state["lines"], reason


def gpu_stream_loop() -> None:
    """Keep one session open. While it fails (VM off, booting, sshd down) retry with backoff up to GPU_RETRY_MAX."""
    backoff = 2.0
    need_cleanup = True  # at startup, and after every stream that ran: its nvidia-smi may still be looping in the VM
    while True:
        if not (VM_CFG["host"] and VM_CFG["user"]):
            LIVE.set_gpu_error("VM_IP / VM_USER not found in the fan script or [vm] in the config")
            time.sleep(GPU_RETRY_MAX)
            continue
        if need_cleanup:
            ran, note = cleanup_gpu_streams()
            if ran:
                need_cleanup = False
                if note:
                    print(note, flush=True)
        lines, reason = run_gpu_stream(gpu_command())
        if lines:
            need_cleanup = True
        LIVE.set_gpu_error(f"GPU stream: {reason}")
        with BUFFER.lock:
            vm_off = BUFFER.last_vm == "stopped"
        if lines:
            backoff = 2.0
        elif vm_off:
            backoff = float(GPU_RETRY_MAX)  # the script says the VM is off; just check now and then
        else:
            backoff = min(float(GPU_RETRY_MAX), backoff * 2)
        time.sleep(backoff)


# ---------- feed: fans, inlet, exhaust (iDRAC Redfish) ----------

def parse_thermal_json(body: dict) -> dict:
    fans: list[dict] = []
    for item in body.get("Fans") or []:
        if not isinstance(item, dict):
            continue
        reading = item.get("Reading")
        units = str(item.get("ReadingUnits") or "RPM").upper()
        status = str((item.get("Status") or {}).get("State") or "Enabled")
        if status.lower() == "absent" or units != "RPM" or not isinstance(reading, (int, float)) or isinstance(reading, bool):
            continue
        name = str(item.get("Name") or item.get("FanName") or "")
        name = re.sub(r"^System Board\s+", "", name, flags=re.I) or f"Fan{len(fans) + 1}"
        fans.append({"name": name, "rpm": int(reading)})
    inlet = exhaust = None
    cpus: list[dict] = []
    for item in body.get("Temperatures") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("Name") or "")
        value = item.get("ReadingCelsius")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if re.search(r"inlet", name, re.I):
            inlet = round(value)
        elif re.search(r"exhaust", name, re.I):
            exhaust = round(value)
        else:
            match = re.match(r"CPU\s*(\d+)", name, re.I)
            if match:
                cpus.append({"name": f"CPU{match.group(1)}", "c": round(value)})
    return {"fans": fans, "inlet": inlet, "exhaust": exhaust, "idrac_cpus": cpus}


def fetch_thermal() -> tuple[dict | None, str]:
    url = f"https://{IDRAC_CFG['host']}/redfish/v1/Chassis/System.Embedded.1/Thermal"
    token = base64.b64encode(f"{IDRAC_CFG['user']}:{IDRAC_CFG['password']}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": "Basic " + token, "Accept": "application/json"})
    context = ssl.create_default_context()
    if not IDRAC_CFG["verify_tls"]:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # iDRAC ships a self-signed certificate
    try:
        with urllib.request.urlopen(request, timeout=REDFISH_TIMEOUT, context=context) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        hint = ": check user/password in [idrac]" if exc.code in (401, 403) else ""
        return None, f"Redfish HTTP {exc.code}{hint}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"Redfish: {getattr(exc, 'reason', None) or exc}"
    if not isinstance(body, dict):
        return None, "Redfish: unexpected reply"
    return parse_thermal_json(body), ""


class Redfish:
    """One request at a time: right after each logged fan change, then every REDFISH_SEC."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.due: list[float] = []
        self.wake = threading.Event()
        self.running = False

    def kick(self) -> None:
        if not self.running:
            return
        now = time.monotonic()
        with self.lock:
            self.due = sorted(set(self.due + [now + delay for delay in REDFISH_BURST]))
        self.wake.set()

    def loop(self) -> None:
        if not redfish_ready():
            LIVE.set_redfish(None, "Redfish off: set host and user under [idrac] in /etc/fanlaw/fanlaw.conf")
            return
        self.running = True
        last_request = -math.inf
        next_steady = time.monotonic()
        while True:
            with self.lock:
                target = min(self.due + [next_steady])
            target = max(target, last_request + REDFISH_GAP)
            delay = target - time.monotonic()
            if delay > 0:
                self.wake.wait(delay)
                self.wake.clear()
                continue
            last_request = time.monotonic()
            data, err = fetch_thermal()
            LIVE.set_redfish(data, err)
            with self.lock:
                self.due = [t for t in self.due if t > last_request]
            next_steady = time.monotonic() + REDFISH_SEC


REDFISH = Redfish()


# ---------- history sampler ----------

def history_loop() -> None:
    while True:
        time.sleep(HIST_SEC - (time.time() % HIST_SEC))
        view = LIVE.view()
        if view["cpu"] is None and view["gpu"] is None:
            continue
        rpms = [item["rpm"] for item in view["board"]["fans"] if isinstance(item.get("rpm"), int)]
        LIVE.add_history({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "gpu": view["gpu"],
            "cpu": view["cpu"],
            "fan": journal_fan(),
            "util": view["board"]["util"],
            "rpm": round(sum(rpms) / len(rpms)) if rpms else None,
        })


# ---------- journal followers ----------

def follow_file(path: Path) -> None:
    BUFFER.source = str(path)
    while True:
        if not path.exists():
            BUFFER.error = f"waiting for {path}"
            time.sleep(1)
            continue
        BUFFER.error = ""
        with path.open("r", errors="replace") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 250_000))
            if size > 250_000:
                handle.readline()
            for line in handle:
                BUFFER.add(line)
            BUFFER.following = True
            while True:
                line = handle.readline()
                if line:
                    BUFFER.add(line)
                    continue
                if not path.exists():
                    break
                time.sleep(0.5)


def follow_unit(unit: str) -> None:
    BUFFER.source = f"journalctl -u {unit}"
    preload = subprocess.run(
        ["journalctl", "-u", unit, "-o", "cat", "-n", "8000", "--no-pager"],
        check=False,
        capture_output=True,
        text=True,
    )
    if preload.returncode != 0:
        BUFFER.error = (preload.stderr or preload.stdout or "journalctl failed").strip()
    else:
        BUFFER.error = ""
        for line in preload.stdout.splitlines():
            BUFFER.add(line)
    BUFFER.following = True
    while True:
        proc = subprocess.Popen(
            ["journalctl", "-u", unit, "-o", "cat", "-n", "0", "-f", "--no-pager"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            BUFFER.error = ""
            BUFFER.add(line)
        err = ""
        if proc.stderr is not None:
            err = proc.stderr.read().strip()
        BUFFER.error = err or f"journalctl exited ({proc.wait()}); retrying"
        time.sleep(2)


PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Fanlaw</title>
<link id="favicon" rel="icon" href="data:,"/>
<style>
  :root {
    color-scheme: dark;
    --bg: #0a0b0d;
    --surface: #111317;
    --raise: #171a1f;
    --fg: #e7e8ea;
    --muted: #9aa1a8;
    --faint: #6a7179;
    --line: #22262c;
    --line2: #2e333a;
    --accent: #9bb0c4;
    --gpu: #5fa8f5;
    --cpu: #c38fe0;
    --fan: #eef0f2;
    --util: #59636e;
    --ok: #6fbf73;
    --warn: #e3a33f;
    --hot: #e5534b;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--fg); }
  body { font: 13px/1.4 ui-sans-serif, system-ui, "Segoe UI", sans-serif; }
  p { margin: 0; }
  .mono { font-family: ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace; font-variant-numeric: tabular-nums; }
  .faint { color: var(--faint); }
  .muted { color: var(--muted); }

  /* ---------- one-screen summary ---------- */
  .app {
    height: 100vh; min-height: 660px; max-width: 1920px; margin: 0 auto;
    display: grid; grid-template-rows: auto auto auto minmax(0, 1fr) auto auto;
    gap: 10px; padding: 10px 14px 6px;
  }
  .top { display: flex; align-items: center; gap: 14px; min-width: 0; }
  .brand { display: flex; align-items: baseline; gap: 10px; white-space: nowrap; min-width: 0; }
  .brand b { font-size: 12px; letter-spacing: 0.18em; color: var(--accent); font-weight: 600; }
  .brand span { color: var(--faint); font-size: 12px; overflow: hidden; text-overflow: ellipsis; }
  .pills { display: flex; gap: 6px; flex-wrap: wrap; flex: 1; min-width: 0; }
  .pill { border: 1px solid var(--line2); color: var(--muted); border-radius: 999px; padding: 1px 8px; font-size: 11px; white-space: nowrap; }
  .pill.good { color: var(--ok); border-color: #2c4a30; }
  .pill.bad { color: var(--warn); border-color: #5a4520; }
  .pill.crit { color: var(--hot); border-color: #5e2c28; }
  .top-r { display: flex; align-items: baseline; gap: 12px; white-space: nowrap; }
  .health { font-size: 11px; letter-spacing: 0.16em; font-weight: 600; padding: 3px 10px; border-radius: 6px; background: var(--raise); color: var(--muted); }
  .health.nominal { color: var(--ok); background: rgba(111, 191, 115, 0.1); }
  .health.watch { color: var(--warn); background: rgba(227, 163, 63, 0.12); }
  .health.alert { color: var(--hot); background: rgba(229, 83, 75, 0.14); }
  #clock { font-size: 18px; letter-spacing: -0.02em; }
  #age { color: var(--faint); font-size: 12px; min-width: 9.5em; }

  .banner { display: flex; align-items: center; gap: 16px; padding: 9px 14px; border: 1px solid var(--line); border-left: 3px solid var(--line2); border-radius: 10px; background: var(--surface); min-width: 0; }
  .banner.nominal { border-left-color: var(--ok); }
  .banner.watch { border-left-color: var(--warn); }
  .banner.alert { border-left-color: var(--hot); }
  .say { flex: 1; min-width: 0; }
  #decision { font-size: 15px; letter-spacing: -0.01em; }
  #plan { margin-top: 2px; color: var(--muted); font-size: 12px; }
  .reasons { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; max-width: 48%; }

  .tiles { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
  .tile { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px 0; overflow: hidden; min-width: 0; display: flex; flex-direction: column; }
  .t-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; min-width: 0; }
  .t-label { font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .chip { font-size: 10.5px; padding: 1px 7px; border-radius: 999px; background: var(--raise); color: var(--muted); white-space: nowrap; }
  .t-body { display: flex; align-items: center; gap: 10px; margin-top: 4px; }
  .gauge { width: 84px; height: 49px; flex: none; }
  .g-track { stroke: #1c2026; }
  .t-main { min-width: 0; }
  .big { font-size: 34px; line-height: 1; letter-spacing: -0.04em; font-weight: 500; white-space: nowrap; }
  .big small { font-size: 13px; color: var(--muted); margin-left: 3px; letter-spacing: 0; }
  .trend { font-size: 11px; color: var(--muted); margin-top: 5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .t-foot { display: flex; gap: 3px 12px; flex-wrap: wrap; margin-top: 7px; font-size: 11px; }
  .t-foot span { white-space: nowrap; }
  .t-foot i { font-style: normal; color: var(--faint); margin-right: 4px; }
  .t-foot b { font-weight: 500; }
  .spark { display: block; width: calc(100% + 24px); height: 26px; margin: 6px -12px 0; }

  .mid { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(330px, 1fr); gap: 10px; min-height: 0; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: 12px; padding: 10px 12px; min-width: 0; min-height: 0; display: flex; flex-direction: column; }
  .c-head { display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap; }
  .c-head h2 { margin: 0; font-size: 11px; letter-spacing: 0.1em; text-transform: uppercase; color: var(--muted); font-weight: 500; }
  .c-head .note { color: var(--faint); font-size: 11px; }
  .c-tools { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
  .plot { position: relative; flex: 1; min-height: 130px; touch-action: pan-y; }
  .plot > svg { position: absolute; inset: 0; width: 100%; height: 100%; display: block; }
  button.seg, button.lg { height: 24px; border-radius: 6px; border: 1px solid var(--line2); background: transparent; color: var(--muted); font: inherit; font-size: 11px; padding: 0 8px; cursor: pointer; }
  button.seg.on { color: var(--fg); border-color: var(--accent); background: rgba(155, 176, 196, 0.08); }
  button.lg { display: inline-flex; align-items: center; gap: 6px; }
  button.lg i { width: 10px; height: 3px; border-radius: 2px; display: inline-block; }
  button.lg.off { opacity: 0.4; }
  button:focus-visible { outline: 1px solid var(--accent); outline-offset: 1px; }
  .sep { width: 1px; height: 16px; background: var(--line2); }
  .tip { position: absolute; z-index: 3; pointer-events: none; background: rgba(13, 15, 18, 0.95); border: 1px solid var(--line2); border-radius: 8px; padding: 7px 9px; font-size: 11.5px; line-height: 1.55; white-space: nowrap; box-shadow: 0 8px 24px rgba(0, 0, 0, 0.45); }
  .tip i { font-style: normal; display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; }

  /* svg classes */
  .ax { fill: var(--faint); font: 10.5px ui-monospace, Menlo, Consolas, monospace; }
  .grid { stroke: #1b1f24; }
  .grid-v { stroke: #16191d; }
  .cap { stroke: #4a515a; stroke-dasharray: 3 4; }
  .th-goal { stroke: rgba(111, 191, 115, 0.45); stroke-dasharray: 4 4; }
  .th-limit { stroke: rgba(229, 83, 75, 0.55); stroke-dasharray: 4 4; }
  .cmd { stroke: rgba(238, 240, 242, 0.55); stroke-dasharray: 2 3; }
  .warn-mark { stroke: rgba(229, 83, 75, 0.7); }
  .hair { stroke: var(--accent); stroke-dasharray: 2 3; }

  .ladder { display: grid; grid-template-columns: auto auto minmax(0, 1fr) 3.2em; gap: 5px 10px; align-items: center; margin-top: 8px; font-size: 12px; }
  .l-k { color: var(--faint); font-size: 11px; letter-spacing: 0.06em; text-transform: uppercase; }
  .l-s { color: var(--muted); white-space: nowrap; }
  .l-v { text-align: right; color: var(--muted); }
  .l-v.win { color: var(--fg); font-weight: 600; }
  .lbar { position: relative; height: 8px; background: #1b1f25; border-radius: 99px; }
  .lbar i { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 99px; }
  .lbar b { position: absolute; top: -3px; bottom: -3px; width: 1px; background: #565e68; }
  #next { margin-top: 7px; color: var(--muted); font-size: 12px; }

  .low { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.05fr) minmax(0, 1.3fr); gap: 10px; height: 172px; }
  .low .card { overflow: hidden; }
  .fanrow { display: grid; grid-template-columns: 52px minmax(0, 1fr) 58px 40px; gap: 8px; align-items: center; font-size: 11.5px; padding: 2px 0; }
  .fbar { height: 6px; background: #1b1f25; border-radius: 99px; position: relative; overflow: hidden; }
  .fbar i { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 99px; background: var(--accent); }
  .r { text-align: right; }
  table.stats { width: 100%; border-collapse: collapse; font-size: 12px; }
  table.stats th { color: var(--faint); font-weight: 400; font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase; text-align: right; padding: 0 0 3px; }
  table.stats th:first-child, table.stats td:first-child { text-align: left; }
  table.stats td { text-align: right; padding: 2px 0; border-top: 1px solid var(--line); }
  .sw { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; }
  .zonebar { display: flex; height: 7px; border-radius: 99px; overflow: hidden; margin-top: 9px; background: #1b1f25; }
  .zlegend { display: flex; gap: 4px 10px; flex-wrap: wrap; font-size: 10.5px; color: var(--faint); margin-top: 5px; }
  .meta { display: flex; gap: 4px 14px; flex-wrap: wrap; font-size: 11px; color: var(--faint); margin-top: 6px; }
  .meta b { color: var(--fg); font-weight: 500; }
  .jrow { display: grid; grid-template-columns: 60px 8px minmax(0, 1fr); gap: 8px; align-items: center; font-size: 11.5px; padding: 3px 0; border-top: 1px solid var(--line); }
  .jrow.full { grid-template-columns: 64px 8px 4.2rem minmax(0, 1fr); padding: 6px 0; }
  .jrow .m { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .jrow.full .m { white-space: normal; overflow-wrap: anywhere; }
  .kdot { width: 7px; height: 7px; border-radius: 9px; background: var(--faint); display: block; }
  .kdot.warn { background: var(--hot); }
  .kdot.change { background: var(--accent); }
  .kdot.temp { background: var(--warn); }
  .kdot.status { background: #3d434b; }
  .more { justify-self: center; color: var(--faint); font-size: 11px; text-decoration: none; letter-spacing: 0.1em; text-transform: uppercase; padding: 2px 10px; }
  .more:hover { color: var(--fg); }

  /* ---------- details below the fold ---------- */
  .details { max-width: 1920px; margin: 0 auto; padding: 10px 14px 48px; display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr); gap: 10px; align-items: start; }
  .details .card { min-height: auto; }
  .stack { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
  .kv { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0 12px; font-size: 12px; }
  .kv > * { padding: 5px 0; border-top: 1px solid var(--line); }
  .kv > .v { text-align: right; overflow-wrap: anywhere; }
  .kv .h { grid-column: 1 / -1; color: var(--faint); font-size: 10.5px; letter-spacing: 0.1em; text-transform: uppercase; border-top: 0; padding-top: 10px; }
  .law-row { display: grid; grid-template-columns: 6.5rem 6rem minmax(0, 1fr); gap: 8px; font-size: 12px; padding: 4px 6px; border-radius: 6px; }
  .law-row.on { background: var(--raise); }
  .law-row .z { display: flex; align-items: center; gap: 6px; }
  .rule { color: var(--muted); font-size: 12px; margin-top: 10px; line-height: 1.55; }
  #jlist { max-height: 560px; overflow: auto; }
  .err { color: var(--warn); overflow-wrap: anywhere; }

  @media (max-height: 780px) and (min-width: 1181px) {
    .app { gap: 8px; padding-top: 8px; }
    .spark, #next, .c-head .note.opt { display: none; }
    .tile { padding-bottom: 9px; }
    .big { font-size: 30px; }
    .plot { min-height: 90px; }
    .ladder { gap: 3px 10px; margin-top: 6px; }
    .low { height: 158px; }
    .fanrow { padding: 1px 0; }
    table.stats td { padding: 1px 0; }
    .zonebar { margin-top: 6px; }
    .meta { margin-top: 4px; }
  }
  @media (max-width: 1180px) {
    .app { height: auto; min-height: 0; }
    .tiles { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .mid { grid-template-columns: minmax(0, 1fr); }
    .plot { min-height: 260px; }
    .low { grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); height: auto; }
    .low .card:last-child { grid-column: 1 / -1; }
    .details { grid-template-columns: minmax(0, 1fr); }
  }
  @media (max-width: 720px) {
    .app { padding: 10px 16px 6px; }
    .details { padding: 10px 16px 40px; }
    .top { flex-wrap: wrap; }
    .top-r { width: 100%; justify-content: space-between; }
    .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .tile:last-child { grid-column: 1 / -1; }
    .banner { flex-direction: column; align-items: flex-start; }
    .reasons { max-width: none; justify-content: flex-start; }
    .low { grid-template-columns: minmax(0, 1fr); }
    .gauge { width: 64px; height: 37px; }
    .big { font-size: 28px; }
  }
</style>
</head>
<body>
<div class="app">
  <header class="top">
    <div class="brand"><b>FANLAW</b><span id="host">__HOST_LINE__</span></div>
    <div class="pills" id="pills"></div>
    <div class="top-r">
      <span id="health" class="health">—</span>
      <span id="clock" class="mono">—</span>
      <span id="age"></span>
    </div>
  </header>

  <section class="banner" id="banner">
    <div class="say">
      <p id="decision">Waiting for the first reading.</p>
      <p id="plan"></p>
    </div>
    <div class="reasons" id="reasons"></div>
  </section>

  <section class="tiles" id="tiles"></section>

  <section class="mid">
    <article class="card">
      <div class="c-head">
        <h2>Trace</h2>
        <div class="c-tools">
          <div id="legend" class="c-tools"></div>
          <span class="sep"></span>
          <div id="ranges" class="c-tools"></div>
        </div>
      </div>
      <div class="plot" id="trace-box">
        <svg id="trace" role="img" aria-label="Temperature and fan command over time"></svg>
        <div id="trace-tip" class="tip mono" hidden></div>
      </div>
    </article>
    <article class="card">
      <div class="c-head">
        <h2>Fan law</h2>
        <span class="note opt">Highest vote wins</span>
      </div>
      <div class="plot" id="curve-box">
        <svg id="curve" role="img" aria-label="GPU and CPU fan curves with the current readings"></svg>
        <div id="curve-tip" class="tip mono" hidden></div>
      </div>
      <div class="ladder" id="ladder"></div>
      <p id="next"></p>
    </article>
  </section>

  <section class="low">
    <article class="card">
      <div class="c-head"><h2>Chassis fans</h2><span class="note" id="fan-note">RPM vs median</span></div>
      <div id="fans"></div>
    </article>
    <article class="card">
      <div class="c-head"><h2>Window</h2><span class="note" id="win-note"></span></div>
      <table class="stats mono">
        <thead><tr><th></th><th>min</th><th>avg</th><th>max</th><th>now</th></tr></thead>
        <tbody id="stats-body"></tbody>
      </table>
      <div class="zonebar" id="zonebar" title="Time the GPU spent in each zone"></div>
      <div class="zlegend" id="zlegend"></div>
      <div class="meta" id="win-meta"></div>
    </article>
    <article class="card">
      <div class="c-head"><h2>Journal</h2><span class="note">latest script lines</span></div>
      <div id="jmini"></div>
    </article>
  </section>

  <a class="more" href="#details">Details ↓</a>
</div>

<section class="details" id="details">
  <article class="card">
    <div class="c-head">
      <h2>Journal</h2>
      <div class="c-tools" id="jfilter">
        <button class="seg on" type="button" data-k="all">All</button>
        <button class="seg" type="button" data-k="change">Changes</button>
        <button class="seg" type="button" data-k="warn">Warnings</button>
        <button class="seg" type="button" data-k="status">Status</button>
        <button class="seg" type="button" data-k="temp">Temp</button>
      </div>
    </div>
    <p class="faint" style="font-size:12px">Lines the fan script printed. Steady ticks stay out of the journal.</p>
    <div id="jlist"></div>
  </article>
  <div class="stack">
    <article class="card">
      <div class="c-head"><h2>Law reference</h2><span class="note">current zone highlighted</span></div>
      <div id="lawref"></div>
      <p class="rule" id="rules"></p>
    </article>
    <article class="card">
      <div class="c-head"><h2>Sensors</h2><span class="note">coretemp · nvidia-smi · Redfish</span></div>
      <div class="kv mono" id="sensors"></div>
    </article>
    <article class="card">
      <div class="c-head"><h2>Diagnostics</h2></div>
      <div class="kv" id="diag"></div>
    </article>
  </div>
</section>

<script>
"use strict";
const NS = "http://www.w3.org/2000/svg";
const $ = (id) => document.getElementById(id);
const DASH = "—";
const ZONES = ["Floor", "Approach", "Goal", "Release", "Limit"];
const ZC = {Floor: "#4f7cac", Approach: "#4fa3a0", Goal: "#6fbf73", Release: "#e3a33f", Limit: "#e5534b"};
const COL = {gpu: "#5fa8f5", cpu: "#c38fe0", fan: "#eef0f2", util: "#59636e", accent: "#9bb0c4", ok: "#6fbf73", warn: "#e3a33f", hot: "#e5534b", muted: "#59636e"};
const RANGES = [[0.25, "15m"], [1, "1h"], [6, "6h"], [24, "24h"]];
const GAP_MS = 180000;

let L = {gpu: [35, 65, 70, 83], cpu: [35, 55, 75, 87], min_fan: 10, max_fan: 50, abs_max: 100, deadband: 3, step_down: 2, off_fan: 10, blind_fan: 25, poll: 5};
let last = null;
let hist = [];
let since = null;
let offline = "";
let fetchedAt = 0;
let ageBase = 0;
let ageAt = 0;
let hoverT = null;
let hoverTemp = null;
let traceGeo = null;
let curveGeo = null;
let journalKind = "all";

const store = {
  get(key, fallback) {
    try { const raw = localStorage.getItem("fanlaw." + key); return raw == null ? fallback : JSON.parse(raw); } catch (e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("fanlaw." + key, JSON.stringify(value)); } catch (e) { /* storage blocked */ }
  },
};
let hours = store.get("hours", 1);
if (!RANGES.some(([h]) => h === hours)) hours = 1;
const show = Object.assign({gpu: true, cpu: true, fan: true, util: true}, store.get("show", {}));

/* ---------- helpers ---------- */
function el(name, attrs, parent) {
  const node = document.createElementNS(NS, name);
  for (const key in attrs) if (attrs[key] != null) node.setAttribute(key, String(attrs[key]));
  if (parent) parent.appendChild(node);
  return node;
}
function label(parent, value, attrs) { const node = el("text", attrs, parent); node.textContent = value; return node; }
function h(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}
function fmt(v, unit) { return v == null || Number.isNaN(v) ? DASH : v + (unit || ""); }
function pad2(n) { return String(n).padStart(2, "0"); }
function hhmm(t) { const d = new Date(t); return pad2(d.getHours()) + ":" + pad2(d.getMinutes()); }
function hhmmss(ts) { const m = /T(\d{2}:\d{2}:\d{2})/.exec(ts || ""); return m ? m[1] : DASH; }
function ago(s) {
  if (s == null || Number.isNaN(s)) return "";
  if (s < 5) return "just now";
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}
function rgba(hex, a) { const n = parseInt(hex.slice(1), 16); return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")"; }
function median(list) { if (!list.length) return null; const s = list.slice().sort((a, b) => a - b); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; }
function signed(v, unit) { return v == null ? DASH : (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v) + (unit || ""); }
function zoneOf(kind, t) {
  if (t == null) return null;
  const b = L[kind];
  if (t <= b[0]) return "Floor";
  if (kind === "gpu" ? t <= b[1] : t < b[1]) return "Approach";
  if (t <= b[2]) return "Goal";
  if (t < b[3]) return "Release";
  return "Limit";
}
function lawAt(kind, t) {
  if (t == null || !last || !last.curve || !last.curve.length) return null;
  const c = last.curve;
  const tt = Math.max(c[0].t, Math.min(c[c.length - 1].t, Math.round(t)));
  const row = c.find((r) => r.t === tt);
  return row ? row[kind] : null;
}
function lowerBound(t) {
  let lo = 0, hi = hist.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (hist[m].t < t) lo = m + 1; else hi = m; }
  return lo;
}

/* ---------- history (incremental) ---------- */
function mergeHistory(rows, full) {
  const add = [];
  for (const r of rows || []) {
    if (!r || !r.ts) continue;
    const t = Date.parse(r.ts);
    if (Number.isNaN(t)) continue;
    add.push({ts: r.ts, t, gpu: r.gpu ?? null, cpu: r.cpu ?? null, fan: r.fan ?? null, util: r.util ?? null, rpm: r.rpm ?? null});
  }
  if (full) hist = add;
  else {
    const lastT = hist.length ? hist[hist.length - 1].t : -Infinity;
    for (const r of add) if (r.t > lastT) hist.push(r);
  }
  if (hist.length > 3600) compactHistory();
  since = hist.length ? hist[hist.length - 1].ts : null;
}
function compactHistory() {
  const end = hist[hist.length - 1].t;
  const out = [];
  let lastOld = null;
  for (const r of hist) {
    const age = end - r.t;
    if (age > 86400000) continue;
    if (age > 3600000) {
      if (lastOld != null && r.t - lastOld < 60000) continue;
      lastOld = r.t;
    }
    out.push(r);
  }
  hist = out;
}
function windowInfo() {
  const end = hist.length ? hist[hist.length - 1].t : Date.now();
  const start = end - hours * 3600000;
  return {start, end, rows: hist.slice(lowerBound(start))};
}
function slope(key, minutes) {
  if (hist.length < 3) return null;
  const end = hist[hist.length - 1].t;
  const from = end - (minutes || 5) * 60000;
  const pts = [];
  for (let i = hist.length - 1; i >= 0 && hist[i].t >= from; i--) if (hist[i][key] != null) pts.push(hist[i]);
  if (pts.length < 3 || (pts[0].t - pts[pts.length - 1].t) < 60000) return null;
  let sx = 0, sy = 0, sxx = 0, sxy = 0;
  for (const p of pts) { const x = (p.t - end) / 60000, y = p[key]; sx += x; sy += y; sxx += x * x; sxy += x * y; }
  const n = pts.length, d = n * sxx - sx * sx;
  return d ? (n * sxy - sx * sy) / d : null;
}
function trendText(s, unit) {
  if (s == null) return "trend " + DASH;
  if (Math.abs(s) < 0.1) return "→ steady";
  return (s > 0 ? "↑ " : "↓ ") + Math.abs(s).toFixed(1) + unit + "/min";
}
function etaText(kind, t, s) {
  if (t == null || s == null || Math.abs(s) < 0.1) return "";
  const b = L[kind];
  const mins = (m) => m < 1 ? "<1m" : Math.round(m) + "m";
  if (s > 0) {
    for (let i = 0; i < b.length; i++) if (b[i] > t) { const m = (b[i] - t) / s; return m <= 90 ? ZONES[i + 1] + " in " + mins(m) : ""; }
  } else {
    for (let i = b.length - 1; i >= 0; i--) if (b[i] < t) { const m = (t - b[i]) / -s; return m <= 90 ? ZONES[i] + " in " + mins(m) : ""; }
  }
  return "";
}

/* ---------- health ---------- */
function assessHealth(data, latest, board) {
  const crit = [], warn = [];
  if (offline) crit.push("Dashboard can't reach the server");
  if (!latest) warn.push("No reading yet");
  else {
    if (latest.gpu != null && latest.gpu >= L.gpu[3]) crit.push("GPU at limit " + latest.gpu + "°");
    else if (latest.gpu_zone === "Release") warn.push("GPU in Release " + latest.gpu + "°");
    if (latest.cpu != null && latest.cpu >= L.cpu[3]) crit.push("CPU at limit " + latest.cpu + "°");
    else if (latest.cpu_zone === "Release") warn.push("CPU in Release " + latest.cpu + "°");
    if (latest.gpu_blind) warn.push("GPU unreadable, blind hold");
    if (latest.fan != null && latest.fan > L.max_fan) warn.push("Fans " + latest.fan + "%, over the " + L.max_fan + "% cap");
    if (latest.vm === "unknown") warn.push("VM state unknown");
  }
  if (data && !data.live) warn.push(data.stale ? "Sensors stale, log only" : "Log only");
  if (data && data.error) warn.push(data.error);
  if (data && data.sensor_error) warn.push(data.sensor_error);
  const fans = (board && board.fans) || [];
  const med = median(fans.map((f) => f.rpm));
  for (const f of fans) {
    if (f.rpm <= 0) { crit.push(f.name + " stalled"); continue; }
    if (!med) continue;
    const dev = Math.abs(f.rpm - med) / med;
    if (dev >= 0.3) crit.push(f.name + " " + Math.round(dev * 100) + "% off median");
    else if (dev >= 0.15) warn.push(f.name + " " + Math.round(dev * 100) + "% off median");
  }
  const level = crit.length ? "alert" : warn.length ? "watch" : "nominal";
  return {level, crit, warn};
}

/* ---------- gauges, sparks, tiles ---------- */
function arc(cx, cy, r, a0, a1) {
  const x0 = cx + r * Math.cos(a0), y0 = cy - r * Math.sin(a0);
  const x1 = cx + r * Math.cos(a1), y1 = cy - r * Math.sin(a1);
  return "M" + x0.toFixed(2) + " " + y0.toFixed(2) + " A" + r + " " + r + " 0 0 1 " + x1.toFixed(2) + " " + y1.toFixed(2);
}
function gauge(svg, o) {
  svg.replaceChildren();
  const cx = 50, cy = 52, R = 45, r = 36;
  const ang = (v) => Math.PI * (1 - (Math.max(o.lo, Math.min(o.hi, v)) - o.lo) / (o.hi - o.lo));
  for (const [a, b, c] of o.bands) {
    if (b <= a) continue;
    el("path", {d: arc(cx, cy, R, ang(a), ang(b)), stroke: c, "stroke-width": 3, fill: "none", "stroke-opacity": 0.85}, svg);
  }
  el("path", {d: arc(cx, cy, r, Math.PI, 0), class: "g-track", "stroke-width": 8, fill: "none", "stroke-linecap": "round"}, svg);
  if (o.value != null) {
    const a = ang(o.value);
    if (a < Math.PI - 0.01) el("path", {d: arc(cx, cy, r, Math.PI, a), stroke: o.color, "stroke-width": 8, fill: "none", "stroke-linecap": "round"}, svg);
    el("circle", {cx: (cx + r * Math.cos(a)).toFixed(2), cy: (cy - r * Math.sin(a)).toFixed(2), r: 4.4, fill: "#0a0b0d", stroke: o.color, "stroke-width": 2.2}, svg);
  }
  if (o.marker != null) {
    const a = ang(o.marker);
    el("line", {x1: cx + (R + 3) * Math.cos(a), y1: cy - (R + 3) * Math.sin(a), x2: cx + (r - 6) * Math.cos(a), y2: cy - (r - 6) * Math.sin(a), stroke: "#eef0f2", "stroke-width": 1.6}, svg);
  }
}
function spark(svg, key, color) {
  svg.replaceChildren();
  const W = 200, H = 26;
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  if (hist.length < 2) return;
  const end = hist[hist.length - 1].t, from = end - 30 * 60000;
  const pts = hist.slice(lowerBound(from)).filter((r) => r[key] != null);
  if (pts.length < 2) return;
  let mn = Infinity, mx = -Infinity;
  for (const p of pts) { mn = Math.min(mn, p[key]); mx = Math.max(mx, p[key]); }
  if (mx - mn < 4) { const c = (mx + mn) / 2; mn = c - 2; mx = c + 2; }
  const X = (t) => (t - from) / (end - from) * W;
  const Y = (v) => H - 2 - (v - mn) / (mx - mn) * (H - 5);
  const d = pts.map((p, i) => (i ? "L" : "M") + X(p.t).toFixed(1) + " " + Y(p[key]).toFixed(1)).join("");
  el("path", {d: d + "L" + X(pts[pts.length - 1].t).toFixed(1) + " " + H + "L" + X(pts[0].t).toFixed(1) + " " + H + "Z", fill: color, "fill-opacity": 0.1, stroke: "none"}, svg);
  el("path", {d, fill: "none", stroke: color, "stroke-width": 1.4, "stroke-opacity": 0.9, "vector-effect": "non-scaling-stroke"}, svg);
}
const TILE_DEF = [["gpu", "GPU", "°C"], ["cpu", "CPU", "°C"], ["fan", "Fan command", "%"], ["air", "Airflow", "k rpm"], ["load", "GPU load", "%"]];
function buildTiles() {
  const root = $("tiles");
  for (const [key, name, unit] of TILE_DEF) {
    const tile = h("div", "tile");
    tile.id = "t-" + key;
    tile.innerHTML = '<div class="t-head"><span class="t-label" data-f="label"></span><span class="chip" data-f="chip">—</span></div>'
      + '<div class="t-body"><svg class="gauge" data-f="gauge" viewBox="0 0 100 58" aria-hidden="true"></svg>'
      + '<div class="t-main"><div class="big mono"><span data-f="v">—</span><small data-f="unit"></small></div><div class="trend mono" data-f="trend">&nbsp;</div></div></div>'
      + '<div class="t-foot" data-f="foot"></div><svg class="spark" data-f="spark" preserveAspectRatio="none" aria-hidden="true"></svg>';
    tile.querySelector('[data-f="label"]').textContent = name;
    tile.querySelector('[data-f="unit"]').textContent = unit;
    root.append(tile);
  }
}
function setTile(key, o) {
  const tile = $("t-" + key);
  const q = (f) => tile.querySelector('[data-f="' + f + '"]');
  if (o.label) q("label").textContent = o.label;
  const v = q("v");
  v.textContent = o.v;
  v.style.color = o.vColor || "";
  const chip = q("chip");
  chip.textContent = o.chip || DASH;
  chip.style.color = o.chipColor || "";
  chip.style.background = o.chipColor ? rgba(o.chipColor, 0.14) : "";
  q("trend").textContent = o.trend || " ";
  const foot = q("foot");
  foot.replaceChildren();
  for (const [a, b, c] of o.foot) {
    const span = h("span");
    const val = h("b", "mono", b);
    if (c) val.style.color = c;
    span.append(h("i", null, a), val);
    foot.append(span);
  }
  gauge(q("gauge"), o.gauge);
  spark(q("spark"), o.spark[0], o.spark[1]);
}
function zoneBands(kind) {
  const b = L[kind];
  return [[30, b[0], ZC.Floor], [b[0], b[1], ZC.Approach], [b[1], b[2], ZC.Goal], [b[2], b[3], ZC.Release], [b[3], 95, ZC.Limit]];
}
function joinBits(...bits) { return bits.filter(Boolean).join(" · "); }
function headroom(kind, t, idx) {
  if (t == null) return [DASH, null];
  const at = L[kind][idx];
  if (t === at) return ["reached", idx === 3 ? COL.hot : COL.warn];
  if (t > at) return ["+" + (t - at) + "° over", idx === 3 ? COL.hot : COL.warn];
  return [(at - t) + "° away", null];
}
function renderTiles(latest, board) {
  const vmOff = latest && latest.vm === "stopped";
  const g = latest ? latest.gpu : null, gz = latest ? latest.gpu_zone : null;
  const gs = slope("gpu", 5);
  const gName = board.gpu_name ? board.gpu_name.replace(/^NVIDIA\s+/i, "").replace(/^GeForce\s+/i, "") : "";
  const gHead = headroom("gpu", g, 2), gLim = headroom("gpu", g, 3);
  setTile("gpu", {
    label: gName ? "GPU · " + gName : "GPU",
    v: fmt(g), vColor: gz === "Limit" ? ZC.Limit : "",
    chip: vmOff ? "VM off" : latest && latest.gpu_blind ? "Blind" : gz,
    chipColor: vmOff ? COL.muted : latest && latest.gpu_blind ? COL.hot : gz ? ZC[gz] : null,
    trend: joinBits(trendText(gs, "°"), etaText("gpu", g, gs)),
    foot: [["law", fmt(latest && latest.gpu_law, "%")], ["cap", gHead[0], gHead[1]], ["limit", gLim[0], gLim[1]]],
    gauge: {lo: 30, hi: 95, bands: zoneBands("gpu"), value: g, color: gz ? ZC[gz] : COL.muted},
    spark: ["gpu", COL.gpu],
  });

  const c = latest ? latest.cpu : null, cz = latest ? latest.cpu_zone : null;
  const cs = slope("cpu", 5);
  const cpus = board.cpus || [];
  const cHead = headroom("cpu", c, 2);
  const cFoot = cpus.slice(0, 2).map((item) => [item.name, item.c + "°"]);
  cFoot.push(["law", fmt(latest && latest.cpu_law, "%")], ["cap", cHead[0], cHead[1]]);
  setTile("cpu", {
    label: cpus.length > 1 ? "CPU · hottest of " + cpus.length : "CPU",
    v: fmt(c), vColor: cz === "Limit" ? ZC.Limit : "",
    chip: cz, chipColor: cz ? ZC[cz] : null,
    trend: joinBits(trendText(cs, "°"), etaText("cpu", c, cs)),
    foot: cFoot,
    gauge: {lo: 30, hi: 95, bands: zoneBands("cpu"), value: c, color: cz ? ZC[cz] : COL.muted},
    spark: ["cpu", COL.cpu],
  });

  const f = latest ? latest.fan : null;
  const want = latest ? latest.want : null;
  const gap = f != null && want != null ? f - want : null;
  const phase = latest ? latest.phase : null;
  const phaseColor = {"On target": COL.ok, "Climbing": COL.warn, "Settling": COL.accent, "Deadband": COL.accent, "Grace hold": COL.warn, "Blind hold": COL.hot, "VM floor": ZC.Floor}[phase] || null;
  setTile("fan", {
    v: fmt(f), vColor: f != null && f > L.max_fan ? COL.hot : "",
    chip: phase, chipColor: phaseColor,
    trend: trendText(slope("fan", 10), "%"),
    foot: [["vote", fmt(want, "%")], ["gap", signed(gap)], ["by", latest ? latest.driver_label : DASH]],
    gauge: {lo: 0, hi: 100, bands: [[0, L.min_fan, "#3a4048"], [L.min_fan, L.max_fan, COL.accent], [L.max_fan, 100, COL.hot]], value: f, marker: want, color: f != null && f > L.max_fan ? COL.hot : COL.fan},
    spark: ["fan", COL.fan],
  });

  const fans = board.fans || [];
  const rpms = fans.map((x) => x.rpm);
  const avg = rpms.length ? rpms.reduce((a, b) => a + b, 0) / rpms.length : null;
  const med = median(rpms);
  let worst = null;
  for (const x of fans) { const dev = med ? (x.rpm - med) / med : 0; if (!worst || Math.abs(dev) > Math.abs(worst.dev)) worst = {name: x.name, dev}; }
  const ok = fans.filter((x) => med && Math.abs(x.rpm - med) / med < 0.15 && x.rpm > 0).length;
  const fanChip = !fans.length ? null : ok === fans.length ? ok + "/" + fans.length + " ok" : worst.name + " " + signed(Math.round(worst.dev * 100), "%");
  const fanChipColor = !fans.length ? null : ok === fans.length ? COL.ok : Math.abs(worst.dev) >= 0.3 ? COL.hot : COL.warn;
  const dT = board.inlet != null && board.exhaust != null ? board.exhaust - board.inlet : null;
  setTile("air", {
    v: avg == null ? DASH : (avg / 1000).toFixed(1),
    chip: fanChip, chipColor: fanChipColor,
    trend: board.inlet == null && board.exhaust == null ? "inlet / exhaust " + DASH : "in " + fmt(board.inlet, "°") + " → out " + fmt(board.exhaust, "°"),
    foot: [["ΔT", fmt(dT, "°")], ["min", rpms.length ? (Math.min(...rpms) / 1000).toFixed(1) + "k" : DASH], ["max", rpms.length ? (Math.max(...rpms) / 1000).toFixed(1) + "k" : DASH]],
    gauge: {lo: 0, hi: 15000, bands: [[0, 11000, ZC.Floor], [11000, 15000, COL.hot]], value: avg, color: avg != null && avg > 11000 ? COL.hot : COL.accent},
    spark: ["rpm", COL.accent],
  });

  const util = board.util;
  const resist = board.power && g != null && board.inlet != null && board.power > 5 ? ((g - board.inlet) / board.power).toFixed(2) : null;
  setTile("load", {
    v: vmOff ? DASH : fmt(util),
    chip: board.power == null || vmOff ? (vmOff ? "VM off" : null) : Math.round(board.power) + " W",
    chipColor: board.power == null || vmOff ? null : COL.gpu,
    trend: vmOff ? "GPU passthrough idle" : trendText(slope("util", 5), "%"),
    foot: [["clock", board.clock == null ? DASH : board.clock + " MHz"], ["VRAM", board.mem_used == null ? DASH : (board.mem_used / 1024).toFixed(1) + "/" + Math.round((board.mem_total || 0) / 1024) + " GB"], ["°C/W", resist == null ? DASH : resist]],
    gauge: {lo: 0, hi: 100, bands: [[0, 100, "#2f3a46"]], value: vmOff ? null : util, color: COL.gpu},
    spark: ["util", "#8a96a3"],
  });
}

/* ---------- header + banner ---------- */
function renderHeader(data, latest, hl) {
  const badge = $("health");
  badge.className = "health " + hl.level;
  badge.textContent = hl.level.toUpperCase();
  badge.title = hl.crit.concat(hl.warn).join("\n") || "All readings inside the law";
  $("clock").textContent = hhmmss(latest && latest.ts);
  const pills = $("pills");
  pills.replaceChildren();
  const pill = (text, cls) => pills.append(h("span", "pill" + (cls ? " " + cls : ""), text));
  if (offline) pill("Server unreachable", "crit");
  else if (!latest) pill("No data");
  else {
    pill(data.live ? "Live sensors" : "Log only", data.live ? "good" : "bad");
    pill(latest.vm === "stopped" ? "VM stopped" : latest.vm === "running" ? "VM running" : "VM unknown", latest.vm === "running" ? "" : "bad");
    if (data.source) pill(data.source.replace(/^journalctl -u /, "unit "));
  }
}
function decisionText(latest) {
  if (!latest) return "Waiting for the first reading.";
  const fan = latest.fan == null ? "no command yet" : latest.fan + "%";
  if (latest.driver === "floor") return "VM is off. Fans sit on the " + fmt(latest.want, "%") + " floor and are commanded to " + fan + ".";
  if (latest.want == null) return "Grace hold: no live vote. Fans stay at " + fan + " unless the CPU vote rises.";
  let s = latest.driver_label + " has the vote at " + latest.want + "%. Fans are commanded to " + fan;
  if (latest.phase) s += " (" + latest.phase.toLowerCase() + ")";
  return s + ".";
}
function renderBanner(latest, hl) {
  $("banner").className = "banner " + hl.level;
  $("decision").textContent = offline ? "Lost contact with the dashboard server. Showing the last good reading." : decisionText(latest);
  const bits = [];
  if (latest) {
    if (latest.plan) bits.push(latest.plan);
    if (latest.gpu != null) bits.push("GPU " + latest.gpu + "° " + (latest.gpu_zone || "").toLowerCase());
    if (latest.cpu != null) bits.push("CPU " + latest.cpu + "° " + (latest.cpu_zone || "").toLowerCase());
  }
  $("plan").textContent = bits.join(" · ");
  const root = $("reasons");
  root.replaceChildren();
  for (const text of hl.crit) root.append(h("span", "pill crit", text));
  for (const text of hl.warn) root.append(h("span", "pill bad", text));
  if (!hl.crit.length && !hl.warn.length) root.append(h("span", "pill good", "All readings inside the law"));
}

/* ---------- trace ---------- */
function buildLegend() {
  const root = $("legend");
  for (const [key, name] of [["gpu", "GPU"], ["cpu", "CPU"], ["fan", "Fan %"], ["util", "Util"]]) {
    const b = h("button", "lg" + (show[key] ? "" : " off"));
    b.type = "button";
    b.dataset.k = key;
    b.setAttribute("aria-pressed", String(show[key]));
    const sw = h("i");
    sw.style.background = COL[key];
    b.append(sw, document.createTextNode(name));
    root.append(b);
  }
  root.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    show[b.dataset.k] = !show[b.dataset.k];
    b.classList.toggle("off", !show[b.dataset.k]);
    b.setAttribute("aria-pressed", String(show[b.dataset.k]));
    store.set("show", show);
    drawTrace();
  });
  const ranges = $("ranges");
  for (const [hrs, name] of RANGES) {
    const b = h("button", "seg" + (hrs === hours ? " on" : ""), name);
    b.type = "button";
    b.dataset.h = String(hrs);
    b.title = "Key " + (RANGES.findIndex(([x]) => x === hrs) + 1);
    ranges.append(b);
  }
  ranges.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setRange(Number(b.dataset.h)); });
}
function setRange(hrs) {
  hours = hrs;
  store.set("hours", hrs);
  for (const b of document.querySelectorAll("#ranges button")) b.classList.toggle("on", Number(b.dataset.h) === hrs);
  drawTrace();
  renderStats();
}
function segments(rows, key) {
  const out = [];
  let cur = null, prev = null;
  for (const r of rows) {
    if (r[key] == null) { cur = null; prev = null; continue; }
    if (!cur || r.t - prev.t > GAP_MS) { cur = []; out.push(cur); }
    cur.push(r);
    prev = r;
  }
  return out;
}
function drawTrace() {
  const box = $("trace-box"), svg = $("trace");
  const W = box.clientWidth, H = box.clientHeight;
  if (W < 60 || H < 60) return;
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.replaceChildren();
  const m = {l: 34, r: 38, t: 8, b: 18};
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const {start, end, rows} = windowInfo();
  traceGeo = null;
  if (rows.length < 2) { label(svg, "The trace fills as samples arrive.", {x: m.l + 8, y: m.t + 20, class: "ax"}); return; }
  let mn = Infinity, mx = -Infinity;
  for (const r of rows) for (const k of ["gpu", "cpu"]) if (show[k] && r[k] != null) { mn = Math.min(mn, r[k]); mx = Math.max(mx, r[k]); }
  if (!Number.isFinite(mn)) { mn = 40; mx = 70; }
  let lo = Math.max(0, Math.floor((mn - 5) / 10) * 10), hi = Math.ceil((mx + 5) / 10) * 10;
  if (hi - lo < 30) hi = lo + 30;
  const X = (t) => m.l + (t - start) / (end - start) * pw;
  const Yt = (v) => m.t + (hi - v) / (hi - lo) * ph;
  const Yp = (p) => m.t + (100 - p) / 100 * ph;
  traceGeo = {m, pw, ph, start, end, rows, X, Yt, Yp, W};

  for (let v = lo; v <= hi; v += 10) {
    el("line", {x1: m.l, x2: m.l + pw, y1: Yt(v), y2: Yt(v), class: "grid"}, svg);
    label(svg, v + "°", {x: m.l - 5, y: Yt(v) + 3.5, class: "ax", "text-anchor": "end"});
  }
  for (const p of [0, 50, 100]) label(svg, p + "%", {x: m.l + pw + 5, y: Yp(p) + 3.5, class: "ax"});
  const spanMin = (end - start) / 60000;
  const step = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360].find((s) => spanMin / s <= Math.max(2, pw / 85)) || 360;
  for (let t = Math.ceil(start / (step * 60000)) * step * 60000; t <= end; t += step * 60000) {
    const x = X(t);
    el("line", {x1: x, x2: x, y1: m.t, y2: m.t + ph, class: "grid-v"}, svg);
    label(svg, hhmm(t), {x, y: H - 4, class: "ax", "text-anchor": "middle"});
  }
  if (show.gpu) {
    for (const [v, cls, name] of [[L.gpu[2], "th-goal", "GPU goal"], [L.gpu[3], "th-limit", "GPU limit"]]) {
      if (v <= lo || v >= hi) continue;
      el("line", {x1: m.l, x2: m.l + pw, y1: Yt(v), y2: Yt(v), class: cls}, svg);
      label(svg, name + " " + v + "°", {x: m.l + 5, y: Yt(v) - 4, class: "ax"});
    }
  }
  if (show.fan) {
    el("line", {x1: m.l, x2: m.l + pw, y1: Yp(L.max_fan), y2: Yp(L.max_fan), class: "cap"}, svg);
    label(svg, "cap " + L.max_fan + "%", {x: m.l + pw - 4, y: Yp(L.max_fan) - 4, class: "ax", "text-anchor": "end"});
  }
  if (show.util) {
    for (const seg of segments(rows, "util")) {
      if (seg.length < 2) continue;
      let d = "";
      seg.forEach((r, i) => { d += (i ? "L" : "M") + X(r.t).toFixed(1) + " " + Yp(r.util).toFixed(1); });
      d += "L" + X(seg[seg.length - 1].t).toFixed(1) + " " + Yp(0) + "L" + X(seg[0].t).toFixed(1) + " " + Yp(0) + "Z";
      el("path", {d, fill: COL.util, "fill-opacity": 0.13, stroke: "none"}, svg);
    }
  }
  const line = (key, Y, color, width, step) => {
    let d = "";
    for (const seg of segments(rows, key)) {
      seg.forEach((r, i) => {
        const x = X(r.t).toFixed(1), y = Y(r[key]).toFixed(1);
        d += i === 0 ? "M" + x + " " + y : step ? "H" + x + "V" + y : "L" + x + " " + y;
      });
    }
    if (d) el("path", {d, fill: "none", stroke: color, "stroke-width": width, "stroke-linejoin": "round"}, svg);
  };
  if (show.fan) line("fan", Yp, COL.fan, 1.5, true);
  if (show.cpu) line("cpu", Yt, COL.cpu, 1.6, false);
  if (show.gpu) line("gpu", Yt, COL.gpu, 2, false);
  for (const e of (last && last.events) || []) {
    if (e.kind !== "warn" || !e.ts) continue;
    const t = Date.parse(e.ts);
    if (t < start || t > end) continue;
    const x = X(t);
    el("line", {x1: x, x2: x, y1: m.t, y2: m.t + ph, class: "warn-mark"}, svg);
    el("path", {d: "M" + (x - 4) + " " + m.t + "L" + (x + 4) + " " + m.t + "L" + x + " " + (m.t + 6) + "Z", fill: COL.hot}, svg);
  }
  if (hoverT != null) {
    const r = nearestRow(rows, hoverT);
    if (r) {
      const x = X(r.t);
      el("line", {x1: x, x2: x, y1: m.t, y2: m.t + ph, class: "hair"}, svg);
      for (const [key, Y] of [["gpu", Yt], ["cpu", Yt], ["fan", Yp]]) {
        if (show[key] && r[key] != null) el("circle", {cx: x, cy: Y(r[key]), r: 3.5, fill: COL[key], stroke: "#0a0b0d", "stroke-width": 1.5}, svg);
      }
    }
  }
}
function nearestRow(rows, t) {
  if (!rows.length) return null;
  let lo = 0, hi = rows.length - 1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (rows[m].t < t) lo = m + 1; else hi = m; }
  if (lo > 0 && Math.abs(rows[lo - 1].t - t) < Math.abs(rows[lo].t - t)) lo -= 1;
  return rows[lo];
}
function tipLine(tip, color, text) {
  const line = h("div");
  if (color) { const sw = h("i"); sw.style.background = color; line.append(sw); }
  line.append(document.createTextNode(text));
  tip.append(line);
}
/* pointer position in the box's own (unzoomed) CSS pixels, so hover stays exact when the page is fit-scaled */
function localPoint(box, e) {
  const rect = box.getBoundingClientRect();
  const sx = rect.width ? box.clientWidth / rect.width : 1;
  const sy = rect.height ? box.clientHeight / rect.height : 1;
  return {x: (e.clientX - rect.left) * sx, y: (e.clientY - rect.top) * sy};
}
function placeTip(tip, box, x, y) {
  const bw = box.clientWidth, tw = tip.offsetWidth, th = tip.offsetHeight;
  let left = x + 14;
  if (left + tw > bw - 4) left = x - tw - 14;
  tip.style.left = Math.max(4, left) + "px";
  tip.style.top = Math.max(4, Math.min(box.clientHeight - th - 4, y - th / 2)) + "px";
}
function onTraceMove(e) {
  if (!traceGeo) return;
  const box = $("trace-box");
  const {x, y} = localPoint(box, e);
  const g = traceGeo;
  if (x < g.m.l || x > g.m.l + g.pw) { onTraceLeave(); return; }
  hoverT = g.start + (x - g.m.l) / g.pw * (g.end - g.start);
  drawTrace();
  const r = traceGeo && nearestRow(traceGeo.rows, hoverT);
  const tip = $("trace-tip");
  if (!r) { tip.hidden = true; return; }
  tip.replaceChildren();
  tipLine(tip, null, hhmmss(r.ts) + "  " + ago(Math.round((g.end - r.t) / 1000)));
  if (r.gpu != null) tipLine(tip, COL.gpu, "GPU  " + r.gpu + "°  law " + fmt(lawAt("gpu", r.gpu), "%") + "  " + (zoneOf("gpu", r.gpu) || ""));
  if (r.cpu != null) tipLine(tip, COL.cpu, "CPU  " + r.cpu + "°  law " + fmt(lawAt("cpu", r.cpu), "%") + "  " + (zoneOf("cpu", r.cpu) || ""));
  if (r.fan != null) tipLine(tip, COL.fan, "Fan  " + r.fan + "%");
  if (r.util != null) tipLine(tip, COL.util, "Util " + r.util + "%");
  if (r.rpm != null) tipLine(tip, COL.accent, "RPM  " + r.rpm.toLocaleString());
  tip.hidden = false;
  placeTip(tip, box, x, y);
}
function onTraceLeave() { hoverT = null; $("trace-tip").hidden = true; drawTrace(); }

/* ---------- curve ---------- */
function drawCurve() {
  const box = $("curve-box"), svg = $("curve");
  const W = box.clientWidth, H = box.clientHeight;
  if (W < 60 || H < 60 || !last || !last.curve) return;
  svg.setAttribute("viewBox", "0 0 " + W + " " + H);
  svg.replaceChildren();
  const m = {l: 34, r: 12, t: 8, b: 18};
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const X = (t) => m.l + (Math.max(30, Math.min(95, t)) - 30) / 65 * pw;
  const Y = (p) => m.t + (100 - p) / 100 * ph;
  curveGeo = {m, pw, X, Y};
  const latest = last.latest;
  for (const p of [0, 25, 50, 75, 100]) {
    el("line", {x1: m.l, x2: m.l + pw, y1: Y(p), y2: Y(p), class: p === L.max_fan ? "cap" : "grid"}, svg);
    label(svg, p + "%", {x: m.l - 5, y: Y(p) + 3.5, class: "ax", "text-anchor": "end"});
  }
  if (![0, 25, 50, 75, 100].includes(L.max_fan)) el("line", {x1: m.l, x2: m.l + pw, y1: Y(L.max_fan), y2: Y(L.max_fan), class: "cap"}, svg);
  for (let t = 40; t <= 90; t += 10) {
    el("line", {x1: X(t), x2: X(t), y1: m.t, y2: m.t + ph, class: "grid-v"}, svg);
    label(svg, t + "°", {x: X(t), y: H - 4, class: "ax", "text-anchor": "middle"});
  }
  const curvePath = (key) => last.curve.map((r, i) => (i ? "L" : "M") + X(r.t).toFixed(1) + " " + Y(r[key]).toFixed(1)).join("");
  el("path", {d: curvePath("cpu"), fill: "none", stroke: COL.cpu, "stroke-width": 1.6, "stroke-dasharray": "5 4", "stroke-opacity": 0.9}, svg);
  el("path", {d: curvePath("gpu"), fill: "none", stroke: COL.gpu, "stroke-width": 2.2}, svg);
  if (latest && latest.fan != null) {
    el("line", {x1: m.l, x2: m.l + pw, y1: Y(latest.fan), y2: Y(latest.fan), class: "cmd"}, svg);
    label(svg, "cmd " + latest.fan + "%", {x: m.l + pw - 4, y: Y(latest.fan) - 4, class: "ax", "text-anchor": "end"});
  }
  if (latest) {
    for (const key of ["cpu", "gpu"]) {
      const t = latest[key], law = latest[key + "_law"];
      if (t == null || law == null) continue;
      const x = X(t), y = Y(law);
      el("line", {x1: x, x2: x, y1: y, y2: m.t + ph, stroke: COL[key], "stroke-opacity": 0.45, "stroke-dasharray": "2 3"}, svg);
      el("circle", {cx: x, cy: y, r: latest.driver === key ? 6 : 4.5, fill: COL[key], stroke: "#0a0b0d", "stroke-width": 2}, svg);
      label(svg, key.toUpperCase() + " " + t + "°", {x: x + 9, y: y + (key === "cpu" ? 13 : -7), class: "ax", style: "fill:" + COL[key]});
    }
  }
  if (hoverTemp != null) {
    const x = X(hoverTemp);
    el("line", {x1: x, x2: x, y1: m.t, y2: m.t + ph, class: "hair"}, svg);
    for (const key of ["gpu", "cpu"]) {
      const law = lawAt(key, hoverTemp);
      if (law != null) el("circle", {cx: x, cy: Y(law), r: 3.5, fill: COL[key]}, svg);
    }
  }
}
function onCurveMove(e) {
  if (!curveGeo) return;
  const box = $("curve-box");
  const {x, y} = localPoint(box, e);
  const g = curveGeo;
  const temp = Math.round(30 + (x - g.m.l) / g.pw * 65);
  if (temp < 30 || temp > 95) { onCurveLeave(); return; }
  hoverTemp = temp;
  drawCurve();
  const tip = $("curve-tip");
  tip.replaceChildren();
  tipLine(tip, null, temp + "°C");
  tipLine(tip, COL.gpu, "GPU " + fmt(lawAt("gpu", temp), "%") + "  " + zoneOf("gpu", temp));
  tipLine(tip, COL.cpu, "CPU " + fmt(lawAt("cpu", temp), "%") + "  " + zoneOf("cpu", temp));
  tip.hidden = false;
  placeTip(tip, box, x, y);
}
function onCurveLeave() { hoverTemp = null; $("curve-tip").hidden = true; drawCurve(); }

function renderLadder(latest) {
  const root = $("ladder");
  root.replaceChildren();
  const win = latest ? latest.driver : null;
  const rows = [
    ["GPU", latest && latest.gpu != null ? latest.gpu + "°" + (win === "gpu" ? " · wins" : "") : DASH, latest ? latest.gpu_law : null, COL.gpu, win === "gpu"],
    ["CPU", latest && latest.cpu != null ? latest.cpu + "°" + (win === "cpu" ? " · wins" : "") : DASH, latest ? latest.cpu_law : null, COL.cpu, win === "cpu"],
    ["Vote", latest ? latest.driver_label : DASH, latest ? latest.want : null, COL.accent, true],
    ["Cmd", latest && latest.phase ? latest.phase : DASH, latest ? latest.fan : null, COL.fan, true],
  ];
  for (const [k, s, v, color, on] of rows) {
    root.append(h("span", "l-k", k), h("span", "l-s mono", s));
    const bar = h("div", "lbar");
    const fill = h("i");
    fill.style.width = (v == null ? 0 : Math.max(1.5, Math.min(100, v))) + "%";
    fill.style.background = color;
    if (!on) fill.style.opacity = "0.4";
    const cap = h("b");
    cap.style.left = L.max_fan + "%";
    bar.append(fill, cap);
    root.append(bar, h("span", "l-v mono" + (on ? " win" : ""), fmt(v, "%")));
  }
  $("next").textContent = latest ? "Next tick: " + latest.plan : "";
}

/* ---------- lower strip ---------- */
function renderFans(board) {
  const root = $("fans");
  root.replaceChildren();
  const fans = board.fans || [];
  if (!fans.length) { root.append(h("p", "faint", board.fan_error || "No tachometer rows yet.")); return; }
  const med = median(fans.map((f) => f.rpm));
  $("fan-note").textContent = "median " + (med ? Math.round(med).toLocaleString() : DASH) + " rpm";
  for (const f of fans) {
    const dev = med ? Math.round((f.rpm - med) / med * 100) : 0;
    const color = Math.abs(dev) >= 30 || f.rpm <= 0 ? COL.hot : Math.abs(dev) >= 15 ? COL.warn : f.rpm > 11000 ? COL.hot : COL.accent;
    const row = h("div", "fanrow");
    const bar = h("div", "fbar");
    const fill = h("i");
    fill.style.width = Math.max(2, Math.min(100, f.rpm / 15000 * 100)) + "%";
    fill.style.background = color;
    bar.append(fill);
    const devNode = h("span", "mono r", signed(dev, "%"));
    devNode.style.color = Math.abs(dev) >= 15 ? color : "var(--faint)";
    row.append(h("span", "faint", f.name), bar, h("span", "mono r", f.rpm.toLocaleString()), devNode);
    root.append(row);
  }
}
function weighted(rows, key) {
  let sum = 0, wsum = 0, mn = Infinity, mx = -Infinity;
  for (let i = 0; i < rows.length; i++) {
    const v = rows[i][key];
    if (v == null) continue;
    const next = rows[i + 1];
    const w = next ? Math.min(GAP_MS, Math.max(1000, next.t - rows[i].t)) : 5000;
    sum += v * w; wsum += w; mn = Math.min(mn, v); mx = Math.max(mx, v);
  }
  return wsum ? {min: mn, avg: Math.round(sum / wsum), max: mx} : null;
}
function renderStats() {
  const {rows} = windowInfo();
  const name = (RANGES.find(([x]) => x === hours) || [0, ""])[1];
  $("win-note").textContent = "last " + name + " · " + rows.length.toLocaleString() + " samples";
  const tb = $("stats-body");
  tb.replaceChildren();
  const latest = last && last.latest;
  for (const [key, title, unit] of [["gpu", "GPU", "°"], ["cpu", "CPU", "°"], ["fan", "Fan", "%"]]) {
    const s = weighted(rows, key);
    const tr = h("tr");
    const first = h("td");
    const sw = h("span", "sw");
    sw.style.background = COL[key];
    first.append(sw, document.createTextNode(title));
    tr.append(first);
    for (const v of s ? [s.min, s.avg, s.max] : [null, null, null]) tr.append(h("td", null, fmt(v, unit)));
    tr.append(h("td", null, fmt(latest ? latest[key] : null, unit)));
    tb.append(tr);
  }
  const zoneMs = {Floor: 0, Approach: 0, Goal: 0, Release: 0, Limit: 0};
  let capMs = 0, totalMs = 0, changes = 0, prevFan = null;
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i], next = rows[i + 1];
    const w = next ? Math.min(GAP_MS, Math.max(1000, next.t - r.t)) : 5000;
    if (r.gpu != null) { zoneMs[zoneOf("gpu", r.gpu)] += w; totalMs += w; }
    if (r.fan != null) {
      if (r.fan > L.max_fan) capMs += w;
      if (prevFan != null && r.fan !== prevFan) changes += 1;
      prevFan = r.fan;
    }
  }
  const bar = $("zonebar"), legend = $("zlegend");
  bar.replaceChildren();
  legend.replaceChildren();
  for (const z of ZONES) {
    const pct = totalMs ? zoneMs[z] / totalMs * 100 : 0;
    if (pct > 0) { const seg = h("i"); seg.style.width = pct + "%"; seg.style.background = ZC[z]; seg.title = z + " " + pct.toFixed(0) + "%"; bar.append(seg); }
    if (pct >= 0.5) {
      const item = h("span");
      const sw = h("span", "sw");
      sw.style.background = ZC[z];
      item.append(sw, document.createTextNode(z + " " + Math.round(pct) + "%"));
      legend.append(item);
    }
  }
  if (!totalMs) legend.append(h("span", null, "No GPU samples in this window"));
  const meta = $("win-meta");
  meta.replaceChildren();
  const fanMs = rows.length ? Math.max(1, rows[rows.length - 1].t - rows[0].t) : 1;
  const item = (k, v) => { const s = h("span", null, k + " "); s.append(h("b", "mono", v)); meta.append(s); };
  item("over cap", Math.round(capMs / fanMs * 100) + "%");
  item("fan changes", String(changes));
}
function jrow(e, full) {
  const row = h("div", "jrow" + (full ? " full" : ""));
  row.append(h("span", "mono faint", hhmmss(e.ts)));
  const dot = h("i", "kdot " + e.kind);
  dot.title = e.kind;
  row.append(dot);
  if (full) row.append(h("span", e.kind === "warn" ? "err" : "faint", e.kind));
  const msg = h("span", "m mono", e.msg);
  msg.title = e.msg;
  row.append(msg);
  return row;
}
function renderJournal(events) {
  const mini = $("jmini");
  mini.replaceChildren();
  if (!events.length) mini.append(h("p", "faint", "No script lines yet."));
  for (const e of events.slice(0, 6)) mini.append(jrow(e, false));
  const list = $("jlist");
  list.replaceChildren();
  const pick = events.filter((e) => journalKind === "all" || e.kind === journalKind);
  if (!pick.length) list.append(h("p", "faint", "Nothing of that kind in the last 40 lines."));
  for (const e of pick) list.append(jrow(e, true));
}

/* ---------- details ---------- */
function renderDetails(data, latest, board) {
  const ref = $("lawref");
  ref.replaceChildren();
  for (const kind of ["gpu", "cpu"]) {
    const b = L[kind];
    const curZone = latest ? latest[kind + "_zone"] : null;
    const lawOf = (t) => lawAt(kind, t);
    const spans = [
      ["Floor", "≤ " + b[0] + "°", fmt(lawOf(b[0]), "%")],
      ["Approach", b[0] + "–" + b[1] + "°", lawOf(b[0]) + "–" + lawOf(b[1]) + "%"],
      ["Goal", b[1] + "–" + b[2] + "°", lawOf(b[1]) + "–" + lawOf(b[2]) + "%"],
      ["Release", b[2] + "–" + b[3] + "°", lawOf(b[2]) + "–" + L.abs_max + "%"],
      ["Limit", "≥ " + b[3] + "°", L.abs_max + "%"],
    ];
    const head = h("div", "law-row");
    head.append(h("b", null, kind.toUpperCase()), h("span", "faint", "temp"), h("span", "faint", "fan law"));
    ref.append(head);
    for (const [z, range, law] of spans) {
      const row = h("div", "law-row" + (z === curZone ? " on" : ""));
      const zone = h("span", "z");
      const sw = h("span", "sw");
      sw.style.background = ZC[z];
      zone.append(sw, document.createTextNode(z));
      row.append(zone, h("span", "mono", range), h("span", "mono", law));
      ref.append(row);
    }
  }
  $("rules").textContent = "The highest vote wins. Climbs are immediate. Descent waits for a " + L.deadband + "% gap, then sheds a third of it (at least " + L.step_down + "%). VM off drops to the " + L.off_fan + "% floor unless the CPU wants more. If the GPU goes unreadable the script blind-holds at " + L.blind_fan + "% or more. " + L.max_fan + "% is the acoustic cap; the law only passes it in Release.";

  const sensors = $("sensors");
  sensors.replaceChildren();
  const kv = (root, k, v, cls) => { root.append(h("span", "k", k)); root.append(h("span", "v" + (cls ? " " + cls : ""), v)); };
  const head = (root, text) => root.append(h("span", "h", text));
  head(sensors, "GPU");
  kv(sensors, "model", board.gpu_name || DASH);
  kv(sensors, "temperature", fmt(latest && latest.gpu, " °C"));
  kv(sensors, "utilization", fmt(board.util, " %"));
  kv(sensors, "power", board.power == null ? DASH : board.power.toFixed(1) + " W");
  kv(sensors, "clock", fmt(board.clock, " MHz"));
  kv(sensors, "VRAM", board.mem_used == null ? DASH : board.mem_used.toLocaleString() + " / " + (board.mem_total || 0).toLocaleString() + " MiB");
  head(sensors, "Board");
  for (const item of board.cpus || []) kv(sensors, item.name + " host", item.c + " °C");
  for (const item of board.idrac_cpus || []) kv(sensors, item.name + " iDRAC", item.c + " °C");
  kv(sensors, "inlet", fmt(board.inlet, " °C"));
  kv(sensors, "exhaust", fmt(board.exhaust, " °C"));
  head(sensors, "Fans");
  for (const f of board.fans || []) kv(sensors, f.name, f.rpm.toLocaleString() + " rpm");
  if (!(board.fans || []).length) kv(sensors, "tachometers", board.fan_error || DASH, "err");

  const diag = $("diag");
  diag.replaceChildren();
  kv(diag, "source", data.source || DASH, "mono");
  kv(diag, "sensor feed", data.live ? "live, " + ago(data.age_s) : data.stale ? "stale, log only" : "log only");
  if (data.feeds) {
    const f = data.feeds, s = (v) => v == null ? DASH : v + "s";
    kv(diag, "feed ages", "CPU " + s(f.cpu) + " · GPU " + s(f.gpu) + " · fans " + s(f.redfish) + " · log " + s(f.log), "mono");
  }
  for (const note of data.setup || []) kv(diag, "setup", note);
  kv(diag, "last fetch", fetchedAt ? new Date(fetchedAt).toLocaleTimeString() : DASH, "mono");
  kv(diag, "history held", hist.length.toLocaleString() + " samples" + (hist.length ? ", " + Math.round((hist[hist.length - 1].t - hist[0].t) / 3600000 * 10) / 10 + " h" : ""));
  kv(diag, "decision line", latest ? latest.msg : DASH, "mono");
  if (data.error) kv(diag, "journal error", data.error, "err");
  if (data.sensor_error) kv(diag, "sensor error", data.sensor_error, "err");
  if (offline) kv(diag, "fetch error", offline, "err");
}

/* ---------- title + favicon ---------- */
function setTitle(latest, hl) {
  document.title = latest ? fmt(latest.gpu, "°") + " " + fmt(latest.cpu, "°") + " " + fmt(latest.fan, "%") + " · Fanlaw" : "Fanlaw";
  const color = {nominal: COL.ok, watch: COL.warn, alert: COL.hot}[hl.level];
  $("favicon").href = "data:image/svg+xml," + encodeURIComponent("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='6.5' fill='" + color + "'/></svg>");
}

/* ---------- main loop ---------- */
function render(data) {
  last = data;
  if (data.law) L = Object.assign({}, L, data.law);
  ageBase = data.age_s || 0;
  ageAt = Date.now();
  const latest = data.latest;
  const board = data.board || {};
  const hl = assessHealth(data, latest, board);
  renderHeader(data, latest, hl);
  renderBanner(latest, hl);
  renderTiles(latest, board);
  drawTrace();
  drawCurve();
  renderLadder(latest);
  renderStats();
  renderFans(board);
  renderJournal(data.events || []);
  renderDetails(data, latest, board);
  setTitle(latest, hl);
  updateAge();
}
function updateAge() {
  const node = $("age");
  if (!last) { node.textContent = offline ? "offline" : ""; return; }
  const age = ageBase + Math.round((Date.now() - ageAt) / 1000);
  node.textContent = offline ? "offline, retrying" : last.live ? "sensors " + ago(age) : last.stale ? "log only · " + ago(age) : "log";
}
let busy = false;
async function tick() {
  if (busy) return;
  busy = true;
  try {
    const url = since ? "/api/state?since=" + encodeURIComponent(since) : "/api/state";
    const res = await fetch(url, {cache: "no-store"});
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();
    mergeHistory(data.history, data.history_full !== false);
    offline = "";
    fetchedAt = Date.now();
    render(data);
  } catch (err) {
    offline = String(err && err.message || err);
    if (last) render(last);
    else {
      const hl = assessHealth(null, null, {});
      renderHeader({}, null, hl);
      renderBanner(null, hl);
      updateAge();
    }
  } finally {
    busy = false;
  }
}

buildTiles();
buildLegend();
$("trace-box").addEventListener("pointermove", onTraceMove);
$("trace-box").addEventListener("pointerleave", onTraceLeave);
$("curve-box").addEventListener("pointermove", onCurveMove);
$("curve-box").addEventListener("pointerleave", onCurveLeave);
$("jfilter").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  journalKind = b.dataset.k;
  for (const x of document.querySelectorAll("#jfilter button")) x.classList.toggle("on", x === b);
  if (last) renderJournal(last.events || []);
});
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || /input|textarea|select/i.test(e.target.tagName)) return;
  const i = ["1", "2", "3", "4"].indexOf(e.key);
  if (i >= 0) setRange(RANGES[i][0]);
});
if (typeof ResizeObserver !== "undefined") {
  const ro = new ResizeObserver(() => { drawTrace(); drawCurve(); });
  ro.observe($("trace-box"));
  ro.observe($("curve-box"));
} else {
  window.addEventListener("resize", () => { drawTrace(); drawCurve(); });
}
setInterval(updateAge, 1000);
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        return

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/api/state":
            # ?since=<iso ts> returns only newer history rows; the page keeps the rest.
            since = (parse_qs(parts.query).get("since") or [None])[0]
            if since is not None and not SINCE_RE.match(since):
                since = None
            self._send(200, json.dumps(BUFFER.snapshot(since)).encode(), "application/json")
            return
        if parts.path in ("/", "/index.html"):
            self._send(200, PAGE_BYTES, "text/html; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")


def demo_reading(at: float) -> tuple[int, int, int]:
    """Slow waves so the demo trace, trends and zone changes have something to show."""
    gpu = round(60 + 11 * math.sin(at / 1800) + 3 * math.sin(at / 170))
    cpu = round(56 + 7 * math.sin(at / 2400 + 1) + 2 * math.sin(at / 90))
    util = max(0, min(100, round(55 + 35 * math.sin(at / 1800) + 8 * math.sin(at / 40))))
    return gpu, cpu, util


def demo_step(current: int | None, want: int) -> int:
    """Same climb and descent rule the script uses, so the demo fan line looks real."""
    if current is None or want > current:
        return want
    gap = current - want
    if gap >= DEADBAND or (want <= OFF_FAN and current > want):
        return max(want, current - max(STEP_DOWN, gap // 3))
    return current


def demo_rpm(fan: int) -> int:
    return 2400 + fan * 115


def demo_loop() -> None:
    rows: list[dict] = []
    fan: int | None = None
    at = time.time() - 86400
    while at < time.time():
        gpu, cpu, util = demo_reading(at)
        fan = demo_step(fan, max(gpu_curve(gpu), cpu_curve(cpu)))
        rows.append({"ts": datetime.fromtimestamp(at).isoformat(timespec="seconds"), "gpu": gpu, "cpu": cpu, "fan": fan, "util": util, "rpm": demo_rpm(fan)})
        at += HIST_SEC if time.time() - at <= 3600 else 60
    with LIVE.lock:
        LIVE.history = compact_history(rows)
    logged: int | None = None
    last_tick = 0.0
    while True:
        now = time.time()
        gpu, cpu, util = demo_reading(now)
        if now - last_tick >= INTERVAL:  # the script itself only decides every INTERVAL seconds
            last_tick = now
            fan = demo_step(fan, max(gpu_curve(gpu), cpu_curve(cpu)))
            if fan != logged:
                stamp = datetime.now().strftime("%a %b %d %H:%M:%S CST %Y")
                driving = " driving" if cpu_curve(cpu) > gpu_curve(gpu) else ""
                BUFFER.add(f"{stamp}: GPU={gpu}C, CPU={cpu}C{driving} -> Fans={fan}%")
                logged = fan
        base = demo_rpm(fan or MIN_FAN)
        LIVE.set_cpu([{"name": "CPU1", "c": cpu}, {"name": "CPU2", "c": cpu - 4}], "")
        LIVE.set_gpu({"name": "NVIDIA GeForce RTX 3060", "temp": gpu, "util": util, "power": round(38 + util * 1.3, 1), "clock": 1100 + util * 8, "mem_used": 6144, "mem_total": 12288})
        LIVE.set_redfish({
            "fans": [{"name": f"Fan{i}", "rpm": round(base * (1 + (i - 3.5) * 0.004))} for i in range(1, 7)],
            "inlet": 23,
            "exhaust": 23 + round((fan or MIN_FAN) * 0.12 + cpu * 0.14),
            "idrac_cpus": [{"name": "CPU1", "c": cpu}, {"name": "CPU2", "c": cpu - 4}],
        }, "")
        time.sleep(CPU_SEC)


def start(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


PAGE_BYTES = b""


def server_model() -> str:
    try:
        name = Path("/sys/class/dmi/id/product_name").read_text().strip()
    except OSError:
        return "PowerEdge"
    return re.sub(r"^PowerEdge\s+", "", name) or "PowerEdge"


def render_page() -> bytes:
    bits = [server_model()]
    if VM_CFG["vmid"]:
        bits.append(f"VM {VM_CFG['vmid']}")
    if VM_CFG["host"]:
        bits.append(VM_CFG["host"])
    bits.append("read only, the script owns the fans")
    return PAGE.replace("__HOST_LINE__", html.escape(" · ".join(bits))).encode()


def main() -> None:
    global PERSIST, PAGE_BYTES
    parser = argparse.ArgumentParser(description="Read-only Fanlaw dashboard for gpu-fan-curve.sh")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--unit", help="systemd unit that already runs gpu-fan-curve.sh")
    source.add_argument("--file", type=Path, help="log file the script's stdout is appended to")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--script", type=Path, default=SCRIPT_PATH, help="fan script to read the curve and VM details from")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH, help="INI file with [idrac] (and optional [vm]) settings")
    parser.add_argument("--demo", action="store_true", help="serve a fake R730 so the layout can be looked at")
    args = parser.parse_args()
    if args.unit and not UNIT_RE.match(args.unit):
        parser.error("unit name has unexpected characters")
    if args.demo:
        PERSIST = False
        SETUP_NOTES.append("demo mode: fake sensors, history file untouched")
    else:
        load_settings(args.script, args.config)
        LIVE.load()
    PAGE_BYTES = render_page()
    if args.unit:
        start(follow_unit, args.unit)
    else:
        start(follow_file, args.file)
    if args.demo:
        start(demo_loop)
    else:
        start(cpu_loop)
        start(gpu_stream_loop)
        start(REDFISH.loop)

        def on_stop(signum, frame) -> None:
            # Don't leave this run's nvidia-smi looping in the VM after systemctl stop/restart.
            if VM_CFG["host"] and VM_CFG["user"]:
                ran, note = cleanup_gpu_streams()
                if ran:
                    print(f"stopping: {note or 'no GPU stream left in the VM'}", flush=True)
                else:
                    print(f"stopping: couldn't reach the VM to end the GPU stream ({note})", flush=True)
            sys.exit(0)

        signal.signal(signal.SIGTERM, on_stop)
    start(history_loop)
    server = ThreadingHTTPServer((args.listen, args.port), Handler)
    print(f"Fanlaw dashboard on http://{args.listen}:{args.port}  (read only)", flush=True)
    for note in SETUP_NOTES:
        print(f"  {note}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
