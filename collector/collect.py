"""BusWise collector: polls Delhi OTD real-time vehicle positions and saves cleaned rows.

Runs for RUN_SECONDS, polling every POLL_SECONDS. Every FLUSH_MINUTES it writes
the rows gathered so far to Parquet part files (split by IST date) and, if
UPLOAD=1, uploads them to the GitHub release "raw-YYYY-MM-DD".

Filters applied before anything is kept:
  * missing or zero coordinates, or outside the Delhi NCR box
  * stale positions (older than STALE_SECONDS when fetched)
  * repeats (same vehicle, timestamp not newer than the last kept one)
  * GPS jumps implying more than MAX_KMH
  * stationary buses: at most one row every STATIONARY_HEARTBEAT seconds

Every poll is also logged to a small "health" table (status, counts, latency).
The API key comes from OTD_API_KEY and is never written anywhere.
"""

import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from google.transit import gtfs_realtime_pb2

URL = "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb"
IST = timezone(timedelta(hours=5, minutes=30))

RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "20400"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))
FLUSH_MINUTES = float(os.environ.get("FLUSH_MINUTES", "30"))
STALE_SECONDS = 600
MAX_KMH = 120
STATIONARY_METRES = 25
STATIONARY_HEARTBEAT = 300
BBOX = (28.2, 29.1, 76.7, 77.7)  # lat_min, lat_max, lon_min, lon_max (Delhi NCR)
OUT = os.environ.get("OUT_DIR", "out")
UPLOAD = os.environ.get("UPLOAD", "0") == "1"
RUN_TAG = os.environ.get("RUN_TAG", str(int(time.time())))

POS_SCHEMA = pa.schema([
    ("vehicle_id", pa.string()), ("vehicle_label", pa.string()),
    ("route_id", pa.string()), ("trip_id", pa.string()),
    ("trip_start_date", pa.string()), ("trip_start_time", pa.string()),
    ("lat", pa.float64()), ("lon", pa.float64()),
    ("speed", pa.float32()), ("bearing", pa.float32()),
    ("ts", pa.int64()), ("fetched_at", pa.int64()),
])
HEALTH_SCHEMA = pa.schema([
    ("fetched_at", pa.int64()), ("status", pa.int32()), ("error", pa.string()),
    ("latency_ms", pa.int32()), ("bytes", pa.int32()), ("header_ts", pa.int64()),
    ("entities", pa.int32()), ("kept", pa.int32()), ("dropped_bbox", pa.int32()),
    ("dropped_stale", pa.int32()), ("dropped_repeat", pa.int32()),
    ("dropped_jump", pa.int32()), ("dropped_stationary", pa.int32()),
])


def metres(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def ist_date(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d")


def fetch(session, key):
    """Return (status, blob, error, latency_ms); retries transient failures."""
    last = (0, b"", "no attempt", 0)
    for attempt in range(3):
        t0 = time.time()
        try:
            r = session.get(URL, params={"key": key}, timeout=40)
            lat = int((time.time() - t0) * 1000)
            if r.status_code == 200:
                return 200, r.content, "", lat
            last = (r.status_code, b"", f"http {r.status_code}", lat)
        except requests.RequestException as exc:
            last = (0, b"", type(exc).__name__, int((time.time() - t0) * 1000))
        time.sleep(3 * (attempt + 1))
    return last


class Collector:
    def __init__(self):
        self.last = {}  # vehicle -> (ts, lat, lon, kept_at_ts)
        self.rows = []
        self.health = []

    def process(self, blob, fetched_at):
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(blob)
        c = dict(entities=0, kept=0, dropped_bbox=0, dropped_stale=0,
                 dropped_repeat=0, dropped_jump=0, dropped_stationary=0)
        for ent in feed.entity:
            if not ent.HasField("vehicle"):
                continue
            c["entities"] += 1
            v = ent.vehicle
            vid = v.vehicle.id or v.vehicle.label or ent.id
            lat, lon, ts = v.position.latitude, v.position.longitude, int(v.timestamp or 0)
            if not (BBOX[0] <= lat <= BBOX[1] and BBOX[2] <= lon <= BBOX[3]):
                c["dropped_bbox"] += 1
                continue
            if ts == 0 or fetched_at - ts > STALE_SECONDS:
                c["dropped_stale"] += 1
                continue
            prev = self.last.get(vid)
            if prev:
                pts, plat, plon, _ = prev
                if ts <= pts:
                    c["dropped_repeat"] += 1
                    continue
                d = metres(plat, plon, lat, lon)
                if d / (ts - pts) * 3.6 > MAX_KMH:
                    c["dropped_jump"] += 1
                    continue
                if d < STATIONARY_METRES and ts - pts < STATIONARY_HEARTBEAT:
                    c["dropped_stationary"] += 1
                    continue
            self.last[vid] = (ts, lat, lon, ts)
            pos = v.position
            self.rows.append({
                "vehicle_id": vid, "vehicle_label": v.vehicle.label or None,
                "route_id": v.trip.route_id or None, "trip_id": v.trip.trip_id or None,
                "trip_start_date": v.trip.start_date or None,
                "trip_start_time": v.trip.start_time or None,
                "lat": lat, "lon": lon,
                "speed": pos.speed if pos.HasField("speed") else None,
                "bearing": pos.bearing if pos.HasField("bearing") else None,
                "ts": ts, "fetched_at": fetched_at,
            })
            c["kept"] += 1
        return feed.header.timestamp or None, c

    def flush(self, final=False):
        """Write buffered rows to Parquet parts (one per IST date) and upload them."""
        os.makedirs(OUT, exist_ok=True)
        stamp = datetime.now(IST).strftime("%H%M%S")
        written = []
        for kind, buf, schema, tcol in (("positions", self.rows, POS_SCHEMA, "ts"),
                                        ("health", self.health, HEALTH_SCHEMA, "fetched_at")):
            by_day = {}
            for r in buf:
                by_day.setdefault(ist_date(r[tcol]), []).append(r)
            for day, rs in by_day.items():
                path = os.path.join(OUT, f"{kind}_{day}_{RUN_TAG}_{stamp}.parquet")
                pq.write_table(pa.Table.from_pylist(rs, schema=schema), path, compression="zstd")
                written.append((day, path, len(rs)))
        self.rows, self.health = [], []
        for day, path, n in written:
            size = os.path.getsize(path)
            print(f"flush: {os.path.basename(path)} rows={n} bytes={size}", flush=True)
            if UPLOAD:
                upload(day, path)
        return written


def upload(day, path):
    tag = f"raw-{day}"
    exists = subprocess.run(["gh", "release", "view", tag], capture_output=True).returncode == 0
    if not exists:
        subprocess.run(["gh", "release", "create", tag, "--title", f"Raw positions {day} (IST)",
                        "--notes", "Raw cleaned vehicle positions collected by BusWise. "
                                   "Source: Delhi Open Transit Data (non-commercial academic use).",
                        "--latest=false"], capture_output=True)
    for attempt in range(4):
        r = subprocess.run(["gh", "release", "upload", tag, path, "--clobber"], capture_output=True, text=True)
        if r.returncode == 0:
            os.remove(path)
            return
        print(f"upload retry {attempt + 1}: {r.stderr.strip()[:200]}", flush=True)
        time.sleep(10 * (attempt + 1))
    print(f"UPLOAD FAILED, keeping {path} for the end-of-run artifact", flush=True)


def main():
    key = os.environ.get("OTD_API_KEY", "").strip()
    if not key:
        sys.exit("OTD_API_KEY is missing")
    session = requests.Session()
    session.headers["User-Agent"] = "BusWise academic project (non-commercial)"
    col = Collector()
    start = time.time()
    next_flush = start + FLUSH_MINUTES * 60
    polls = kept_total = outages = 0
    vehicles = set()
    while time.time() - start < RUN_SECONDS:
        t0 = time.time()
        fetched_at = int(t0)
        status, blob, err, lat_ms = fetch(session, key)
        rec = dict(fetched_at=fetched_at, status=status, error=err or None, latency_ms=lat_ms,
                   bytes=len(blob), header_ts=None, entities=0, kept=0, dropped_bbox=0,
                   dropped_stale=0, dropped_repeat=0, dropped_jump=0, dropped_stationary=0)
        if status == 200:
            try:
                header_ts, counts = col.process(blob, fetched_at)
                rec.update(counts, header_ts=header_ts)
            except Exception as exc:  # malformed feed: log and continue
                rec["error"] = f"parse {type(exc).__name__}"
        if rec["entities"] == 0:
            outages += 1
        polls += 1
        kept_total += rec["kept"]
        col.health.append(rec)
        vehicles.update(r["vehicle_id"] for r in col.rows[-rec["kept"]:] if rec["kept"])
        if polls % 20 == 0:
            print(f"poll {polls}: entities={rec['entities']} kept={rec['kept']} total_kept={kept_total} "
                  f"vehicles_seen={len(vehicles)} outages={outages}", flush=True)
        if time.time() >= next_flush:
            col.flush()
            next_flush = time.time() + FLUSH_MINUTES * 60
        time.sleep(max(0, POLL_SECONDS - (time.time() - t0)))
    col.flush(final=True)
    summary = (f"## Collector run summary\n\n| Metric | Value |\n|---|---|\n"
               f"| Polls | {polls} |\n| Rows kept | {kept_total:,} |\n"
               f"| Distinct buses | {len(vehicles):,} |\n| Empty or failed polls | {outages} |\n"
               f"| Duration | {round((time.time() - start) / 60)} min |\n")
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(summary)


if __name__ == "__main__":
    main()
