# gpumode

NVIDIA GPU mode switch for Linux desktops: root daemon (power limit + fan curve via NVML)
+ unprivileged PyQt5 widget (sensors + one button per mode). User docs: `README.md`.

## Layout

| file | role |
|---|---|
| `gpumoded.py` | root daemon. Parses config, applies mode, fan curve loop, unix socket server. Also `--probe`, `--default-config`, `--check [FILE]`, `--dry-run` |
| `gpumode.py` | widget (frameless, kept below normal windows, draggable) + CLI `get` / `set MODE` |
| `gpumoded.service` | system unit → `/etc/systemd/system/` |
| `gpumode.service` | user unit → `/etc/systemd/user/`, `WantedBy=graphical-session.target` |
| `install.sh` | root installer / `uninstall`; idempotent, never overwrites `/etc/gpumode.conf` |

Installed: `/usr/local/lib/gpumode/*.py`, `/usr/local/bin/gpumode` (wrapper),
`/etc/gpumode.conf`, state `/var/lib/gpumode/mode`, socket `/run/gpumode.sock` (0666).

**The services run the installed copies, not this checkout.** After editing code:
`sudo ./install.sh`. After editing only `/etc/gpumode.conf`: `sudo systemctl reload gpumoded`.

## Data flow

```
/etc/gpumode.conf ──parse──> Config.modes {name: [(plimit W|None, curve|None) per GPU]}
widget/CLI ──"set <mode>" | "get"──> /run/gpumode.sock ──> {"mode": .., "modes": [..]}
daemon loop (every [general] interval): Gpu.tick() → temp → hysteresis → interp(curve) → NVML set fan
widget: reads temp/fan/power itself via pynvml (no root), polls daemon "get" every 2 s
```

- One request per connection, plain text in, JSON out. Only names of configured modes are
  accepted; the socket carries no free-form values. Keep it that way (it is world-writable).
- SIGHUP = reload config; invalid config is rejected, old one stays.
- Mode list in the reply drives the widget buttons; order = section order in the config.

## Invariants (do not break)

- Daemon exit path (`finally` in `run()`) returns every fan to driver auto. Fans must never
  stay pinned at a manual speed without the curve loop running.
- NVML error while setting a fan → that GPU falls back to auto.
- `panic_temp` overrides the curve with 100%.
- Power limits are clamped to NVML-reported constraints; fan % to `nvmlDeviceGetMinMaxFanSpeed`.
- Unknown config keys are errors (catches typos like `plimt`, or `gpu2.*` on a 2-GPU box).

## Setting it up for a user (agent procedure)

1. **Deps**: `python3 -c 'import pynvml, PyQt5.QtWidgets'`. Missing → `apt install
   python3-pynvml python3-pyqt5` (or `pip install nvidia-ml-py PyQt5`).
2. **Probe**: `python3 gpumoded.py --probe` (no root needed). Gives per GPU: index, name,
   plimit min/default/max, fan count, settable fan range, current temp.
3. **Ask the user** — do not invent these:
   - which modes they want (names = button labels, no spaces) and which is the default
   - power limit per GPU per mode (must be within probed min..max; `default` = stock)
   - fan policy per mode: `auto`, or curve points `TEMP:PERCENT`. Offer a starting point
     (e.g. `40:30 55:45 65:65 72:85 78:100`) and let them adjust. Lowest percent should be
     ≥ the probed fan minimum; last point should reach 100 below `panic_temp`.
4. **Write the config**. `python3 gpumoded.py --default-config` prints a commented starter;
   edit it into the user's modes. Validate before installing:
   `python3 gpumoded.py --check /path/to/draft.conf`.
5. **Install** (needs root — ask the user to run it if you have no root shell):
   `sudo cp draft.conf /etc/gpumode.conf && sudo ./install.sh`
   The installer enables + starts `gpumoded.service`, runs
   `systemctl --global enable gpumode.service`, and starts the widget for `$SUDO_USER`.
   If it could not (no `$SUDO_USER`), run as the desktop user:
   `systemctl --user daemon-reload && systemctl --user start gpumode`.
6. **Verify**:
   - `systemctl is-active gpumoded` and `systemctl --user is-active gpumode`
   - `journalctl -u gpumoded -n 20` shows `mode X`, `gpuN plimit ..W`, `gpuN ..C -> fan ..%`
   - `gpumode get`; `nvidia-smi --query-gpu=index,power.limit,fan.speed,temperature.gpu --format=csv`
   - Switching mode changes the power limit immediately. If the GPUs are under load, ask
     before switching to a lower-power mode just to test.

## Testing without root / without touching the GPUs

```sh
export GPUMODE_CONF=/tmp/x/gpumode.conf GPUMODE_SOCK=/tmp/x/gm.sock GPUMODE_STATE=/tmp/x/state/mode
python3 gpumoded.py --dry-run &          # logs what it would set
python3 gpumode.py get; python3 gpumode.py set <mode>
kill -HUP %1                             # reload
QT_QPA_PLATFORM=offscreen python3 -c 'import gpumode'   # widget import check, headless
```

No test suite; the above plus a real switch on hardware is the check.

## Conventions

- Stdlib + pynvml + PyQt5 only. No extra deps, no build step.
- Config stays INI (`configparser`), one section per mode.
- Keep both files single-module and small; this is meant to be read in one sitting.
