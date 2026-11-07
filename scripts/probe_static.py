"""Find the Delhi OTD static GTFS download link and print what the page exposes."""

import re
import sys

import requests

PAGE = "https://otd.delhi.gov.in/data/static/"
html = requests.get(PAGE, timeout=60).text
print("page bytes:", len(html))
for pat in [r'href="([^"]+)"', r"action=\"([^\"]+)\"", r"(https?://[^\s'\"<>]+)", r"['\"](/[^'\"\s]*(?:zip|download|static|gtfs)[^'\"\s]*)['\"]"]:
    found = sorted(set(re.findall(pat, html, flags=re.I)))
    print(f"--- {pat} ({len(found)})")
    for f in found:
        if any(w in f.lower() for w in ("zip", "download", "static", "gtfs", "api", "txt", "form")):
            print("  ", f)
for m in re.finditer(r"<form[\s\S]*?</form>", html, flags=re.I):
    print("--- form ---")
    print(re.sub(r"\s+", " ", m.group(0))[:1500])
for m in re.finditer(r"<script[\s\S]*?</script>", html, flags=re.I):
    s = m.group(0)
    if re.search(r"zip|download|static", s, re.I) and len(s) < 6000:
        print("--- script ---")
        print(re.sub(r"\s+", " ", s)[:2500])
sys.exit(0)
