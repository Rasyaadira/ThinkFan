# ThinkPad Fan Control

A compact desktop GUI (360×500) for controlling the ThinkPad fan, styled with
**Material Design 3** (baseline dark scheme). The whole interface is drawn by
hand on a single `tk.Canvas` — no ttk theme, no matplotlib, no dependency
outside the Python standard library.

```
GUI  →  sudo -n /usr/local/bin/fanctl set <level>  →  /proc/acpi/ibm/fan  →  EC
```

* tested only on Thinkpad X1 Carbon G7 

## Run

```bash
python3 thinkpad_fan_control.py
```

Or from the application menu: **ThinkPad Fan Control**
(`~/.local/share/applications/thinkpadfancontrol.desktop`).

Keyboard: `Esc` = back to Auto.

## Requirements

- `tk` (Tkinter): `sudo pacman -S --needed tk`
- Root helper `fanctl` plus `fan_control=1` — see the `fanapp` project in
  `~/.openclaw/workspace/fanapp` (`install.sh`, `docs/07-rebuild-from-scratch.md`).
  Without it the window still opens, but every command fails and the status dot
  in the top-right corner turns red.

## Interface

| Area | Content | Source |
|---|---|---|
| Top app bar | `ThinkPad Fan Control` + status dot (primary = healthy, error = failure) | helper status + `fan_control` |
| Card 1 | `CPU °C` | hwmon `coretemp` → `temp1_input` ÷ 1000 |
| Card 2 | `CHIPSET °C` | hwmon `pch_cannonlake` |
| Card 3 | `FAN RPM` | `/proc/acpi/ibm/fan` (`speed`), fallback hwmon `thinkpad` |
| Graph | two lines — CPU and chipset; Y axis 0–80 °C, X axis in minutes (1m…11m) | `deque(maxlen=330)` = 11 min @ 2 s |
| Fan level | status text (`Auto · firmware curve` / `Level N`), slider with `L0`–`L7` stops, value indicator while dragging | `fanctl set N` / `fanctl set auto` |
| Button | full-width pill, filled primary, label `Auto` | `fanctl set auto` |

Dragging the slider only previews the change; the command is sent **once**, when
the mouse button is released (previously it fired on every intermediate step).

## Material Design 3 tokens

| Token | Value | Applied to |
|---|---|---|
| Surface | `#141218` | window background |
| Surface container | `#211F26` | reserved (inset areas) |
| Surface container high | `#2B2930` | telemetry cards, graph card |
| Surface container highest | `#36343B` | slider handle halo (state layer) |
| On surface | `#E6E1E5` | primary text |
| On surface variant | `#CAC4D0` | labels, axis text, helper message |
| Outline variant | `#49454F` | grid lines, inactive track, stops |
| Primary | `#D0BCFF` | accent, CPU line, active track, status dot |
| On primary | `#381E72` | label on the filled button |
| Primary container | `#4F378B` | value indicator container |
| On primary container | `#EADDFF` | value indicator text |
| Tertiary | `#EFB8C8` | chipset line |
| Error | `#F2B8B5` | failure text and status dot |

Shapes: cards **16 px** (M3 *large*), filled button **pill = height/2** (18 px),
slider handle a vertical pill (5 × 26), stops 2 px.
Type scale (px): title large 20, headline medium 28 (values), body medium 13,
label small 11 (all caps, 0.5 px tracking), label large 14 (button).

## Deliberate deviations from Material Design 3

| M3 spec | What is implemented | Why |
|---|---|---|
| Roboto typeface | `Roboto` → `Noto Sans` → `DejaVu Sans`, first available | Roboto is not installed on this machine |
| Translucent state layers (alpha) | solid tonal tints (e.g. halo `#36343B`, button `#BDAAF5` when idle) | Tk cannot composite alpha |
| Elevation via shadow | tonal surface containers | Tk cannot draw blur/shadows |
| Rounded window corners | square window, rounded interior | compositor-dependent, not available in Tk |
| Value indicator always visible | shown while dragging | keeps the compact window uncluttered |

## Env

| Var | Default | Purpose |
|---|---|---|
| `FANAPP_REVERT_MIN` | `5` | minutes before a manual level reverts to `auto` (0 = disabled) |
| `FANAPP_POLL_MS` | `2000` | sensor polling interval |

## Design notes

- **Only `0`–`7` plus Auto in the GUI.** `full-speed` / `disengaged` (fan at
  100% indefinitely) stay CLI-only via `fanctl set full-speed`, so they cannot
  be enabled by accident.
- **No GPU temperature.** This machine has an Intel UHD 620 iGPU with no
  hwmon temperature sensor of its own; `thinkpad/temp2` is labelled "GPU" but
  always reads `0` (a placeholder, not a measurement). The second card shows
  the chipset (PCH) instead of inventing a number.
- **Zero values are discarded** — `_temp_from()` only accepts 1–150 °C.
- **Sensors are found through the `name` file**, never through the `hwmonN`
  number, which changes across boots.
- **The revert timer lives in memory.** This app is started manually per
  session; if it ever becomes an autostart service, port the `state.json`
  pattern from the web version (`fanapp/app.py`).

## Verification

```bash
python3 -c "import importlib.util as u; s=u.spec_from_file_location('t','thinkpad_fan_control.py'); m=u.module_from_spec(s); s.loader.exec_module(m); print(m.read_sensors())"
```

The command above exercises the sensor and control layers without an X server —
`tkinter` is imported inside `run_gui()`, not at module level.

Full system documentation (architecture, security design, pitfalls,
verification): `~/.openclaw/workspace/fanapp/docs/00-index.md`.
