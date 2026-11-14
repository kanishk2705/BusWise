# BusWise

A self-updating MLOps system for bus arrival prediction and reliability monitoring in Delhi.
Final-year project, Government College of Engineering, Tirunelveli.

## Data collection (running now)

BusWise collects real-time vehicle positions from
[Delhi Open Transit Data](https://otd.delhi.gov.in) (non-commercial academic use).

- `collector/collect.py` polls the feed every 30 seconds and filters each batch
  (out-of-area points, stale positions, repeats, GPS jumps, parked buses).
- `.github/workflows/collect.yml` runs the collector on GitHub Actions in back-to-back runs of about 5 h 40 min;
  `watchdog.yml` restarts it hourly if it ever stops.
- Every 30 minutes, part files are uploaded to the release `raw-YYYY-MM-DD` (IST date).
- `processing/daily_process.py` (nightly, 00:30 IST) merges the day's parts, writes `data/daily/<date>.json`
  and updates [STATUS.md](STATUS.md).

Raw files are kept for 30 days once their processed tables (stop arrivals, segment travel times) exist;
the processed tables are kept for the whole project.

## Planned components

Route reconstruction · arrival-time model (LightGBM) · daily evaluation, drift detection and retraining (MLflow) ·
FastAPI service · Progressive Web App · Power BI dashboard.
