# gpumode

A tiny desktop widget for NVIDIA GPUs on Linux: live temperature / fan / power readout
and one button per **mode**. A mode is a power limit plus a fan policy, per GPU.

```
┌──────────────────────────────────────┐
│ RTX 3080 Ti   69°C   75%  229/250W   │
│ RTX 3080      60°C   55%  208/220W   │
│ [   silent   ] [█████  llm  █████]   │
└──────────────────────────────────────┘
```

Typical use: a `silent` mode (low power limit, stock fan behaviour) for desk work and an
`llm` mode (higher limit, aggressive fan curve) for inference or training. Any number of
modes, any number of GPUs.

- No Coolbits, no `xorg.conf` edits, no `nvidia-settings`: the daemon does not need a display at all.
- Widget tested on KDE Plasma / X11. On Wayland it runs, but compositors may ignore its saved position.
- Two small Python files, one INI config, two systemd units.
- The selected mode survives reboots.

## How it works

| piece | runs as | does |
|---|---|---|
| `gpumoded` | root, system service | sets power limits and fan speeds through NVML, runs the fan curve loop, listens on `/run/gpumode.sock` |
| `gpumode` | you, user service | the widget; reads sensors itself, asks the daemon to switch modes |

The widget never needs root. The daemon only accepts "switch to mode X" for modes that
exist in the root-owned config, so the socket is safe to leave open to local users.

## Requirements

- A recent NVIDIA proprietary driver (one whose NVML can set fan speed; `--probe` will tell)
- Python 3.8+, `pynvml`, PyQt5
- systemd

```sh
sudo apt install python3-pynvml python3-pyqt5      # Debian / Ubuntu
# other distros: pip install nvidia-ml-py PyQt5
```

## Install

```sh
git clone https://github.com/complexwave/kde-gpumode.git
cd kde-gpumode
sudo ./install.sh
```

This installs to `/usr/local/lib/gpumode`, writes a starter `/etc/gpumode.conf` for the
GPUs it finds (only if none exists), starts the daemon, and enables the widget for
graphical sessions. The widget appears in the top-right corner.

Update: `git pull && sudo ./install.sh`. Remove: `sudo ./install.sh uninstall`.

## Configure

See what your cards allow:

```sh
$ python3 /usr/local/lib/gpumode/gpumoded.py --probe
gpu0  NVIDIA GeForce RTX 3080 Ti
      plimit  min 100 W, default 400 W, max 450 W (now 250 W)
      fans    2, settable range 30-100 % (now 65 %)
      temp    65 C
gpu1  NVIDIA GeForce RTX 3080
      ...
```

Then edit `/etc/gpumode.conf`. Each section is a mode and becomes a button, in file order:

```ini
[general]
default = silent        ; mode on first start (later: last selected mode)
interval = 2            ; seconds between fan updates
hysteresis = 3          ; fans slow down only after temp drops this many C
panic_temp = 85         ; at/above this, fans go 100% whatever the curve says

[silent]
plimit = 150            ; watts, applies to every GPU
curve = auto            ; leave fans to the driver

[llm]
gpu0.plimit = 250       ; per-GPU override, N as in nvidia-smi
gpu1.plimit = 220
curve = 40:30 55:45 65:65 72:85 78:100
```

| key | value | meaning |
|---|---|---|
| `plimit` | watts or `default` | power limit; clamped to what the card allows. Omitted = card default |
| `curve` | `TEMP:PERCENT ...` or `auto` | fan curve, linear between points, flat outside them. Omitted = `auto` |
| `gpuN.plimit`, `gpuN.curve` | same | override for GPU `N` only |

Apply:

```sh
sudo systemctl reload gpumoded
```

A config with errors is rejected and the running one stays in effect; check first with
`python3 /usr/local/lib/gpumode/gpumoded.py --check`.

## Use

- Click a button to switch mode.
- The widget sits on the desktop, below normal windows.
- Drag it anywhere; the position is remembered.
- Right-click → Quit. Bring it back with `systemctl --user start gpumode`.
- From scripts or a keyboard shortcut: `gpumode set llm`, `gpumode get`.

## Safety

- If the daemon stops or crashes, fans are handed back to the driver's automatic control.
  systemd then restarts the daemon.
- `panic_temp` forces 100% fan regardless of the curve.
- Power limits are clamped to the range the card reports.
- A fan curve that is too lazy can still let a card run hot up to `panic_temp`. Pick sane
  points and watch temperatures the first time you load the GPUs in a new mode.

## Troubleshooting

| symptom | look at |
|---|---|
| widget says `gpumoded down` | `systemctl status gpumoded`, `journalctl -u gpumoded` |
| no widget after login | `systemctl --user status gpumode`; needs a session that reaches `graphical-session.target` (KDE Plasma, GNOME do) |
| `Insufficient Permissions` in the daemon log | daemon is not running as root |
| fan speed ignores low curve points | the card has a minimum (see `--probe`), values are clamped to it |

## License

MIT
