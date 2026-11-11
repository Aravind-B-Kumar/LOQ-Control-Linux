#!/usr/bin/env python3
"""Legion Control backend.

Start it with ../start.sh, not directly. It runs as root, binds to 127.0.0.1
only, and exposes a small JSON API that the browser UI (ui.html) uses. Every
hardware access goes through Linux kernel interfaces in sysfs:

  * battery:   ideapad_laptop  .../conservation_mode   (or power_supply charge_types)
               power_supply    BAT*/charge_control_{start,end}_threshold (if present)
  * tunables:  /sys/class/firmware-attributes/lenovo*/attributes/*  (lenovo-wmi-other, kernel >= 6.17)
               /sys/bus/platform/drivers/legion/*/                  (LenovoLegionLinux, fallback)
  * profile:   /sys/firmware/acpi/platform_profile
  * fans:      /sys/class/hwmon/hwmon*/pwm*_auto_point*_*          (fan curve, LenovoLegionLinux)

Only the Python standard library is used.
"""
import argparse
import glob
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = "/etc/legion-control/config.json"
PLATFORM_PROFILE = "/sys/firmware/acpi/platform_profile"

# Optional background service: keeps the 90% limit and saved fan curves active
# while the app is closed and across reboots.
SERVICE_NAME = "legion-control.service"
SERVICE_UNIT = "/etc/systemd/system/" + SERVICE_NAME
SERVICE_DIR = "/usr/local/libexec/legion-control"
SERVICE_BACKEND = SERVICE_DIR + "/backend.py"
OLD_UNIT = "/etc/systemd/system/legion-charge-limiter.service"  # name used by older versions

# Software 90% limit (used only when the hardware has just an on/off conservation mode)
LIMIT_HIGH = 90  # turn conservation mode ON at/above this
LIMIT_LOW = 87   # turn it OFF again at/below this (hysteresis)

IDLE_TIMEOUT = 180  # seconds without UI contact before the backend exits
TICK = 15           # limiter / idle check interval (seconds)


# --------------------------------------------------------------------------- helpers
def rd(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def wr(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def ival(s):
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return None


def is_writable(path):
    try:
        return bool(os.stat(path).st_mode & 0o222)
    except OSError:
        return False


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------- battery
class Battery:
    def __init__(self):
        bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
        self.bat = bats[0] if bats else None
        self.ac = None
        for p in sorted(glob.glob("/sys/class/power_supply/*")):
            if rd(os.path.join(p, "type")) == "Mains":
                self.ac = p
                break
        cons = sorted(glob.glob("/sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode"))
        self.cons = cons[0] if cons else None
        self.types = None
        self.end = None
        self.start = None
        if self.bat:
            t = os.path.join(self.bat, "charge_types")
            if "Long_Life" in (rd(t) or ""):
                self.types = t
            e = os.path.join(self.bat, "charge_control_end_threshold")
            if os.path.exists(e):
                self.end = e
            s = os.path.join(self.bat, "charge_control_start_threshold")
            if os.path.exists(s):
                self.start = s

    def method(self):
        if self.end:
            return "threshold"
        if self.cons or self.types:
            return "conservation"
        return None

    def capacity(self):
        return ival(rd(os.path.join(self.bat, "capacity"))) if self.bat else None

    def cons_get(self):
        if self.cons:
            return rd(self.cons) == "1"
        if self.types:
            return "[Long_Life]" in (rd(self.types) or "")
        return None

    def cons_set(self, on):
        if self.cons:
            wr(self.cons, 1 if on else 0)
        elif self.types:
            wr(self.types, "Long_Life" if on else "Standard")
        else:
            raise RuntimeError("conservation mode is not available")

    def mode(self, cfg):
        m = self.method()
        if m == "threshold":
            end = ival(rd(self.end))
            if end is None or end >= 100:
                return "100"
            return "90" if end >= 85 else "80"
        if m == "conservation":
            if cfg.get("charge_mode") == "90":
                return "90"
            return "80" if self.cons_get() else "100"
        return None

    def _write_thresholds(self, start, end):
        cur_end = ival(rd(self.end)) or 100
        # keep start < end at every step
        if end < cur_end:
            if self.start:
                wr(self.start, start)
            wr(self.end, end)
        else:
            wr(self.end, end)
            if self.start:
                try:
                    wr(self.start, start)
                except OSError:
                    if end == 100:
                        wr(self.start, 95)
                    else:
                        raise

    def apply(self, mode, cfg):
        if mode not in ("100", "90", "80"):
            raise ValueError("mode must be 100, 90 or 80")
        m = self.method()
        if m is None:
            raise RuntimeError("no battery charge control interface found")
        cfg["charge_mode"] = mode
        save_config(cfg)
        if m == "threshold":
            end = int(mode)
            start = 0 if end == 100 else end - 5
            self._write_thresholds(start, end)
            return
        if mode == "100":
            self.cons_set(False)
        elif mode == "80":
            self.cons_set(True)
        else:
            cap = self.capacity()
            self.cons_set(cap is not None and cap >= LIMIT_HIGH)

    def tick(self, cfg):
        """Software 90% limiter (conservation-mode hardware only)."""
        if self.method() != "conservation" or cfg.get("charge_mode") != "90":
            return
        cap = self.capacity()
        if cap is None:
            return
        on = self.cons_get()
        if cap >= LIMIT_HIGH and not on:
            self.cons_set(True)
        elif cap <= LIMIT_LOW and on:
            self.cons_set(False)

    def state(self, cfg):
        return {
            "method": self.method(),
            "mode": self.mode(cfg),
            "capacity": self.capacity(),
            "status": rd(os.path.join(self.bat, "status")) if self.bat else None,
            "ac": (rd(os.path.join(self.ac, "online")) == "1") if self.ac else None,
            "conservation_on": self.cons_get(),
            "limiter_service": service_installed(),
            "limit_high": LIMIT_HIGH,
            "limit_low": LIMIT_LOW,
        }


# --------------------------------------------------------------------------- tunables
# key, label, unit, firmware-attribute names, LenovoLegionLinux attribute files.
# Firmware IDs in comments are the ones Vantage uses for the same settings.
TUNABLES = [
    ("cpu_temp", "CPU Temperature Limit", "°C",
     ["cpu_temp", "cpu_temp_limit"], []),                                   # 0x0104ff00
    ("tpp_ac", "Total Processing Power Target on AC (offset)", "W",
     ["gpu_nv_ac_offset"], []),                                             # 0x0204ff00
    ("pl2", "CPU/Platform Short Term Power Limit", "W",
     ["ppt_pl2_sppt"], ["cpu_shortterm_powerlimit"]),                       # 0x0101ff00
    ("pl1", "CPU/Platform Long Term Power Limit", "W",
     ["ppt_pl1_spl"], ["cpu_longterm_powerlimit"]),                         # 0x0102ff00
    ("pl3", "CPU/Platform Peak Power Limit", "W",
     ["ppt_pl3_fppt"], ["cpu_peak_powerlimit"]),                            # 0x0103ff00
    ("apu_sppt", "APU sPPT Power Limit", "W",
     ["ppt_pl1_apu_sppt", "apu_sppt"], ["cpu_apu_sppt_powerlimit"]),       # 0x0105ff00
    ("gpu2cpu", "GPU to CPU Dynamic Boost", "W",
     ["gpu_nv_cpu_boost"], []),                                             # 0x020bff00
    ("pl1_cl", "CPU Long Term Power Limit (Cross Loading)", "W",
     ["ppt_pl1_spl_cl", "ppt_cpu_cl"], ["cpu_cross_loading_powerlimit"]),   # 0x0106ff00
    ("cpu2gpu", "CPU to GPU Dynamic Boost", "W",
     ["gpu_nv_ppab"], ["gpu_ppab_powerlimit"]),                             # 0x0201ff00
    ("ctgp", "GPU cTGP", "W",
     ["gpu_nv_ctgp"], ["gpu_ctgp_powerlimit"]),                             # 0x0202ff00
    ("gpu_temp", "GPU Temperature Limit", "°C",
     ["gpu_temp", "gpu_temp_limit"], ["gpu_temperature_limit"]),            # 0x0203ff00
]

# Conservative ranges for the LenovoLegionLinux fallback, which exposes no min/max.
LEGION_RANGE = {"°C": (60, 100), "W": (5, 200)}


def find_legion_dir():
    """Device dir of the legion_laptop driver (LenovoLegionLinux or the loq-control fork)."""
    candidates = ["/sys/bus/platform/devices/legion"] + sorted(glob.glob("/sys/bus/platform/drivers/legion/*"))
    for d in candidates:
        if os.path.isdir(d) and any(os.path.exists(os.path.join(d, f))
                                    for f in ("fancurve", "powermode", "fan_fullspeed")):
            return d
    return None


class Tunables:
    def __init__(self, legion_dir):
        self.legion = legion_dir
        self.items = []
        self.by_key = {}
        fw = {}
        for dev in sorted(glob.glob("/sys/class/firmware-attributes/*")):
            name = os.path.basename(dev).lower()
            if "lenovo" not in name or "thinklmi" in name:
                continue  # never touch generic BIOS settings
            for a in sorted(glob.glob(os.path.join(dev, "attributes", "*"))):
                if not os.path.isfile(os.path.join(a, "current_value")):
                    continue
                t = rd(os.path.join(a, "type"))
                if t and t != "integer":
                    continue
                fw.setdefault(os.path.basename(a), a)

        used = set()
        for key, label, unit, fw_names, legion_names in TUNABLES:
            item = None
            for n in fw_names:
                if n in fw:
                    item = self._fw_item(key, label, unit, fw[n])
                    used.add(n)
                    break
            if item is None and self.legion:
                for n in legion_names:
                    p = os.path.join(self.legion, n)
                    if os.path.isfile(p):
                        lo, hi = LEGION_RANGE[unit]
                        item = {"key": key, "label": label, "unit": unit,
                                "source": "legion_laptop", "name": n,
                                "path": p, "min": lo, "max": hi, "step": 1}
                        break
            if item:
                self._add(item)

        for n, a in fw.items():  # any other integer firmware tunable
            if n in used:
                continue
            label = rd(os.path.join(a, "display_name")) or n
            unit = "°C" if "temp" in n else ""
            self._add(self._fw_item("fw:" + n, label, unit, a, extra=True))

    def _fw_item(self, key, label, unit, attr_dir, extra=False):
        return {"key": key, "label": label, "unit": unit,
                "source": "firmware-attributes", "name": os.path.basename(attr_dir),
                "path": os.path.join(attr_dir, "current_value"),
                "min": ival(rd(os.path.join(attr_dir, "min_value"))),
                "max": ival(rd(os.path.join(attr_dir, "max_value"))),
                "step": ival(rd(os.path.join(attr_dir, "scalar_increment"))) or 1,
                "extra": extra}

    def _add(self, item):
        self.items.append(item)
        self.by_key[item["key"]] = item

    def state(self):
        out = []
        for it in self.items:
            d = {k: v for k, v in it.items() if k != "path"}
            d["value"] = ival(rd(it["path"]))
            out.append(d)
        return out

    def set(self, key, value):
        it = self.by_key.get(key)
        if it is None:
            raise KeyError("unknown tunable: %s" % key)
        v = ival(value)
        if v is None:
            raise ValueError("value must be an integer")
        if it["min"] is not None and v < it["min"]:
            raise ValueError("%s: %d is below minimum %d" % (it["label"], v, it["min"]))
        if it["max"] is not None and v > it["max"]:
            raise ValueError("%s: %d is above maximum %d" % (it["label"], v, it["max"]))
        wr(it["path"], v)


# --------------------------------------------------------------------------- profile
# The legacy /sys/firmware/acpi/platform_profile file rejects "custom" (EINVAL);
# it has to be set on the driver's own handler in /sys/class/platform-profile/.
PROFILE_CLASS = "/sys/class/platform-profile"
# "max-power" is listed by the driver but is not a real mode on LOQ firmware
# (selecting it just lands in Custom), so it is hidden from the UI.
HIDDEN_PROFILES = {"max-power"}


def profile_handler():
    """Return (dir, name) of the best per-driver platform-profile handler."""
    found = []
    for d in sorted(glob.glob(os.path.join(PROFILE_CLASS, "*"))):
        if os.path.isfile(os.path.join(d, "profile")):
            found.append((d, rd(os.path.join(d, "name")) or os.path.basename(d)))
    for d, name in found:
        if any(k in name.lower() for k in ("lenovo", "gamezone", "ideapad", "legion")):
            return d, name
    return found[0] if found else (None, None)


def _raw_profile():
    d, name = profile_handler()
    if d:
        return (rd(os.path.join(d, "profile")),
                (rd(os.path.join(d, "choices")) or "").split(), name)
    return (rd(PLATFORM_PROFILE),
            (rd(PLATFORM_PROFILE + "_choices") or "").split(), "legacy")


def profile_state():
    current, choices, handler = _raw_profile()
    visible = [c for c in choices if c not in HIDDEN_PROFILES]
    shown = current
    if current in HIDDEN_PROFILES and "custom" in visible:
        shown = "custom"  # firmware treats it as Custom
    return {"current": shown, "raw_current": current, "choices": visible,
            "all_choices": choices, "handler": handler}


def _write_profile(name):
    d, _ = profile_handler()
    targets = ([os.path.join(d, "profile")] if d else []) + [PLATFORM_PROFILE]
    errors = []
    for path in targets:
        if not os.path.exists(path):
            continue
        try:
            wr(path, name)
            return path
        except OSError as e:
            errors.append("%s: %s" % (path, e.strerror or e))
    raise RuntimeError("could not set power mode %s (%s)" % (name, "; ".join(errors) or "no interface"))


def set_custom_profile():
    try:
        return _write_profile("custom")
    except RuntimeError:
        # Last resort observed on LOQ firmware: max-power activates firmware Custom.
        if "max-power" in _raw_profile()[1]:
            return _write_profile("max-power")
        raise


def set_profile(name):
    if name not in profile_state()["choices"]:
        raise ValueError("unsupported profile: %s" % name)
    if name == "custom":
        set_custom_profile()
    else:
        _write_profile(name)


def ensure_custom_profile():
    st = profile_state()
    if "custom" in st["choices"] and st["current"] != "custom":
        set_custom_profile()
        time.sleep(0.3)


# --------------------------------------------------------------------------- fans
CURVE_MAX_RPM = 5800  # highest speed the legion_laptop fancurve file accepts
AUTO_POINT = re.compile(r"^pwm(\d+)_auto_point(\d+)_(\w+)$")
FAN_INPUT = re.compile(r"^fan(\d+)_input$")


class Fans:
    def __init__(self, legion_dir):
        self.curves = []
        self.allowed = {}  # id -> path; only these paths can be written
        self.speeds = []
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            hw = os.path.basename(d)
            name = rd(os.path.join(d, "name")) or hw
            try:
                files = sorted(os.listdir(d))
            except OSError:
                continue
            chans = {}
            for f in files:
                m = AUTO_POINT.match(f)
                if m:
                    ch, pt, attr = int(m.group(1)), int(m.group(2)), m.group(3)
                    chans.setdefault(ch, {}).setdefault(pt, {})[attr] = os.path.join(d, f)
                elif FAN_INPUT.match(f):
                    label = rd(os.path.join(d, f.replace("_input", "_label")))
                    self.speeds.append(("%s %s" % (name, label or f[:-6]), os.path.join(d, f)))
            if chans:
                self.curves.append({"id": hw, "name": name, "chans": chans})
                for pts in chans.values():
                    for attrs in pts.values():
                        for p in attrs.values():
                            self.allowed["%s/%s" % (hw, os.path.basename(p))] = p
        self.fullspeed = os.path.join(legion_dir, "fan_fullspeed") if legion_dir else None
        if self.fullspeed and not os.path.exists(self.fullspeed):
            self.fullspeed = None
        # Whole-curve file of the loq-control legion_laptop fork:
        # one line per point, "cpu_temp gpu_temp fan1_rpm fan2_rpm".
        self.fancurve = os.path.join(legion_dir, "fancurve") if legion_dir else None
        if self.fancurve and not os.path.exists(self.fancurve):
            self.fancurve = None

    # ---- legion "fancurve" file
    def read_point_curve(self):
        raw = rd(self.fancurve) if self.fancurve else None
        if not raw:
            return None
        pts = []
        for line in raw.splitlines():
            vals = [ival(x) for x in line.split()]
            if len(vals) != 4 or None in vals:
                return None
            pts.append(vals)
        return pts

    @staticmethod
    def validate_point_curve(points, size):
        if not isinstance(points, list) or len(points) != size:
            raise ValueError("the fan curve needs exactly %d points" % size)
        out, prev_cpu, prev_rpm = [], 0, 0
        for i, p in enumerate(points, 1):
            vals = [ival(x) for x in p] if isinstance(p, list) and len(p) == 4 else None
            if vals is None or None in vals:
                raise ValueError("point %d: needs 4 whole numbers" % i)
            cpu, gpu, r1, r2 = vals
            if not (0 <= cpu <= 127 and 0 <= gpu <= 127):
                raise ValueError("point %d: temperatures must be 0-127 °C" % i)
            if not (0 <= r1 <= CURVE_MAX_RPM and 0 <= r2 <= CURVE_MAX_RPM):
                raise ValueError("point %d: fan speed must be 0-%d rpm" % (i, CURVE_MAX_RPM))
            if i == 1 and (r1 or r2):
                raise ValueError("point 1 must be 0 rpm (the firmware requires it)")
            if cpu < prev_cpu:
                raise ValueError("point %d: CPU temperature must not go down" % i)
            if max(r1, r2) < prev_rpm:
                raise ValueError("point %d: fan speed must not go down" % i)
            prev_cpu, prev_rpm = cpu, max(r1, r2)
            out.append([cpu, gpu, r1 // 100 * 100, r2 // 100 * 100])  # firmware stores rpm/100
        return out

    def write_point_curve(self, points):
        cur = self.read_point_curve()
        if cur is None:
            raise RuntimeError("fan curve is not available")
        pts = self.validate_point_curve(points, len(cur))
        wr(self.fancurve, "".join("%d %d %d %d\n" % tuple(p) for p in pts))
        return pts

    def state(self):
        curves = []
        for c in self.curves:
            chans = []
            for ch in sorted(c["chans"]):
                pts = c["chans"][ch]
                attrs = sorted({a for p in pts.values() for a in p})
                points = []
                for pt in sorted(pts):
                    vals = {}
                    for a, path in pts[pt].items():
                        vals[a] = {"id": "%s/%s" % (c["id"], os.path.basename(path)),
                                   "value": ival(rd(path)), "writable": is_writable(path)}
                    points.append({"point": pt, "values": vals})
                chans.append({"channel": ch, "attrs": attrs, "points": points})
            curves.append({"id": c["id"], "name": c["name"], "channels": chans})
        return {"curves": curves,
                "speeds": [{"name": n, "rpm": ival(rd(p))} for n, p in self.speeds],
                "fullspeed": (rd(self.fullspeed) == "1") if self.fullspeed else None,
                "point_curve": self.read_point_curve(),
                "max_rpm": CURVE_MAX_RPM}

    def write_curve(self, writes):
        if not isinstance(writes, list):
            raise ValueError("writes must be a list")
        pending = []
        for w in writes:
            path = self.allowed.get(str(w.get("id")))
            v = ival(w.get("value"))
            if path is None or v is None:
                raise ValueError("invalid fan write: %r" % (w,))
            pending.append((w["id"], path, v))
        # Two passes: drivers may reject values that are temporarily out of order.
        for _ in range(2):
            failed = []
            for wid, path, v in pending:
                try:
                    wr(path, v)
                except OSError as e:
                    failed.append((wid, path, v, e))
            if not failed:
                return
            pending = [(a, b, c) for a, b, c, _e in failed]
        raise RuntimeError("; ".join("%s: %s" % (f[0], f[3].strerror or f[3]) for f in failed))

    def set_fullspeed(self, on):
        if not self.fullspeed:
            raise RuntimeError("max fan speed toggle not available")
        wr(self.fullspeed, 1 if on else 0)


# --------------------------------------------------------------------------- sensors
def sensors():
    out = []
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = rd(os.path.join(d, "name"))
        if name not in ("coretemp", "k10temp", "zenpower", "amdgpu", "legion_hwmon", "acpitz"):
            continue
        for f in sorted(glob.glob(os.path.join(d, "temp*_input"))):
            label = rd(f[:-6] + "_label") or os.path.basename(f)[:-6]
            if name == "coretemp" and not label.startswith("Package"):
                continue
            v = ival(rd(f))
            if v is not None:
                out.append({"name": "%s %s" % (name, label), "c": round(v / 1000.0, 1)})
    return out


# --------------------------------------------------------------------------- service
def _sha256(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def service_installed():
    return os.path.exists(SERVICE_UNIT)


def service_state():
    inst = service_installed()
    return {"installed": inst,
            "outdated": inst and _sha256(SERVICE_BACKEND) != _sha256(os.path.abspath(__file__))}


def _systemctl(*args):
    r = subprocess.run(["systemctl"] + list(args), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("systemctl %s failed: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))


def install_service():
    if not shutil.which("systemctl"):
        raise RuntimeError("systemd is not available")
    os.makedirs(SERVICE_DIR, exist_ok=True)
    shutil.copyfile(os.path.abspath(__file__), SERVICE_BACKEND)
    os.chmod(SERVICE_BACKEND, 0o755)
    with open(SERVICE_UNIT, "w") as f:
        f.write("[Unit]\n"
                "Description=Legion Control background service (90%% charge limit, fan curves)\n\n"
                "[Service]\n"
                "ExecStart=%s %s --daemon\n"
                "Restart=on-failure\n\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n" % (sys.executable, SERVICE_BACKEND))
    _systemctl("daemon-reload")
    _systemctl("enable", SERVICE_NAME)
    _systemctl("restart", SERVICE_NAME)


def remove_service():
    for name, unit in ((SERVICE_NAME, SERVICE_UNIT), ("legion-charge-limiter.service", OLD_UNIT)):
        if os.path.exists(unit):
            subprocess.run(["systemctl", "disable", "--now", name], capture_output=True, timeout=60)
            os.remove(unit)
    shutil.rmtree(SERVICE_DIR, ignore_errors=True)
    _systemctl("daemon-reload")


# --------------------------------------------------------------------------- app
class App:
    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = load_config()
        self.legion = find_legion_dir()
        self.battery = Battery()
        self.tunables = Tunables(self.legion)
        self.fans = Fans(self.legion)
        self.last_seen = time.time()
        self.server = None

    def touch(self):
        self.last_seen = time.time()

    def state(self):
        prof = profile_state()
        fans = self.fans.state()
        fans["saved_for_mode"] = prof["raw_current"] in self.cfg.get("fancurves", {})
        return {
            "kernel": platform.release(),
            "charge": self.battery.state(self.cfg),
            "service": service_state(),
            "profile": prof,
            "tunables": self.tunables.state(),
            "fans": fans,
            "sensors": sensors(),
            "backends": {
                "ideapad_conservation": bool(self.battery.cons or self.battery.types),
                "charge_thresholds": bool(self.battery.end),
                "firmware_attributes": any(t["source"] == "firmware-attributes"
                                           for t in self.tunables.items),
                "legion_laptop": bool(self.legion),
                "platform_profile": os.path.exists(PLATFORM_PROFILE),
            },
        }

    def post(self, path, body):
        if path == "/api/charge":
            self.battery.apply(str(body.get("mode")), self.cfg)
        elif path == "/api/profile":
            set_profile(str(body.get("profile")))
        elif path == "/api/tunable":
            ensure_custom_profile()
            self.tunables.set(str(body.get("key")), body.get("value"))
        elif path == "/api/fan":
            self.fans.write_curve(body.get("writes"))
        elif path == "/api/fanfull":
            self.fans.set_fullspeed(bool(body.get("on")))
        elif path == "/api/fancurve":
            pts = self.fans.write_point_curve(body.get("points"))
            mode = profile_state()["raw_current"] or "default"
            self.cfg.setdefault("fancurves", {})[mode] = pts
            save_config(self.cfg)
        elif path == "/api/fancurve/forget":
            self.cfg.get("fancurves", {}).pop(profile_state()["raw_current"] or "default", None)
            save_config(self.cfg)
        elif path == "/api/service":
            if body.get("install"):
                install_service()
            else:
                remove_service()
        elif path == "/api/heartbeat":
            pass
        elif path == "/api/quit":
            threading.Timer(0.3, self.stop).start()
            return {"ok": True}
        else:
            raise KeyError("unknown endpoint")
        return {"ok": True, "state": self.state()}

    def stop(self):
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def maintain(self):
        """Periodic work shared by the app and the background service."""
        try:
            self.battery.tick(self.cfg)
        except OSError as e:
            print("charge limiter: %s" % e, file=sys.stderr)
        # The firmware resets the fan curve on every power-mode change; put the saved one back.
        saved = self.cfg.get("fancurves", {}).get(profile_state()["raw_current"] or "default")
        if saved and self.fans.fancurve:
            try:
                if self.fans.read_point_curve() != saved:
                    self.fans.write_point_curve(saved)
            except (OSError, ValueError, RuntimeError) as e:
                print("fan curve: %s" % e, file=sys.stderr)

    def on_exit(self):
        """Never leave a software 90% limit unattended: fall back to firmware conservation."""
        with self.lock:
            if (self.cfg.get("charge_mode") == "90" and self.battery.method() == "conservation"
                    and not service_installed()):
                try:
                    self.battery.cons_set(True)
                    print("90% limit needs the app or the background service; "
                          "conservation mode switched on so the battery stops at about 80%.")
                except OSError as e:
                    print("could not enable conservation mode: %s" % e, file=sys.stderr)

    def background(self):
        while True:
            time.sleep(TICK)
            with self.lock:
                self.maintain()
            if time.time() - self.last_seen > IDLE_TIMEOUT:
                print("No UI activity for %ds, exiting." % IDLE_TIMEOUT)
                self.stop()
                return


def make_handler(app, token, port):
    hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class Handler(BaseHTTPRequestHandler):
        server_version = "LegionControl"

        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(data)

        def _guard(self, api):
            if self.headers.get("Host") not in hosts:
                self._send(403, {"error": "bad host"})
                return False
            if api and not secrets.compare_digest(self.headers.get("X-Token", ""), token):
                self._send(403, {"error": "bad token"})
                return False
            return True

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/":
                if self._guard(False):
                    with open(os.path.join(APP_DIR, "ui.html"), "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
            elif path == "/api/state":
                if self._guard(True):
                    app.touch()
                    with app.lock:
                        st = app.state()
                    self._send(200, st)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not self._guard(True):
                return
            n = ival(self.headers.get("Content-Length")) or 0
            if n > 65536:
                self._send(413, {"error": "too large"})
                return
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                self._send(400, {"error": "invalid JSON"})
                return
            app.touch()
            try:
                with app.lock:
                    res = app.post(path, body)
                self._send(200, res)
            except KeyError as e:
                self._send(404, {"error": str(e.args[0] if e.args else e)})
            except (ValueError, RuntimeError) as e:
                self._send(400, {"error": str(e)})
            except OSError as e:
                self._send(400, {"error": "kernel rejected the write: %s" % (e.strerror or e)})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Legion Control backend (start it with ../start.sh)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--probe", action="store_true", help="print detected hardware and exit")
    ap.add_argument("--daemon", "--limiter", dest="daemon", action="store_true",
                    help="background service: 90%% charge limit + saved fan curves")
    ap.add_argument("--no-root-check", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.probe:
        print(json.dumps(App().state(), indent=2, ensure_ascii=False))
        return 0
    if not args.daemon and (not args.port or sys.stdin.isatty()):
        print("This is the backend. Start the app with ./start.sh (without sudo).", file=sys.stderr)
        return 2
    if not args.no_root_check and os.geteuid() != 0:
        print("backend must run as root (start the app with ./start.sh)", file=sys.stderr)
        return 1

    app = App()
    if args.daemon:
        while True:
            app.cfg = load_config()  # the app may have changed it
            app.maintain()
            time.sleep(TICK)

    token = sys.stdin.readline().strip()
    if len(token) < 16:
        print("missing session token (start the app with ./start.sh)", file=sys.stderr)
        return 1
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app, token, args.port))
    srv.daemon_threads = True
    app.server = srv
    threading.Thread(target=app.background, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: app.stop())
    print("Legion Control backend on http://127.0.0.1:%d" % args.port)
    try:
        srv.serve_forever()
    finally:
        app.on_exit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
