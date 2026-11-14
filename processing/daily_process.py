"""BusWise nightly processing for one IST date.

1. Downloads that day's part files from the release raw-<date>.
2. Merges them into positions_<date>.parquet and health_<date>.parquet
   (removing any duplicates left between runs), uploads both, deletes the parts.
3. Writes a daily summary to data/daily/<date>.json and rebuilds STATUS.md.
4. Retention: deletes raw releases older than RETAIN_DAYS, but only for days
   whose summary says their processed tables (stop arrivals etc.) exist.
   Until that stage is built, nothing is deleted.

Usage: python daily_process.py [YYYY-MM-DD]   (default: yesterday, IST)
"""

import glob
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

IST = timezone(timedelta(hours=5, minutes=30))
RETAIN_DAYS = 30
POLLS_PER_DAY = 86400 // 30


def gh(*args, check=True):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:3])} failed: {r.stderr.strip()[:300]}")
    return r


def assets(tag):
    r = gh("release", "view", tag, "--json", "assets", "-q", ".assets[].name", check=False)
    return r.stdout.split() if r.returncode == 0 else []


def merge(day, kind, workdir, keys):
    parts = sorted(glob.glob(os.path.join(workdir, f"{kind}_{day}_*.parquet")))
    full = os.path.join(workdir, f"{kind}_{day}.parquet")
    frames = [pd.read_parquet(p) for p in parts]
    if os.path.exists(full):
        frames.append(pd.read_parquet(full))
    if not frames:
        return None, parts
    df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=keys).sort_values(keys)
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), full, compression="zstd")
    return df, parts


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else (datetime.now(IST) - timedelta(days=1)).strftime("%Y-%m-%d")
    tag = f"raw-{day}"
    names = assets(tag)
    if not names:
        print(f"No release {tag}; nothing to process.")
        return
    workdir = os.path.join("work", day)
    os.makedirs(workdir, exist_ok=True)
    gh("release", "download", tag, "-D", workdir, "--clobber", "-p", "*.parquet")

    pos, pos_parts = merge(day, "positions", workdir, ["vehicle_id", "ts"])
    hel, hel_parts = merge(day, "health", workdir, ["fetched_at"])

    for kind in ("positions", "health"):
        full = os.path.join(workdir, f"{kind}_{day}.parquet")
        if os.path.exists(full):
            gh("release", "upload", tag, full, "--clobber")
    for p in pos_parts + hel_parts:
        name = os.path.basename(p)
        if name in names:
            gh("release", "delete-asset", tag, name, "-y", check=False)

    s = {"date": day, "processed_at": datetime.now(IST).isoformat(timespec="seconds"),
         "arrivals_processed": False}
    if pos is not None and len(pos):
        hours = pd.to_datetime(pos["ts"], unit="s", utc=True).dt.tz_convert(IST).dt.hour
        per_hour = pos.groupby(hours)["vehicle_id"].nunique()
        age = (pos["fetched_at"] - pos["ts"])
        s.update({
            "rows": int(len(pos)),
            "vehicles": int(pos["vehicle_id"].nunique()),
            "routes": int(pos["route_id"].nunique()),
            "trips": int(pos["trip_id"].nunique()),
            "with_route_pct": round(100 * pos["route_id"].notna().mean(), 1),
            "with_speed_pct": round(100 * pos["speed"].notna().mean(), 1),
            "position_age_median_s": float(age.median()),
            "buses_by_hour_ist": {int(h): int(n) for h, n in per_hour.items()},
            "positions_file_mb": round(os.path.getsize(os.path.join(workdir, f"positions_{day}.parquet")) / 1e6, 1),
        })
    if hel is not None and len(hel):
        empty = int((hel["entities"] == 0).sum())
        s.update({
            "polls": int(len(hel)),
            "poll_coverage_pct": round(100 * len(hel) / POLLS_PER_DAY, 1),
            "empty_or_failed_polls": empty,
            "median_latency_ms": float(hel["latency_ms"].median()),
            "dropped": {k: int(hel[k].sum()) for k in
                        ("dropped_bbox", "dropped_stale", "dropped_repeat", "dropped_jump", "dropped_stationary")},
        })
    os.makedirs("data/daily", exist_ok=True)
    with open(f"data/daily/{day}.json", "w") as fh:
        json.dump(s, fh, indent=2)
    print(json.dumps(s, indent=2))

    write_status()
    retention()


def write_status():
    days = []
    for f in sorted(glob.glob("data/daily/*.json")):
        with open(f) as fh:
            days.append(json.load(fh))
    lines = ["# BusWise data collection status", "",
             "Collected from Delhi Open Transit Data (real-time vehicle positions) by the BusWise collector.",
             "Updated nightly.", "",
             "| Date (IST) | Rows kept | Buses | Routes | Poll coverage | Empty/failed polls | File (MB) |",
             "|---|---|---|---|---|---|---|"]
    for d in reversed(days):
        lines.append(f"| {d['date']} | {d.get('rows', 0):,} | {d.get('vehicles', '-')} | {d.get('routes', '-')} | "
                     f"{d.get('poll_coverage_pct', '-')}% | {d.get('empty_or_failed_polls', '-')} | "
                     f"{d.get('positions_file_mb', '-')} |")
    total = sum(d.get("rows", 0) for d in days)
    lines += ["", f"Days collected: {len(days)} · Total rows kept: {total:,}", ""]
    with open("STATUS.md", "w") as fh:
        fh.write("\n".join(lines))


def retention():
    cutoff = (datetime.now(IST) - timedelta(days=RETAIN_DAYS)).strftime("%Y-%m-%d")
    r = gh("release", "list", "--limit", "500", "--json", "tagName", "-q", ".[].tagName", check=False)
    for tag in r.stdout.split():
        if not tag.startswith("raw-"):
            continue
        day = tag[4:]
        if day >= cutoff:
            continue
        summary = f"data/daily/{day}.json"
        done = os.path.exists(summary) and json.load(open(summary)).get("arrivals_processed")
        if done:
            gh("release", "delete", tag, "-y", "--cleanup-tag", check=False)
            print(f"retention: deleted {tag}")
        else:
            print(f"retention: kept {tag} (processed tables not built yet)")


if __name__ == "__main__":
    main()
