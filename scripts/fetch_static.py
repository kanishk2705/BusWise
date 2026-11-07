"""Download the official Delhi OTD static data (stops, routes, trips, stop_times).

The portal serves each file through a short POST form (usage type, purpose,
name, e-mail, terms). Name and e-mail come from environment variables so they
are never stored in the repository.

Usage: python fetch_static.py <out_dir>
"""

import io
import os
import re
import sys
import zipfile

import requests

BASE = "https://otd.delhi.gov.in"
PAGE = BASE + "/data/static/"
FILES = ["stops", "routes", "trips", "stop_times"]

out = sys.argv[1]
os.makedirs(out, exist_ok=True)
name = os.environ["OTD_NAME"]
email = os.environ["OTD_EMAIL"]

s = requests.Session()
s.headers["User-Agent"] = "BusWise student project (non-commercial)"
html = s.get(PAGE, timeout=60).text

forms = re.findall(r"<form[\s\S]*?</form>", html, flags=re.I)
print(f"forms on page: {len(forms)}")
first = forms[0] if forms else ""
fields = sorted(set(re.findall(r'name=["\']([^"\']+)["\']', first)))
print("form fields:", fields)

for kind in FILES:
    form = next((f for f in forms if f"value='{kind}'" in f or f'value="{kind}"' in f), None)
    if form is None:
        print(f"{kind}: no form found")
        continue
    token = re.search(r"name=['\"]csrfmiddlewaretoken['\"] value=['\"]([^'\"]+)", form).group(1)
    data = {
        "csrfmiddlewaretoken": token,
        "dataDownloaded": kind,
        "usageType": "NonCommercial",
        "purpose": "Academia",
        "name": name,
        "email": email,
    }
    # Tick any checkbox that is not a purpose option (e.g. terms agreement).
    for box in re.findall(r"<input[^>]*type=['\"]checkbox['\"][^>]*>", form, flags=re.I):
        n = re.search(r"name=['\"]([^'\"]+)", box)
        v = re.search(r"value=['\"]([^'\"]+)", box)
        if n and n.group(1) not in ("purpose",):
            data[n.group(1)] = v.group(1) if v else "on"
    r = s.post(PAGE, data=data, headers={"Referer": PAGE}, timeout=180)
    ctype = r.headers.get("Content-Type", "")
    disp = r.headers.get("Content-Disposition", "")
    print(f"{kind}: HTTP {r.status_code}, {len(r.content):,} bytes, type={ctype}, disposition={disp}")
    body = r.content
    if body[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            for member in z.namelist():
                print(f"   zip member: {member}")
                z.extract(member, out)
    elif "text/html" in ctype:
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
        print("   html response:", text[:400])
    else:
        fname = re.search(r'filename="?([^";]+)', disp)
        path = os.path.join(out, fname.group(1) if fname else f"{kind}.txt")
        with open(path, "wb") as fh:
            fh.write(body)
        print(f"   saved {path}")

print("files:", sorted(os.listdir(out)))
for f in sorted(os.listdir(out)):
    p = os.path.join(out, f)
    if os.path.isfile(p):
        with open(p, encoding="utf-8-sig", errors="replace") as fh:
            head = [next(fh, "").strip() for _ in range(3)]
        n = sum(1 for _ in open(p, encoding="utf-8-sig", errors="replace"))
        print(f"  {f}: {n:,} lines | {head}")
