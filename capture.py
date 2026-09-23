#!/usr/bin/env python3
"""
Laptop-side capture for the ESP32 dual-accelerometer logger.

Records one run to a CSV that carries its own configuration header, so the
file still makes sense next month when nobody remembers what range the
accelerometer was on.

    pip install pyserial

    python capture.py --list
    python capture.py --port /dev/cu.usbmodem1101 --monitor     # is the board alive?

One sensor:

    python capture.py --port /dev/cu.usbmodem1101 \\
        --oz 50 --trial 1 --seconds 40

Log the fill with --oz (fluid ounces) or --cups, whichever you measure in.

Two boards at once (writes two files, one per board):

    python capture.py --port /dev/cu.usbmodem1101 --port2 /dev/cu.usbserial-0001 \\
        --label A --label2 B --oz 50 --trial 1 --seconds 40

The two boards have independent clocks, so their timestamps are NOT aligned
with each other. The analysis lines them up on the taps instead, which is why
a few milliseconds of start skew here does not matter.

Reading a run back:

    from capture import load_run
    df, meta = load_run("run_oz050_trial1_A_20260920_161500.csv")
"""

import argparse
import datetime as dt
import re
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is missing.  pip install pyserial")


# --------------------------------------------------------------- port setup

def list_serial_ports():
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return
    for p in ports:
        print(f"  {p.device:32s} {p.description}")


def guess_port(exclude=()):
    candidates = []
    for p in list_ports.comports():
        if p.device in exclude:
            continue
        if (p.vid, p.pid) == (0x303A, 0x1001):      # C3 native USB-serial-JTAG
            return p.device
        blob = f"{p.description} {p.manufacturer or ''}".lower()
        if any(k in blob for k in ("esp32", "espressif", "jtag", "cp210", "ch340")):
            candidates.append(p.device)
    return candidates[0] if candidates else None


def open_port(port, baud=921600):
    """Open without asserting DTR/RTS.

    On an ESP32-C3 the USB-serial-JTAG peripheral watches those two lines and
    uses them as the reset / download-mode strap. pyserial raises both by
    default when it opens a port, which can drop the chip into its bootloader
    and leave you with a port that enumerates fine and never says anything.
    On a WROOM with a USB-UART bridge they drive the auto-reset circuit, so
    leaving them low just means the sketch keeps running instead of rebooting.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0
    ser.dtr = False
    ser.rts = False
    try:
        ser.open()
    except serial.SerialException as e:
        text = str(e).lower()
        if any(k in text for k in ("busy", "access", "permission")):
            sys.exit(
                f"\n{port} is already open in another program.\n"
                "Usually the Arduino IDE Serial Monitor -- close it and try again.\n"
                f"To find what is holding it:  lsof {port}\n"
            )
        sys.exit(f"\nCould not open {port}: {e}")
    try:
        ser.dtr = False
        ser.rts = False
    except (OSError, serial.SerialException):
        pass
    return ser


def diagnosis(port):
    return f"""
Nothing arrived from {port}. In rough order of likelihood:

  1. USB CDC On Boot is disabled in the Arduino IDE (C3 only).
     Tools > USB CDC On Boot > Enabled, then reflash. Without it the
     sketch prints to the UART pins instead of USB, so the port
     enumerates but stays silent.

  2. The sketch did not actually flash. Reflash and watch for
     "Hard resetting" at the end.

  3. Wrong board selected.

  4. The board is in bootloader mode. Unplug it, plug it back in,
     and try again without pressing BOOT.

To check by hand:   python capture.py --port {port} --monitor
"""


# ------------------------------------------------------------------ monitor

def monitor(port, seconds=20, baud=921600):
    ser = open_port(port, baud)
    print(f"Listening on {port} for {seconds}s. Sending 'g' at 3s.\n")
    got = 0
    with ser:
        t0 = time.time()
        sent = False
        while time.time() - t0 < seconds:
            data = ser.read(4096)
            if data:
                got += len(data)
                sys.stdout.write(data.decode("utf-8", errors="replace"))
                sys.stdout.flush()
            else:
                time.sleep(0.01)
            if not sent and time.time() - t0 > 3.0:
                ser.write(b"g\n")
                sent = True
                print("\n[sent g]\n")
        ser.write(b"s\n")
    print(f"\n\n--- {got} bytes received ---")
    if got == 0:
        print(diagnosis(port))
    return got


# ------------------------------------------------------------------- stream

DATA_ROW = re.compile(r"^-?\d+(?:,-?\d+)+$")


class Stream:
    """One board: its port, its output file, its own line buffer."""

    def __init__(self, ser, path, label, notes):
        self.ser = ser
        self.label = label
        self.path = path
        self.f = open(path, "w", newline="")
        self.buf = b""
        self.n_data = 0
        self.n_noise = 0
        self.first_t = None
        self.last_t = None

        self.f.write(f"# captured_at={dt.datetime.now().isoformat(timespec='seconds')}\n")
        self.f.write(f"# board={label}\n")
        for note in notes:
            self.f.write(f"# {note}\n")

    def send(self, text):
        self.ser.write(text.encode())

    def pump(self, echo=True):
        """Read whatever is waiting, split into lines, file them. Never blocks."""
        try:
            n = self.ser.in_waiting
        except (OSError, serial.SerialException):
            return
        if n:
            self.buf += self.ser.read(n)

        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            # Strip nulls and replacement chars too: the ESP32 ROM prints boot
            # messages before the sketch runs, and a UART bridge emits garbage
            # while the baud rate settles. Sometimes a real row is glued to the
            # tail of that garbage, so strip first and test after.
            line = raw.decode("utf-8", errors="replace").strip(" \t\r\n\x00\ufffd")
            if not line:
                continue

            if line.startswith("#"):
                self.f.write(line + "\n")
                if echo:
                    print(f"  [{self.label}] {line}")
                continue

            if not DATA_ROW.match(line):
                self.n_noise += 1
                if self.n_noise <= 3:
                    self.f.write(f"# noise: {line[:120]!r}\n")
                continue

            self.f.write(line + "\n")
            self.n_data += 1
            try:
                t_us = int(line.split(",", 1)[0])
            except ValueError:
                continue
            if self.first_t is None:
                self.first_t = t_us
            self.last_t = t_us

    def summary(self):
        out = [f"{self.path}", f"  board {self.label}: {self.n_data} samples"]
        if self.n_noise:
            out.append(f"  {self.n_noise} non-data lines filtered (boot messages)")
        if self.first_t is not None and self.last_t is not None and self.last_t > self.first_t:
            span = (self.last_t - self.first_t) / 1e6
            rate = self.n_data / span
            out.append(f"  {span:.2f} s of data, {rate:.1f} Hz mean rate")
            if rate < 900:
                out.append("  WARNING rate well under 1000 Hz -- check dropped/late above")
        return "\n".join(out)

    def close(self):
        self.f.close()


# ------------------------------------------------------------------ capture

def capture(specs, seconds, notes, baud=921600):
    """specs: list of (port, out_path, label). One entry per board."""
    streams = []
    for port, path, label in specs:
        print(f"Opening {port}  (board {label}) ...")
        streams.append(Stream(open_port(port, baud), path, label, notes))

    try:
        time.sleep(2.0)                       # boards boot and print their headers
        for s in streams:
            s.pump()                          # keep the banner: it has the scale factors

        for s in streams:
            for note in notes:
                s.send(note + "\n")
            s.send("z\n")
        time.sleep(0.1)

        # Start every board back to back. A few ms of skew is fine -- the
        # analysis aligns on taps, not on these clocks.
        for s in streams:
            s.send("g\n")

        t0 = time.time()
        last_report = t0
        retried = False
        gave_up = False

        while True:
            elapsed = time.time() - t0
            if elapsed >= seconds:
                break

            for s in streams:
                s.pump()
            time.sleep(0.002)

            now = time.time()
            if now - last_report >= 2.0:
                last_report = now
                counts = "  ".join(
                    f"{s.label}:{s.n_data:6d} ({s.n_data / elapsed:6.1f} Hz)"
                    for s in streams)
                print(f"  {elapsed:5.1f}s   {counts}")

                total = sum(s.n_data for s in streams)
                if total == 0 and elapsed > 4.0 and not retried:
                    print("  nothing yet -- resending 'g'")
                    for s in streams:
                        s.send("g\n")
                    retried = True
                elif total == 0 and elapsed > 9.0:
                    gave_up = True
                    break

        for s in streams:
            s.send("s\n")
        deadline = time.time() + 1.2
        while time.time() < deadline:
            for s in streams:
                s.pump()
            time.sleep(0.01)

    finally:
        for s in streams:
            s.close()
            try:
                s.ser.close()
            except Exception:
                pass

    if gave_up:
        print(diagnosis(specs[0][0]))
        return 0

    print()
    for s in streams:
        print(s.summary())

    quiet = [s.label for s in streams if s.n_data == 0]
    if quiet:
        print(f"\nWARNING board(s) {', '.join(quiet)} sent nothing.")
    return sum(s.n_data for s in streams)


# -------------------------------------------------------------- reading back

def load_run(path):
    """Return (DataFrame in g, metadata dict). Requires pandas.

    Tolerates ESP32 ROM boot messages and UART garbage appearing anywhere,
    including above the sketch's own banner.
    """
    import pandas as pd
    import numpy as np

    meta, columns, rows, noise = {}, None, [], 0
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", "replace").strip(" \t\r\n\x00\ufffd")
            if not line:
                continue
            if line.startswith("#"):
                body = line[1:].strip()
                if body.startswith("columns:"):
                    columns = [c.strip() for c in body.split(":", 1)[1].split(",")]
                for token in body.split():
                    if "=" in token:
                        k, v = token.split("=", 1)
                        meta.setdefault(k, v)
            elif DATA_ROW.match(line):
                rows.append(line.split(","))
            else:
                noise += 1

    if columns is None:
        raise ValueError(f"{path} has no '# columns:' header anywhere -- "
                         "the boot banner was lost, so scale factors are unknown")
    if not rows:
        raise ValueError(f"{path} has no data rows")

    rows = [r for r in rows if len(r) == len(columns)]
    df = pd.DataFrame(np.array(rows, dtype=np.int64), columns=columns)

    keep = df["t_us"].diff().fillna(1) > 0
    meta["_noise_lines"] = noise
    meta["_backwards_rows"] = int((~keep).sum())
    df = df[keep].reset_index(drop=True)

    mpu_lsb = float(meta.get("lsb_per_g", 4096.0))
    for c in df.columns:
        if c.startswith("mpu_"):
            df[c] = df[c] / mpu_lsb
        elif c.startswith("adxl_"):
            df[c] = df[c] / 20.5

    df["t_s"] = (df["t_us"] - df["t_us"].iloc[0]) / 1e6
    return df, meta


# -------------------------------------------------------------------- entry

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list serial ports and exit")
    ap.add_argument("--monitor", action="store_true",
                    help="dump raw board output for 20s, no file written")
    ap.add_argument("--port", help="serial port (auto-detected if omitted)")
    ap.add_argument("--port2", help="second board's port, for simultaneous capture")
    ap.add_argument("--label", default="A", help="name for the first board")
    ap.add_argument("--label2", default="B", help="name for the second board")
    ap.add_argument("--seconds", type=float, default=40.0, help="run length")
    ap.add_argument("--cups", type=int, help="water in the bucket, cups")
    ap.add_argument("--oz", type=int, help="water in the bucket, fluid ounces")
    ap.add_argument("--trial", type=int, default=1, help="repeat number at this fill")
    ap.add_argument("--mount", default="", help="how the boards are attached")
    ap.add_argument("--note", action="append", default=[], help="free text, repeatable")
    args = ap.parse_args()

    if args.list:
        list_serial_ports()
        return

    port = args.port or guess_port()
    if not port:
        sys.exit("No port found. Run with --list and pass --port explicitly.")

    if args.monitor:
        monitor(port, seconds=min(args.seconds, 20.0))
        return

    notes = []
    if args.oz is not None and args.cups is not None:
        sys.exit("Give --oz or --cups, not both.")
    if args.oz is not None:
        notes.append(f"oz={args.oz}")
    elif args.cups is not None:
        notes.append(f"cups={args.cups}")
    notes.append(f"trial={args.trial}")
    if args.mount:
        notes.append(f"mount={args.mount}")
    notes.extend(args.note)

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.oz is not None:
        fill = f"oz{args.oz:03d}"
    elif args.cups is not None:
        fill = f"cups{args.cups:03d}"
    else:
        fill = "fillxxx"

    def name(label):
        return f"run_{fill}_trial{args.trial}_{label}_{stamp}.csv"

    specs = [(port, name(args.label), args.label)]
    if args.port2:
        specs.append((args.port2, name(args.label2), args.label2))

    print(f"Recording {args.seconds:.0f}s from {len(specs)} board(s)")
    print("Tap the table about once a second, same spot, same mallet.\n")
    capture(specs, args.seconds, notes)


if __name__ == "__main__":
    main()
