# Baja fuel gauge

Estimating how much fuel (on the bench, water) is in a rubber-mounted tank from vibration measured off the tank, on the frame or table beside it.

## Spectral features

**Table peak**:
The spectral peak near 57 Hz on board A's tap-averaged spectrum. It rises as fill increases, attributed to the rubber mounts stiffening under load, not to mass.
_Avoid_: resonance, resonance frequency, bucket resonance

**Ratio dip**:
The minimum near 78–86 Hz in the ratio of board A's spectrum to board B's. Both its frequency and its depth change with fill.
_Avoid_: anti-resonance (unless the mechanism is confirmed), dip

**Mass mode**:
The resonance of tank plus contents bouncing on the rubber mounts, whose frequency should fall as `1/√m`. Predicted by the physics; not yet observed.
_Avoid_: resonance (unqualified), table peak

## Fill

**Fill level**:
How much liquid is in the container, held as kilograms and reported in US fluid ounces (with cups alongside; 1 cup = 8 fl oz).
_Avoid_: volume, cups (as the primary unit)

**Up-sweep / down-sweep**:
A sequence of runs taken while fill is only increasing, or only decreasing. Runs at the same fill level differ between the two because the rubber lags.
_Avoid_: trial (a trial number is just a repeat index)

## Evaluation

**Leave-one-level-out**:
Scoring where each fill level is predicted by a fit that never saw any run at that level. The headline accuracy number.
_Avoid_: cross-validation (unqualified), test accuracy

**Up→down test**:
Scoring where the predictor is fit on the up-sweep only and predicts the down-sweep. Checks that a feature follows the fill level rather than the rubber's loading history.
_Avoid_: holdout
