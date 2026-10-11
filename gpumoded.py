#!/usr/bin/env python3
"""Root daemon: applies GPU power limit + fan policy per mode, switched over a unix socket.

  gpumoded.py                   run (reads /etc/gpumode.conf)
  gpumoded.py --probe           list GPUs with their power/fan ranges
  gpumoded.py --default-config  print a starter config for the GPUs found
  gpumoded.py --check [FILE]    validate a config and exit
  gpumoded.py --dry-run         run without touching the GPUs (logs only)
"""

import configparser
import json
import os
import signal
import socket
import sys
import pynvml as nv

DRY = "--dry-run" in sys.argv
CONF = os.environ.get("GPUMODE_CONF", "/etc/gpumode.conf")
SOCK = os.environ.get("GPUMODE_SOCK", "/run/gpumode.sock")
STATE = os.environ.get("GPUMODE_STATE", "/var/lib/gpumode/mode")

GENERAL = "general"
GENERAL_DEFAULTS = {"default": "", "interval": "2", "hysteresis": "3", "panic_temp": "85"}


class ConfigError(Exception):
    pass


def log(msg):
    print(msg, flush=True)


def parse_curve(s):
    """'40:30 55:45 78:100' -> [(40, 30), (55, 45), (78, 100)]; 'auto' -> None."""
    if s.strip().lower() == "auto":
        return None
    try:
        pts = [tuple(int(x) for x in p.split(":")) for p in s.split()]
    except ValueError:
        raise ConfigError(f"bad curve '{s}' (want 'TEMP:PERCENT TEMP:PERCENT ...')")
    if not pts or any(len(p) != 2 for p in pts):
        raise ConfigError(f"bad curve '{s}' (want 'TEMP:PERCENT TEMP:PERCENT ...')")
    if any(a[0] >= b[0] for a, b in zip(pts, pts[1:])):
        raise ConfigError(f"curve '{s}': temperatures must be increasing")
    if any(not 0 <= sp <= 100 for _, sp in pts):
        raise ConfigError(f"curve '{s}': fan percent must be 0..100")
    return pts


class Config:
    """modes: {name: [(plimit W | None, curve | None) per GPU]}, in button order."""

    def __init__(self, path, ngpus):
        cp = configparser.ConfigParser()
        try:
            with open(path) as f:
                cp.read_file(f)
        except (OSError, configparser.Error) as e:
            raise ConfigError(f"{path}: {e}")

        gen = dict(GENERAL_DEFAULTS)
        if cp.has_section(GENERAL):
            unknown = set(cp[GENERAL]) - set(GENERAL_DEFAULTS)
            if unknown:
                raise ConfigError(f"[{GENERAL}] unknown key(s): {', '.join(sorted(unknown))}")
            gen.update(cp[GENERAL])
        try:
            self.interval = float(gen["interval"])
            self.hysteresis = int(gen["hysteresis"])
            self.panic_temp = int(gen["panic_temp"])
        except ValueError as e:
            raise ConfigError(f"[{GENERAL}] {e}")

        allowed = {"plimit", "curve"}
        allowed |= {f"gpu{i}.{k}" for i in range(ngpus) for k in ("plimit", "curve")}
        self.modes = {}
        for name in cp.sections():
            if name == GENERAL:
                continue
            if len(name.split()) != 1:
                raise ConfigError(f"[{name}]: mode names cannot contain spaces")
            sec = cp[name]
            unknown = set(sec) - allowed
            if unknown:
                raise ConfigError(
                    f"[{name}] unknown key(s): {', '.join(sorted(unknown))} ({ngpus} GPU(s) present)"
                )
            per_gpu = []
            for i in range(ngpus):
                try:
                    pl = sec.get(f"gpu{i}.plimit", sec.get("plimit"))
                    pl = None if pl is None or pl.strip().lower() == "default" else int(pl)
                    curve = parse_curve(sec.get(f"gpu{i}.curve", sec.get("curve", "auto")))
                except (ValueError, ConfigError) as e:
                    raise ConfigError(f"[{name}] gpu{i}: {e}")
                per_gpu.append((pl, curve))
            self.modes[name] = per_gpu
        if not self.modes:
            raise ConfigError(f"{path}: no modes defined")
        self.default = gen["default"] or next(iter(self.modes))
        if self.default not in self.modes:
            raise ConfigError(f"[{GENERAL}] default = {self.default}: no such mode")


def interp(curve, t):
    if t <= curve[0][0]:
        return curve[0][1]
    for (t0, s0), (t1, s1) in zip(curve, curve[1:]):
        if t <= t1:
            return round(s0 + (s1 - s0) * (t - t0) / (t1 - t0))
    return curve[-1][1]


class Gpu:
    def __init__(self, idx):
        self.idx = idx
        self.h = nv.nvmlDeviceGetHandleByIndex(idx)
        self.nfans = nv.nvmlDeviceGetNumFans(self.h)
        try:
            self.fan_min, self.fan_max = nv.nvmlDeviceGetMinMaxFanSpeed(self.h)
        except nv.NVMLError:
            self.fan_min, self.fan_max = 0, 100
        self.curve = None
        self.held = None  # temp the current fan speed is based on
        self.speed = None

    def name(self):
        n = nv.nvmlDeviceGetName(self.h)
        return n.decode() if isinstance(n, bytes) else n

    def temp(self):
        return nv.nvmlDeviceGetTemperature(self.h, nv.NVML_TEMPERATURE_GPU)

    def plimit_range(self):
        """(min, default, max) in W."""
        lo, hi = nv.nvmlDeviceGetPowerManagementLimitConstraints(self.h)
        return lo // 1000, nv.nvmlDeviceGetPowerManagementDefaultLimit(self.h) // 1000, hi // 1000

    def apply(self, plimit, curve):
        lo, default, hi = self.plimit_range()
        watts = default if plimit is None else min(max(plimit, lo), hi)
        log(f"gpu{self.idx} plimit {watts}W")
        if not DRY:
            nv.nvmlDeviceSetPowerManagementLimit(self.h, watts * 1000)
        self.curve = curve
        self.held = self.speed = None
        if curve is None:
            self.fans_auto()

    def fans_auto(self):
        self.curve = self.held = self.speed = None
        log(f"gpu{self.idx} fans auto")
        if not DRY:
            for f in range(self.nfans):
                nv.nvmlDeviceSetDefaultFanSpeed_v2(self.h, f)

    def tick(self, cfg):
        if self.curve is None:
            return
        t = self.temp()
        if self.held is None or t > self.held or t < self.held - cfg.hysteresis:
            self.held = t
        speed = 100 if t >= cfg.panic_temp else interp(self.curve, self.held)
        speed = min(max(speed, self.fan_min), self.fan_max)
        if speed == self.speed:
            return
        self.speed = speed
        log(f"gpu{self.idx} {t}C -> fan {speed}%")
        if not DRY:
            for f in range(self.nfans):
                nv.nvmlDeviceSetFanSpeed_v2(self.h, f, speed)


def probe(gpus):
    for g in gpus:
        lo, default, hi = g.plimit_range()
        now = nv.nvmlDeviceGetPowerManagementLimit(g.h) // 1000
        print(
            f"gpu{g.idx}  {g.name()}\n"
            f"      plimit  min {lo} W, default {default} W, max {hi} W (now {now} W)\n"
            f"      fans    {g.nfans}, settable range {g.fan_min}-{g.fan_max} %"
            f" (now {nv.nvmlDeviceGetFanSpeed(g.h)} %)\n"
            f"      temp    {g.temp()} C"
        )


def default_config(gpus):
    out = [
        "# gpumode config. One section per mode = one button in the widget, in this order.",
        "# Apply changes with: sudo systemctl reload gpumoded",
        "#",
        "#   plimit = WATTS | default     power limit, all GPUs",
        "#   curve  = TEMP:PERCENT ... | auto   fan curve (linear between points), all GPUs",
        "#   gpuN.plimit / gpuN.curve     override for one GPU (N as in nvidia-smi)",
        "",
        f"[{GENERAL}]",
        "# mode used on first start; afterwards the last selected mode is restored",
        "default = quiet",
        "# seconds between fan curve updates",
        "interval = 2",
        "# fan slows down only after temp drops this many C",
        "hysteresis = 3",
        "# at or above this temp fans go to 100% regardless of the curve",
        "panic_temp = 85",
        "",
        "[quiet]",
        "curve = auto",
    ]
    for g in gpus:
        lo, default, _ = g.plimit_range()
        out.append(f"# {g.name()}: min {lo} W, default {default} W")
        out.append(f"gpu{g.idx}.plimit = {max(lo, round(default * 0.6 / 10) * 10)}")
    fmin = max(g.fan_min for g in gpus)
    out += [
        "",
        "[full]",
        "plimit = default",
        f"curve = 40:{max(30, fmin)} 55:45 65:65 72:85 78:100",
        "",
    ]
    return "\n".join(out)


def load_state(cfg):
    try:
        m = open(STATE).read().strip()
        return m if m in cfg.modes else cfg.default
    except OSError:
        return cfg.default


def save_state(mode):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w") as f:
            f.write(mode + "\n")
    except OSError as e:
        log(f"state save failed: {e}")


def apply_mode(gpus, cfg, mode):
    log(f"mode {mode}")
    for g, (plimit, curve) in zip(gpus, cfg.modes[mode]):
        g.apply(plimit, curve)


def serve(conn, gpus, cfg, mode):
    """One request per connection: 'get' | 'set <mode>'. Returns the new mode."""
    conn.settimeout(1)
    try:
        req = conn.recv(256).decode().split()
        if len(req) == 2 and req[0] == "set" and req[1] in cfg.modes and req[1] != mode:
            mode = req[1]
            apply_mode(gpus, cfg, mode)
            save_state(mode)
        conn.sendall(json.dumps({"mode": mode, "modes": list(cfg.modes)}).encode())
    except (OSError, UnicodeDecodeError):
        pass
    finally:
        conn.close()
    return mode


def run(gpus):
    cfg = Config(CONF, len(gpus))
    reload_req = []

    def stop(*_):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, lambda *_: reload_req.append(1))

    if os.path.exists(SOCK):
        os.unlink(SOCK)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o666)
    srv.listen(4)

    mode = load_state(cfg)
    try:
        apply_mode(gpus, cfg, mode)
        while True:
            srv.settimeout(cfg.interval)
            try:
                conn, _ = srv.accept()
                mode = serve(conn, gpus, cfg, mode)
            except socket.timeout:
                pass
            if reload_req:
                reload_req.clear()
                try:
                    cfg = Config(CONF, len(gpus))
                    mode = mode if mode in cfg.modes else cfg.default
                    log("config reloaded")
                    apply_mode(gpus, cfg, mode)
                except ConfigError as e:
                    log(f"reload failed, keeping old config: {e}")
            for g in gpus:
                try:
                    g.tick(cfg)
                except nv.NVMLError as e:
                    log(f"gpu{g.idx} fan control failed, back to auto: {e}")
                    g.fans_auto()
    finally:
        # never leave fans pinned at a manual speed without the curve loop
        for g in gpus:
            try:
                g.fans_auto()
            except nv.NVMLError as e:
                log(f"gpu{g.idx} fan restore failed: {e}")
        os.unlink(SOCK)


def main():
    nv.nvmlInit()
    gpus = [Gpu(i) for i in range(nv.nvmlDeviceGetCount())]
    try:
        if "--probe" in sys.argv:
            probe(gpus)
        elif "--default-config" in sys.argv:
            print(default_config(gpus), end="")
        elif "--check" in sys.argv:
            rest = sys.argv[sys.argv.index("--check") + 1 :]
            cfg = Config(rest[0] if rest else CONF, len(gpus))
            print("ok:", ", ".join(cfg.modes))
        else:
            run(gpus)
    except ConfigError as e:
        sys.exit(f"config error: {e}")


if __name__ == "__main__":
    main()
