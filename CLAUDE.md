# Baja fuel gauge — project context

Save this as `CLAUDE.md` in the project folder and Claude Code picks it up
automatically, or paste it as the first message of a session.

---

## Goal

Estimate how much fuel is in the Baja car's tank by measuring vibration with an
accelerometer, with no sensor on the tank itself.

## Hard constraints

- **Competition rules forbid mounting a sensor on the fuel tank.** The
  accelerometer goes on the frame near the tank.
- The tank is isolated from the frame on **4 rubber O-rings**.
- A sensor on the tank is allowed **on the bench**, as ground truth.

## Project folder

`~/Documents/baja_frequency/baja_logger` on Andrew's Mac. Python work happens in
a venv there (`python3 -m venv venv && source venv/bin/activate`); the only
dependency for capture is `pyserial`. Analysis needs numpy / scipy / pandas /
matplotlib.

---

## Hardware

### Board A — ESP32-C3 on a perf board (the main board)

| | |
|---|---|
| MPU6050 | I²C addr `0x68`, **SDA GPIO10, SCL GPIO2** |
| ADXL375 | I²C addr `0x1D`, wired to **SDA GPIO6, SCL GPIO4** |
| Serial | native USB-serial-JTAG, port looks like `/dev/cu.usbmodem1101` |
| Arduino board | ESP32C3 Dev Module, **USB CDC On Boot: Enabled** |

### Board B — ESP32-WROOM on a breadboard (second sensor)

| | |
|---|---|
| MPU6050 | I²C addr `0x68`, **SDA GPIO21, SCL GPIO22** |
| Serial | CP2102/CH340 UART bridge, `/dev/cu.usbserial-*` |
| Arduino board | ESP32 Dev Module |

### Sensor settings in firmware

- MPU6050 at **±8 g** (4096 LSB/g), **DLPF 260 Hz**, sampled at **1 kHz**
  from a deadline loop, ring-buffered so serial hiccups can't jitter timing.
- ADXL375 is **disabled** (`USE_ADXL 0`). The C3 has only one hardware I²C
  controller and the ADXL is on different pins from the MPU, so they can't share
  a bus as wired. To enable it, move its two wires onto GPIO10/GPIO2 alongside
  the MPU and set `USE_ADXL 1` — the addresses differ, so they coexist fine.
  It's only a clipping witness anyway: at 49 mg per count it's 200× coarser than
  the MPU6050 and can't see the fine structure.

### Wiring and toolchain gotchas that already cost hours

- **The C3 has one I²C controller.** One pin pair, both sensors or neither.
- **GPIO2 on the C3 is a strapping pin.** It must be high at boot; the I²C
  pull-up handles that. Suspect it first if boots become flaky.
- **On the classic ESP32 (WROOM), GPIO6–11 are the SPI flash.** Touching them
  crashes or boot-loops the chip. GPIO34–39 are input-only. `i2c_scan.ino`
  excludes both ranges per chip.
- **pyserial raises DTR and RTS when it opens a port.** On the C3 those lines
  are the reset / download-mode strap, so a default open can drop the board into
  its bootloader — the port enumerates and then says nothing forever.
  `capture.py` opens with both deasserted. Don't undo that.
- **USB CDC On Boot disabled** sends `Serial` to GPIO20/21 instead of USB.
  Same symptom: a port that opens and never speaks.
- **"Resource busy" on the port** means the Arduino Serial Monitor is open.
- Baud is real on the WROOM (921600) and ignored on the C3's native USB.

---

## Files

| File | What it is |
|---|---|
| `baja_logger.ino` | Firmware. One sketch for both boards — pins are chosen by `#if defined(CONFIG_IDF_TARGET_ESP32C3)`, so just pick the right board in Tools. Streams CSV over serial. Commands: `g` start, `s` stop, `r` toggle, any other line is recorded as a note. Prints a stats line every 5 s. |
| `capture.py` | Laptop-side capture. `--port`/`--port2` record both boards at once into two files. `--monitor` dumps raw board output for debugging. `--oz` / `--cups` log the fill level. Also exports `load_run()`. |
| `i2c_scan/i2c_scan.ino` | Diagnostic. Scans the configured pins, then sweeps other plausible pairs looking for the four addresses an MPU6050 or ADXL375 can have. Board-aware pin lists. |
| `tap_analysis.ipynb` | Analysis, sections 1–9. Edit it directly. |

---

## Data format

CSV with `#` comment lines carrying the configuration, so a file is
self-describing months later:

```
# captured_at=2026-09-20T23:37:36
# board=A
# oz=50
# trial=1
# mount=tape
# chip=esp32c3 sda=10 scl=2
# sample_hz=1000
# mpu6050 addr=0x68 range_g=8 lsb_per_g=4096.0 dlpf_hz=260
# columns: t_us,mpu_ax,mpu_ay,mpu_az,adxl_ax,adxl_ay,adxl_az
# stats sent=29954 dropped=0 late=0 mpu_clip=0 i2c_err=0
0,0,0,3701,0,0,0
```

- **Values are raw integer counts, not g.** Conversion uses `lsb_per_g` from
  the file's own header. Never hard-code 4096.
- Both loaders tolerate ESP32 ROM boot messages and UART garbage anywhere in
  the file, including above the real header. Don't "simplify" that back into
  stopping at the first non-`#` line.
- Filenames: `run_<oz|cups>NNN_trialN_<board>_<YYYYmmdd_HHMMSS>.csv`. Both
  boards in one capture share the timestamp, which is how the notebook pairs
  them — never by fill level and trial, which repeat.
- **One folder per physical setup.** Moving a board, the bucket, the erasers or
  the tap spot changes the spectrum as much as the water does.
- A run recorded with no `--oz`/`--cups` is left out of calibration and is the
  way to test the predictor blind.

### Data quality check, every time

The firmware's own `dropped`, `late`, `mpu_clip` and `i2c_err` counts are the
authority. All runs so far are 0 on all four. A few microseconds of timing
jitter is normal and costs nothing, because every sample carries its own
timestamp.

---

## The physics we're chasing

Tank + fuel on rubber mounts is a mass on a spring:

  f = (1/2π)·√(k/m),  m = m_tank + m_fuel

which rearranges to **1/f² = (4π²/k)(m_tank + m_fuel)** — a straight line in
fuel mass whose intercept-over-slope is the tank's own mass. That ratio isn't
fitted to anything measured, so comparing it to the tank on a scale is a real
test of whether an observed shift is a mass effect at all. The notebook does
this in section 5.

From the frame side you see the tank's resonance as an **anti-resonance (a
dip)**, not a peak: at its own frequency the tank's apparent mass goes huge and
pins the mount point. The dip frequency depends only on k and m, not on the
frame, which is what makes it measurable from a legal sensor position.

**Track frequencies, not amplitudes.** How hard you tap changes a peak's
height, not its position. Confirmed on the bench: a 30–75% harder set of taps
moved the peak 0.3 Hz.

---

## Bench results so far (20–21 Sept)

Setup: Home Depot bucket (838 g empty) on 3 erasers (22.5 g total, so
0.846 kg effective moving mass — a spring contributes a third of its mass) on a
table. Board A taped to the table beside the bucket, board B taped farther away
on the other side. Rubber-mallet taps on the table, ~1/s, 30 s per run.
Water logged in 50 fl oz pours (1 fl oz of water = 0.02957 kg).

**Repeatability is good.** Two empty runs five minutes apart: peak 57.5 and
57.2 Hz, log-spectra matching at r = 0.98.

**The peak does not track mass.** Up-sweep 0 → 350 oz (10.35 kg of water,
12× the bucket's own mass): board A's 57 Hz peak rose to 61.5 Hz. A mass on a
spring would have *dropped* ~72%, to about 16 Hz. Section 5 correctly reports
this as non-physical (negative slope → `k` and tank mass come out `nan`). A
rising R² does not rescue it; R² measures straightness, not physics.

**Nothing in 3–150 Hz moves down** along the mass curve, on either board.

**What does change with water:**
- board A's 57 Hz peak: +4 Hz; its ~78 Hz dip: +6 Hz (to ~84 Hz)
- the A/B ratio's ~80 Hz dip deepens, 0.26 → 0.04
- board A's 5–50 Hz motion relative to its 50–150 Hz motion: 0.41 → 0.27
  (this one is a real mass effect — at low frequency the bucket rides with the
  table, so the table near A gets heavier and moves less)
- a 29 Hz line is present on both boards at every fill and never moves — some
  external source, ignore it

**Down-sweep (350 → 100 oz) verdict:** the trend reverses, so it's driven by the
load rather than by time. But the peak crept **+0.8 Hz in 13 minutes at constant
350 oz**, and every down-sweep point from 200 oz up sits 0.7–0.8 Hz above its
up-sweep partner.

**Interpretation.** Rising under load, creeping at constant load, lagging on the
way down: that's rubber. The erasers stiffen as the weight squashes them, so the
peak is acting as a rubber weighing scale rather than sensing the water's mass.
The water most likely isn't moving with the bucket at these frequencies — the
bucket's thin floor is a spring between the water and the walls, and above the
water's bounce frequency on that floor the water stays put while the walls
vibrate beneath it. Also worth knowing for the real tank: a stiff tank carries
its fuel with it; a floppy one may not.

**Why this matters for the car:** the stiffening signal is ~7% of frequency
across the full range, versus ~72% for the mass effect. Rubber stiffness also
moves with temperature, so engine heat could swamp a 7% signal while barely
denting a 72% one.

---

## The predictor (notebook section 9)

A working baseline, not the final gauge. Linear fit of water on two features
from one board's tap-averaged spectrum:

- `peak_Hz` — log-interpolated peak in 45–75 Hz. Moves most when nearly full.
- `low/high` — RMS 5–50 Hz over RMS 50–150 Hz. Moves most in the first pours.

Each is flat where the other changes. Scored by **leave-one-level-out** CV
(every level predicted by a fit that never saw it): **±27 oz RMS, worst miss
50 oz**, over 14 runs at 8 levels. Guessing the mean would be ±113 oz. It
recalibrates itself from whatever runs are in `DATA_DIR`, warns on
extrapolation, and leaves a file out of its own fit before predicting it.

The 45–75 Hz band was chosen for this setup. Check section 3's plot before
trusting it on a new arrangement.

---

## Already ruled out — don't redo

- **The original 44 Hz shaker dataset (`baja_*.csv`, `main.ipynb`).** The
  "8–13 Hz peak vs volume" correlation there is an artifact. A single-tone
  shaker only illuminates its own harmonics: 4.4% of the 0–500 Hz axis held
  95.4% of the energy, and the 8–13 Hz band sat at the sensor's noise floor.
  What looked like a peak was spectral leakage from the 44 Hz line plus
  single-FFT noise, and R² fell from 0.85 to 0.2 when the first 2 s of each
  recording was trimmed.
- **A single-tone exciter can't find a resonance** that doesn't happen to sit
  on one of its harmonics. Use taps (broadband) or a swept source.
- **A single FFT of one tap is noise.** Average power spectra over many taps.
- **Tracking the tallest peak in a wide band is fragile** — it jumps between
  features. Narrow the band or track a named feature.

---

## Next experiments

1. **Board B onto the bucket** (tape low on the outside wall, Z axis vertical),
   taps still on the table, `PRIMARY_BOARD = "B"`, new folder. This measures the
   bucket's own motion directly and settles whether the mass mode exists at all.
   If its peak falls along `f = 57.4·√(0.846/(0.846 + m_water))`, the physics is
   there and the problem is only seeing it from the table side.
2. If the water proves decoupled, **stiffen the container's floor** so it stands
   in for a real tank, and repeat.
3. **Mount check:** one run with the board clamped or glued instead of taped.
   Where the two spectra diverge is the honest upper frequency limit of taped
   data (section 7).
4. On the car: chassis-side A/B transfer function, with the engine's RPM sweep
   as a free broadband exciter.

---

## How Andrew likes to work on this

- Derivations over formulas — where an equation comes from matters.
- Honest error bars. Cross-validated numbers, not fitted-on-everything numbers.
- Check claims against the data before asserting them; say when something is a
  guess.
- Flag confounds: the up-sweep's water and time were confounded until the
  down-sweep separated them.

---

## Agent skills

### Issue tracker

Issues live in this repo's GitHub Issues, managed with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` plus `docs/adr/` at the repo root. See `docs/agents/domain.md`.
