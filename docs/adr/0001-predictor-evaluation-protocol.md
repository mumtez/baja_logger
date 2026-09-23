# Score predictors by run, level-out and up→down, with selection nested inside

With only 14 runs per board, the easy ways of scoring a fill predictor overstate it, so every predictor is scored the same honest way. **One run is one data point**: taps within a run share its fill level and its moment in the rubber's loading history, so splitting a run into tap groups would add correlated points and shrink the error bars falsely (tap bootstrap is used only for error bars on individual features). The headline number is **leave-one-level-out** RMS, and every predictor also reports the **up→down test** (fit on the up-sweep, predict the down-sweep), because in the car fuel only goes down and the rubber lags, so a feature that passes the first test but fails the second is tracking loading history, not fill. When features are chosen automatically, the **selection runs inside each cross-validation fold**, so the quoted error includes the cost of choosing.

## Considered options

- Per-tap samples (~400 points): rejected; not independent, error bars would be fiction.
- Pick the best subset by leave-one-level-out, then report that score: rejected; the selection has seen the test data.
- Random k-fold over runs: rejected; runs at the same level would leak between train and test.
