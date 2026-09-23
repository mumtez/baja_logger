# Bucket on erasers, 20–21 Sept 2026

The first bench setup: a stand-in for the rubber-mounted tank.

| | |
|---|---|
| Container | Home Depot bucket, 838 g empty |
| Mounts | 3 rubber erasers under the bucket, 22.5 g total, on a table |
| Board A | taped to the table beside the bucket (the primary board) |
| Board B | taped to the table farther away, on the other side of the bucket |
| Mount method | tape (`mount=tape`) |
| Excitation | rubber-mallet taps on the table, ~1/s, same spot, 30 s per run |
| Fill | water in 50 fl oz pours (1 fl oz = 0.02957 kg), 0–350 oz |

## Runs

- **Up-sweep** 23:27–23:50 on 20 Sept: 0 (twice), 50, 100, 150, 200, 250, 300, 350 oz.
- **Down-sweep** 00:03–00:11 on 21 Sept: 350, 250, 200, 150, 100 oz.

Every run has both boards; A and B files from one capture share a timestamp.
All runs report `dropped=0 late=0 mpu_clip=0 i2c_err=0`.

## Known quirks

- A 29 Hz line is present on both boards at every fill and never moves: an
  outside source, not the setup.
- The table peak crept +0.8 Hz over 13 minutes at constant 350 oz: the erasers
  creep under load.
