"""Check whether live vehicle positions can be matched to the static GTFS.

Usage: python compare_static_live.py <gtfs_dir>
Needs OTD_API_KEY in the environment. Prints coverage numbers only.
"""

import csv
import math
import os
import sys
import time
from collections import defaultdict

import requests
from google.transit import gtfs_realtime_pb2

URL = "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb"
gdir = sys.argv[1]


def read(name):
    with open(os.path.join(gdir, name), newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def hav(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


routes = {r["route_id"]: r for r in read("routes.txt")}
trips = {t["trip_id"]: t for t in read("trips.txt")}
stops = {s["stop_id"]: (float(s["stop_lat"]), float(s["stop_lon"])) for s in read("stops.txt")}
route_stops = defaultdict(set)
with open(os.path.join(gdir, "stop_times.txt"), newline="", encoding="utf-8-sig") as fh:
    for row in csv.DictReader(fh):
        t = trips.get(row["trip_id"])
        if t:
            route_stops[t["route_id"]].add(row["stop_id"])
print(f"static: {len(routes)} routes, {len(trips)} trips, {len(stops)} stops")

key = os.environ["OTD_API_KEY"].strip()
for attempt in range(6):
    blob = requests.get(URL, params={"key": key}, timeout=60).content
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(blob)
    vs = [e.vehicle for e in feed.entity if e.HasField("vehicle")]
    if vs:
        break
    time.sleep(10)
print(f"live: {len(vs)} vehicles")

live_routes = {v.trip.route_id for v in vs if v.trip.route_id}
live_trips = {v.trip.trip_id for v in vs if v.trip.trip_id}
print(f"live distinct routes: {len(live_routes)}, in static: {len(live_routes & routes.keys())} "
      f"({100 * len(live_routes & routes.keys()) / max(len(live_routes), 1):.1f}%)")
print(f"live distinct trips: {len(live_trips)}, in static: {len(live_trips & trips.keys())} "
      f"({100 * len(live_trips & trips.keys()) / max(len(live_trips), 1):.1f}%)")
trip_route_agree = sum(1 for v in vs if v.trip.trip_id in trips and trips[v.trip.trip_id]["route_id"] == v.trip.route_id)
print(f"vehicles whose trip_id maps to the same route_id in static: {trip_route_agree}/{len(vs)}")
print("sample live route ids:", sorted(live_routes, key=lambda x: (len(x), x))[:15])
print("sample live trip ids:", sorted(live_trips)[:8])

# Spatial sanity: distance from each bus to the nearest stop of its claimed route
dists = []
for v in vs:
    sids = route_stops.get(v.trip.route_id)
    if not sids:
        continue
    p = (v.position.latitude, v.position.longitude)
    dists.append(min(hav(p, stops[s]) for s in sids if s in stops))
dists.sort()
if dists:
    q = lambda f: round(dists[int(f * (len(dists) - 1))])
    within = lambda m: 100 * sum(d <= m for d in dists) / len(dists)
    print(f"bus-to-own-route-stop distance (m): median {q(0.5)}, p75 {q(0.75)}, p90 {q(0.9)}; "
          f"within 300 m: {within(300):.1f}%, within 1 km: {within(1000):.1f}%  (n={len(dists)})")
for rid in list(live_routes & routes.keys())[:8]:
    r = routes[rid]
    print(f"  route {rid}: {r.get('route_long_name') or r.get('route_short_name')}")
