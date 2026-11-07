"""Smoke test for the Delhi Open Transit Data real-time feed.

Fetches the GTFS-Realtime VehiclePositions feed a few times, parses it and
writes a summary (never the API key) to stdout, the GitHub job summary and
feed_check.json.
"""

import json
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from google.transit import gtfs_realtime_pb2

URL = "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb"
SNAPSHOTS = int(os.environ.get("SNAPSHOTS", "4"))
GAP_SECONDS = int(os.environ.get("GAP_SECONDS", "30"))
IST = timezone(timedelta(hours=5, minutes=30))
DELHI_BBOX = (28.3, 29.0, 76.8, 77.5)  # lat_min, lat_max, lon_min, lon_max


def fetch(key):
    resp = requests.get(URL, params={"key": key}, timeout=60)
    return resp.status_code, resp.content


def parse(blob):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(blob)
    rows = []
    for ent in feed.entity:
        if not ent.HasField("vehicle"):
            continue
        v = ent.vehicle
        rows.append({
            "vehicle_id": v.vehicle.id or v.vehicle.label or ent.id,
            "route_id": v.trip.route_id,
            "trip_id": v.trip.trip_id,
            "lat": v.position.latitude,
            "lon": v.position.longitude,
            "speed": v.position.speed if v.position.HasField("speed") else None,
            "ts": v.timestamp or None,
        })
    return feed.header.timestamp, rows


def ist(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d %H:%M:%S IST") if ts else "n/a"


def main():
    key = os.environ.get("OTD_API_KEY", "").strip()
    if not key:
        print("OTD_API_KEY secret is missing or empty.")
        sys.exit(1)

    snaps = []
    for i in range(SNAPSHOTS):
        t0 = time.time()
        try:
            status, blob = fetch(key)
        except requests.RequestException as exc:
            snaps.append({"ok": False, "error": type(exc).__name__})
            print(f"snapshot {i + 1}: request failed ({type(exc).__name__})")
        else:
            if status != 200:
                snaps.append({"ok": False, "status": status, "body": blob[:200].decode("utf-8", "replace")})
                print(f"snapshot {i + 1}: HTTP {status}")
            else:
                header_ts, rows = parse(blob)
                snaps.append({"ok": True, "status": status, "bytes": len(blob),
                              "header_ts": header_ts, "fetched_at": int(t0), "rows": rows})
                print(f"snapshot {i + 1}: HTTP 200, {len(blob):,} bytes, {len(rows):,} vehicles, feed time {ist(header_ts)}")
        if i < SNAPSHOTS - 1:
            time.sleep(GAP_SECONDS)

    empty = sum(1 for s in snaps if s.get("ok") and not s["rows"])
    good = [s for s in snaps if s.get("ok") and s["rows"]]
    report = {"checked_at": ist(int(time.time())), "snapshots_with_data": len(good),
              "snapshots_empty": empty, "snapshots_total": len(snaps)}
    lines = ["# Delhi OTD live feed check", "", f"Checked at {report['checked_at']}", ""]

    if not good:
        report["errors"] = [{k: v for k, v in s.items() if k != "rows"} for s in snaps]
        lines += ["**Result: FAILED** — no snapshot returned usable data.", "", "```", json.dumps(report["errors"], indent=2), "```"]
    else:
        last = good[-1]
        rows = last["rows"]
        now = last["fetched_at"]
        ages = [now - r["ts"] for r in rows if r["ts"]]
        in_delhi = sum(DELHI_BBOX[0] <= r["lat"] <= DELHI_BBOX[1] and DELHI_BBOX[2] <= r["lon"] <= DELHI_BBOX[3] for r in rows)
        routes = {r["route_id"] for r in rows if r["route_id"]}
        trips = {r["trip_id"] for r in rows if r["trip_id"]}
        speeds = [r["speed"] for r in rows if r["speed"] is not None]

        moved = None
        if len(good) >= 2:
            first = {r["vehicle_id"]: (r["lat"], r["lon"], r["ts"]) for r in good[0]["rows"]}
            common = [r for r in rows if r["vehicle_id"] in first]
            moved = sum(first[r["vehicle_id"]][:2] != (r["lat"], r["lon"]) for r in common)
            report["vehicles_in_first_and_last"] = len(common)
            report["vehicles_moved_between_first_and_last"] = moved

        report.update({
            "vehicles_last_snapshot": len(rows),
            "vehicles_per_snapshot": [len(s["rows"]) for s in good],
            "bytes_last_snapshot": last["bytes"],
            "feed_header_time": ist(last["header_ts"]),
            "distinct_routes": len(routes),
            "distinct_trips": len(trips),
            "with_route_id_pct": round(100 * sum(bool(r["route_id"]) for r in rows) / max(len(rows), 1), 1),
            "inside_delhi_bbox_pct": round(100 * in_delhi / max(len(rows), 1), 1),
            "position_age_seconds_median": statistics.median(ages) if ages else None,
            "position_age_seconds_p90": sorted(ages)[int(0.9 * (len(ages) - 1))] if ages else None,
            "with_speed_pct": round(100 * len(speeds) / max(len(rows), 1), 1),
            "est_rows_per_day_at_this_rate": len(rows) * (86400 // 10),
        })
        sample = [{k: r[k] for k in ("vehicle_id", "route_id", "lat", "lon", "speed")} | {"time": ist(r["ts"])} for r in rows[:5]]
        report["sample"] = sample

        lines += ["**Result: OK** — live data received.", "", "| Metric | Value |", "|---|---|"]
        for k, v in report.items():
            if k != "sample":
                lines.append(f"| {k} | {v} |")
        lines += ["", "Sample rows:", "", "```", json.dumps(sample, indent=2), "```"]

    text = "\n".join(lines)
    print(text)
    with open("feed_check.json", "w") as fh:
        json.dump(report, fh, indent=2)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(text + "\n")
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
