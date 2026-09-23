"""
Shared analysis code for the tap-test notebooks.

    from tapkit import load_setup, add_spectra

Loading, tap finding and spectra are used by both notebooks. The features and
scoring are what 02_predictor.ipynb uses; the scoring follows
docs/adr/0001-predictor-evaluation-protocol.md.
"""

import glob
import itertools
import re
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal

# Mass of one unit of water, kg, for whichever unit the fill was logged in
# (capture.py --oz or --cups). A US fluid ounce of water is 29.57 g.
KG_PER_UNIT = {"oz": 0.02957, "cups": 0.2366}
OZ_PER_CUP = 8.0


# ------------------------------------------------------------------ loading

DATA_ROW = re.compile(r"^-?\d+(?:,-?\d+)+$")


def load_run(path):
    """Return (DataFrame in g, metadata dict) for one capture file.

    Classifies every line rather than assuming the header ends at the first
    non-comment. The ESP32 ROM prints boot messages before the sketch starts,
    and a WROOM's UART emits garbage bytes while the baud rate settles, so
    both land in the file above the real header.
    """
    meta, columns, rows, noise = {}, None, [], 0
    with open(path, "rb") as fh:
        for raw in fh:
            line = raw.decode("utf-8", "replace").strip(" \t\r\n\x00�")
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
        raise ValueError(f"{Path(path).name}: no '# columns:' header anywhere -- "
                         "the boot banner was lost, so scale factors are unknown")
    if not rows:
        raise ValueError(f"{Path(path).name}: no data rows")

    rows = [r for r in rows if len(r) == len(columns)]
    df = pd.DataFrame(np.array(rows, dtype=np.int64), columns=columns)

    # A garbled line can leave a timestamp that jumps backwards. Drop those.
    keep = df["t_us"].diff().fillna(1) > 0
    n_back = int((~keep).sum())
    df = df[keep].reset_index(drop=True)
    meta["_noise_lines"] = noise
    meta["_backwards_rows"] = n_back

    mpu_lsb = float(meta.get("lsb_per_g", 4096.0))
    for c in df.columns:
        if c.startswith("mpu_"):
            df[c] = df[c] / mpu_lsb
        elif c.startswith("adxl_"):
            df[c] = df[c] / 20.5

    df["t_s"] = (df["t_us"] - df["t_us"].iloc[0]) / 1e6
    return df, meta


def run_info(path, df, meta):
    """The run dict the notebooks work with, or None if it has no fill level."""
    name = Path(path).name
    fs = 1.0 / np.median(np.diff(df["t_s"].values))
    # Fill level in whatever unit it was logged in, converted to grams of
    # water. Grouping on grams means 0 cups and 0 oz land in the same place.
    unit = next((u for u in KG_PER_UNIT if u in meta), None)
    if unit is not None:
        fill = float(meta[unit])
    else:
        mf = re.search(r"run_(oz|cups)(\d+)_", name)
        if not mf:
            return None
        unit, fill = mf.group(1), float(mf.group(2))
    mb = re.search(r"_trial\d+_([A-Za-z0-9]+)_", name)
    ms = re.search(r"_(\d{8}_\d{6})\.csv$", name)
    return dict(
        name=name, path=str(path),
        df=df, meta=meta, fs=fs,
        water_g=int(round(fill * KG_PER_UNIT[unit] * 1000)),
        fill=fill, unit=unit, lvl=f"{fill:g} {unit}",
        trial=int(meta.get("trial", 1)),
        mount=meta.get("mount", "?"),
        board=meta.get("board", mb.group(1) if mb else "A"),
        stamp=ms.group(1) if ms else meta.get("captured_at", ""),
    )


def load_setup(data_dir, min_samples=1000):
    """Load every run_*.csv in one setup folder. Prints what it skips."""
    runs = []
    for path in sorted(glob.glob(str(Path(data_dir) / "run_*.csv"))):
        try:
            df, meta = load_run(path)
        except Exception as e:
            print(f"skipped {Path(path).name}: {e}")
            continue
        if len(df) < min_samples:
            print(f"skipped {Path(path).name}: only {len(df)} samples")
            continue
        r = run_info(path, df, meta)
        if r is None:
            print(f"left out {Path(path).name}: no fill level recorded "
                  "(use it with predict_water in 02_predictor)")
            continue
        runs.append(r)
    assign_sweeps(runs)
    return runs


def assign_sweeps(runs):
    """Label each run 'up' or 'down' by when it was captured.

    Everything up to and including the first run at the session's highest fill
    is the up-sweep; everything after is the down-sweep. Trial numbers are not
    used: the second empty run of 20 Sept is trial 2 but came before any water.
    A `sweep=` note in the file header overrides this.
    """
    if not runs:
        return
    order = sorted(runs, key=lambda r: r["stamp"])
    top = max(r["water_g"] for r in runs)
    first_top = next(r["stamp"] for r in order if r["water_g"] == top)
    for r in runs:
        r["sweep"] = r["meta"].get("sweep") or ("up" if r["stamp"] <= first_top else "down")


def hhmm(r):
    """Capture time as HH:MM, to tell apart runs that share a fill and trial."""
    s = r.get("stamp", "")
    if len(s) >= 15 and s[8] == "_":
        return f"{s[9:11]}:{s[11:13]}"
    return s[11:16] if len(s) >= 16 else "?"


def captures(runs):
    """Group runs into captures by timestamp: {stamp: {board: run}}.

    capture.py gives both boards' files the same stamp, so this is how A and B
    from one recording are paired -- never by fill level and trial, which repeat.
    """
    out = {}
    for r in runs:
        out.setdefault(r["stamp"], {})[r["board"]] = r
    return dict(sorted(out.items()))


# ------------------------------------------------------------ taps, spectra

def find_taps(x, fs, sigma_mult=6.0, refractory_s=0.30):
    """Indices of tap onsets in x (g)."""
    b, a = signal.butter(2, 5.0, "highpass", fs=fs)
    y = signal.filtfilt(b, a, x - np.mean(x))
    env = np.abs(y)
    # Median absolute deviation: robust to the taps themselves.
    sigma = np.median(env) * 1.4826
    idx, _ = signal.find_peaks(env,
                               height=max(sigma_mult * sigma, 1e-4),
                               distance=int(refractory_s * fs))
    return idx


def tap_spectrum(x, fs, taps, pre_s=0.02, win_s=0.5):
    """Tap-synchronous averaged amplitude spectrum. Returns (freqs, amp, n_used).

    The exponential window forces each tap's response to zero by the window's
    end, which stops truncation from smearing the peaks. It adds a known,
    uniform bit of damping; frequencies are unaffected.
    """
    n = int(win_s * fs)
    npre = int(pre_s * fs)
    w = np.exp(-np.arange(n) / (n / 4.0))

    acc, used = np.zeros(n // 2 + 1), 0
    for t in taps:
        s = t - npre
        if s < 0 or s + n > len(x):
            continue
        seg = x[s:s + n].astype(float)
        seg = seg - seg.mean()
        acc += np.abs(np.fft.rfft(seg * w)) ** 2
        used += 1

    if used == 0:
        return np.fft.rfftfreq(n, 1 / fs), np.zeros(n // 2 + 1), 0
    return np.fft.rfftfreq(n, 1 / fs), np.sqrt(acc / used), used


def add_spectra(runs, axis="mpu_az"):
    """Find taps and the tap-averaged spectrum for every run, in place."""
    for r in runs:
        x = r["df"][axis].values
        r["x"] = x
        r["taps"] = find_taps(x, r["fs"])
        r["f"], r["amp"], r["n_taps"] = tap_spectrum(x, r["fs"], r["taps"])


def ratio_spectrum(a, b, amp_a=None, amp_b=None):
    """|A| / |B| on A's frequency grid.

    The two boards have independent crystals, so B's spectrum is interpolated
    onto A's grid before dividing. The tap's strength is in both, so it cancels.
    """
    amp_a = a["amp"] if amp_a is None else amp_a
    amp_b = b["amp"] if amp_b is None else amp_b
    return amp_a / np.maximum(np.interp(a["f"], b["f"], amp_b), 1e-12)


# ---------------------------------------------------------------- features

TABLE_PEAK_BAND = (45.0, 75.0)   # the table peak, near 57 Hz on board A
LOW_BAND = (5.0, 50.0)           # where the bucket's weight makes the table sluggish
HIGH_BAND = (50.0, 150.0)
LINE_29HZ = (27.0, 31.0)         # outside source, present at every fill
DIP_BAND = (65.0, 100.0)         # the ratio dip, 78-86 Hz on 20 Sept


def _vertex(y, i):
    """Sub-bin offset of the extremum at y[i], by a parabola through 3 points."""
    if 0 < i < len(y) - 1:
        y0, y1, y2 = y[i - 1:i + 2]
        d = y0 - 2 * y1 + y2
        if d != 0:
            return float(np.clip(0.5 * (y0 - y2) / d, -1, 1))
    return 0.0


def peak_freq(f, amp, band):
    """Frequency of the tallest point in band, interpolated on log amplitude."""
    m = (f >= band[0]) & (f <= band[1])
    fb, la = f[m], np.log(amp[m] + 1e-30)
    i = int(np.argmax(la))
    return fb[i] + _vertex(la, i) * (fb[1] - fb[0])


def dip_freq(f, ratio, band=DIP_BAND):
    """Frequency of the deepest point in band, interpolated on log ratio."""
    return peak_freq(f, 1.0 / np.maximum(ratio, 1e-30), band)


def band_rms(f, amp, band, notch=None):
    m = (f >= band[0]) & (f <= band[1])
    if notch is not None:
        m &= ~((f >= notch[0]) & (f <= notch[1]))
    return np.sqrt(np.mean(amp[m] ** 2))


def low_high(f, amp, notch=None):
    """RMS in LOW_BAND over RMS in HIGH_BAND."""
    return band_rms(f, amp, LOW_BAND, notch) / band_rms(f, amp, HIGH_BAND)


# Name -> (boards needed, one-line physical story). The order is the order
# every table and plot uses.
FEATURES = {
    "table_peak_Hz":  ("A",  "rubber stiffening under load"),
    "low/high":       ("A",  "table near A heavier at low frequency: a mass effect"),
    "low/high_notch": ("A",  "same, with the 29 Hz outside line removed"),
    "dip_Hz":         ("AB", "ratio dip frequency; anti-resonance if the physics holds"),
    "dip_depth_log":  ("AB", "log10 of the ratio at the dip; mechanism unclear"),
    "B_peak_Hz":      ("B",  "far board's own peak: expected to be a control"),
    "B_low/high":     ("B",  "far board's low/high: expected to be a control"),
}
FEATURE_NAMES = list(FEATURES)


def features_from_spectra(f_a, amp_a, f_b, amp_b):
    """Every survey feature from one capture's two tap-averaged spectra."""
    ratio = amp_a / np.maximum(np.interp(f_a, f_b, amp_b), 1e-12)
    m = (f_a >= DIP_BAND[0]) & (f_a <= DIP_BAND[1])
    return {
        "table_peak_Hz": peak_freq(f_a, amp_a, TABLE_PEAK_BAND),
        "low/high": low_high(f_a, amp_a),
        "low/high_notch": low_high(f_a, amp_a, notch=LINE_29HZ),
        "dip_Hz": dip_freq(f_a, ratio),
        "dip_depth_log": float(np.log10(ratio[m].min())),
        "B_peak_Hz": peak_freq(f_b, amp_b, TABLE_PEAK_BAND),
        "B_low/high": low_high(f_b, amp_b),
    }


def feature_table(runs, primary="A", other="B", min_taps=5):
    """One row per capture that has both boards: fill, sweep, every feature."""
    rows = []
    for stamp, boards in captures(runs).items():
        a, b = boards.get(primary), boards.get(other)
        if a is None or b is None or min(a["n_taps"], b["n_taps"]) < min_taps:
            continue
        row = dict(stamp=stamp, when=hhmm(a), lvl=a["lvl"], trial=a["trial"],
                   sweep=a["sweep"], water_kg=a["water_g"] / 1000.0,
                   n_taps=a["n_taps"], name_a=a["name"], name_b=b["name"])
        row.update(features_from_spectra(a["f"], a["amp"], b["f"], b["amp"]))
        rows.append(row)
    return pd.DataFrame(rows)


def tap_bootstrap(a, b, n=200, seed=0):
    """Feature spread from resampling one capture's taps. Returns (n, features).

    Only for error bars on a feature. Resampled taps share the run's fill and
    its moment in the rubber's history, so they are never used as extra data
    points for fitting (ADR 0001).
    """
    rng = np.random.default_rng(seed)
    ta, tb = np.asarray(a["taps"]), np.asarray(b["taps"])
    same = len(ta) == len(tb)   # the boards caught the same taps: resample jointly
    out = np.empty((n, len(FEATURE_NAMES)))
    for k in range(n):
        ia = rng.integers(0, len(ta), len(ta))
        ib = ia if same else rng.integers(0, len(tb), len(tb))
        f_a, amp_a, _ = tap_spectrum(a["x"], a["fs"], ta[ia])
        f_b, amp_b, _ = tap_spectrum(b["x"], b["fs"], tb[ib])
        feats = features_from_spectra(f_a, amp_a, f_b, amp_b)
        out[k] = [feats[nm] for nm in FEATURE_NAMES]
    return out


# ----------------------------------------------------------------- scoring
#
# A "model" here is a function  fit(X_train, y_train) -> predict(X) .
# Every scorer takes the full feature matrix and does its own splitting, so a
# model that chooses features does that choosing inside each fold.

def fit_linear(X, y):
    A = np.column_stack([np.ones(len(X)), X])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    return coef


def apply_linear(coef, X):
    X = np.atleast_2d(X)
    return coef[0] + X @ coef[1:]


def linear_model(cols):
    """Straight-line fit on the given feature columns."""
    cols = list(cols)

    def fit(X, y):
        coef = fit_linear(X[:, cols], y)
        return lambda Xn: apply_linear(coef, np.atleast_2d(Xn)[:, cols])
    fit.cols = cols
    return fit


def level_out(X, y, model):
    """Leave-one-level-out predictions: each level from a fit that never saw it."""
    pred = np.full(len(y), np.nan)
    for lv in np.unique(y):
        test = y == lv
        pred[test] = model(X[~test], y[~test])(X[test])
    return pred


def up_down(X, y, sweep, model):
    """Fit on the up-sweep, predict the down-sweep. NaN for up-sweep runs."""
    sweep = np.asarray(sweep)
    up = sweep == "up"
    pred = np.full(len(y), np.nan)
    if up.any() and (~up).any():
        pred[~up] = model(X[up], y[up])(X[~up])
    return pred


def rmse(pred, y):
    ok = np.isfinite(pred)
    return float(np.sqrt(np.mean((pred[ok] - y[ok]) ** 2)))


def worst(pred, y):
    ok = np.isfinite(pred)
    return float(np.max(np.abs(pred[ok] - y[ok])))


def subset_search(n_features, max_k=3, candidates=None):
    """Linear model that picks its own feature subset by inner leave-one-level-out.

    Called as a model inside level_out or up_down, the search only ever sees
    that fold's training runs, so the outer score includes the cost of
    choosing. After fitting, `.chosen` on the returned predictor holds the
    subset it settled on.
    """
    pool = list(range(n_features)) if candidates is None else list(candidates)
    subsets = [c for k in range(1, max_k + 1) for c in itertools.combinations(pool, k)]

    def fit(X, y):
        # Need at least k + 2 levels for the inner fits to be determined.
        n_lv = len(np.unique(y))
        best, best_err = None, np.inf
        for cols in subsets:
            if len(cols) + 2 > n_lv:
                continue
            err = rmse(level_out(X, y, linear_model(cols)), y)
            if err < best_err:
                best, best_err = cols, err
        predict = linear_model(best)(X, y)
        predict.chosen = best
        return predict
    return fit


def pls_model(max_components=3):
    """Partial least squares on whole spectra, components chosen by inner LOLO.

    A benchmark for how much fill information the spectrum holds in total,
    not a gauge anyone should trust: there is no physical story behind it.
    """
    from sklearn.cross_decomposition import PLSRegression

    def fit_n(X, y, n):
        p = PLSRegression(n_components=n).fit(X, y)
        return lambda Xn: p.predict(np.atleast_2d(Xn)).ravel()

    def fit(X, y):
        n_lv = len(np.unique(y))
        best, best_err = 1, np.inf
        for n in range(1, max_components + 1):
            if n + 2 > n_lv:
                break
            err = rmse(level_out(X, y, lambda Xt, yt: fit_n(Xt, yt, n)), y)
            if err < best_err:
                best, best_err = n, err
        predict = fit_n(X, y, best)
        predict.chosen = best
        return predict
    return fit


def log_ratio_matrix(runs, table, primary="A", other="B", band=(5.0, 150.0)):
    """log10 |A|/|B| over band, one row per capture in `table` (for PLS)."""
    caps = captures(runs)
    rows = []
    for stamp in table["stamp"]:
        a, b = caps[stamp][primary], caps[stamp][other]
        m = (a["f"] >= band[0]) & (a["f"] <= band[1])
        rows.append(np.log10(ratio_spectrum(a, b)[m]))
    return np.array(rows)


# ------------------------------------------------------------------- units

def kg_to_oz(kg):
    return np.asarray(kg) / KG_PER_UNIT["oz"]


def oz_and_cups(kg):
    """'150 oz (18.8 cups)' for a mass of water."""
    oz = float(kg_to_oz(kg))
    return f"{oz:.0f} oz ({oz / OZ_PER_CUP:.1f} cups)"
