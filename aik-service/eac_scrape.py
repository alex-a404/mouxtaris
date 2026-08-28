"""
Env:
  INGEST_URL     e.g. https://oracle.example.com:8080/ingest/eac
  INGEST_TOKEN   shared secret, sent as X-Ingest-Token (must match Oracle)

  python eac_push.py            # loop forever
  python eac_push.py --once     # single push (for cron / testing)
"""

import argparse, hashlib, os, random, re, sys, time
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

BASE = ("https://www.eac.com.cy/EN/RegulatedActivities/Distribution/"
        "PowerInterruptions/Pages/Faultsandscheduledinterruptions.aspx")

DISTRICTS = {0: "Lefkosia", 1: "Lemesos", 2: "Larnaka", 3: "Pafos", 4: "Ammochostos"}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,el;q=0.8",
}
TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)
RETRIES_PER_DISTRICT = 4

TZ = ZoneInfo("Asia/Nicosia")

ZWSP = re.compile(r"[\u200b\u200c\u200d\u200e\u200f\ufeff]")
WS   = re.compile(r"\s+")
SECTION_RE  = re.compile(r"CURRENT OUTAGES|SCHEDULED INTERRUPTIONS", re.I)
EMPTY_HINTS = ("there are not", "there are no", "δεν υπάρχουν")

def clean(s: str) -> str:
    return WS.sub(" ", ZWSP.sub("", s or "")).strip()

MERIDIEM = {"π.μ.": "AM", "μ.μ.": "PM", "πμ": "AM", "μμ": "PM"}

def parse_dt(raw: str) -> str | None:
    s = clean(raw)
    for gr, en in MERIDIEM.items():
        s = s.replace(gr, en)
    for fmt in ("%d/%m/%Y %I:%M %p", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ).isoformat()
        except ValueError:
            pass
    return None

@dataclass
class EACRow:
    district: str
    section: str            # "fault" | "scheduled"
    town_village: str
    area_subdistrict: str
    part_of_area: str
    start_raw: str
    start: str | None
    est_restoration_raw: str
    est_restoration: str | None
    key: str = ""

    def finish(self):
        basis = "|".join((self.district, self.section, self.town_village,
                          self.area_subdistrict, self.part_of_area, self.start_raw))
        self.key = hashlib.sha1(basis.encode()).hexdigest()[:16]
        return self

def section_of(table) -> str | None:
    marker = table.find_previous(string=SECTION_RE)
    if not marker:
        return None
    return "scheduled" if "SCHEDULED" in marker.upper() else "fault"

def parse_page(html: str, district: str) -> list[EACRow]:
    soup, rows = BeautifulSoup(html, "html.parser"), []
    for table in soup.find_all("table"):
        head = clean(table.get_text(" ")).upper()
        if "TOWN" not in head or "INTERRUPTION" not in head:
            continue
        sec = section_of(table)
        if sec is None:
            continue
        for tr in table.find_all("tr"):
            cells = [clean(td.get_text(" ")) for td in tr.find_all("td")]
            if len(cells) < 5:
                continue
            joined = " ".join(cells).lower()
            if any(h in joined for h in EMPTY_HINTS) or cells[0].upper().startswith("TOWN"):
                continue
            rows.append(EACRow(
                district=district, section=sec,
                town_village=cells[0], area_subdistrict=cells[1], part_of_area=cells[2],
                start_raw=cells[3], start=parse_dt(cells[3]),
                est_restoration_raw=cells[4], est_restoration=parse_dt(cells[4]),
            ).finish())
    return rows

def fetch_district(client: httpx.Client, district_id: int) -> str | None:
    for attempt in range(RETRIES_PER_DISTRICT):
        try:
            r = client.get(BASE, params={"District": district_id})
            r.raise_for_status()
            return r.text
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as e:
            if attempt < RETRIES_PER_DISTRICT - 1:
                wait = min(2 ** attempt + random.uniform(0, 1), 30)
                print(f"  district {district_id} attempt {attempt + 1} failed "
                      f"({type(e).__name__}); retry in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"  district {district_id} gave up after "
                      f"{RETRIES_PER_DISTRICT} attempts ({type(e).__name__})", file=sys.stderr)
    return None

def get_outages() -> List[dict]:
    out = []
    with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        for did, name in DISTRICTS.items():
            html = fetch_district(client, did)
            if html is None:
                continue
            out += parse_page(html, name or f"district_{did}")
            time.sleep(1)
    uniq = {}
    for row in out:
        uniq.setdefault(row.key, asdict(row))
    return list(uniq.values())


def to_payload(row: dict) -> dict:
    """Map an EACRow dict to the JSON shape main.go's ingest expects."""
    return {
        "source": "eac",
        "district": row["district"],
        "town_village": row["town_village"],
        "area_subdistrict": row["area_subdistrict"],
        "part_of_area": row["part_of_area"],
        "outage_type": "power",
        "outage_cause": row["section"],              # "fault" | "scheduled"
        "outage_from": row["start"] or "",
        "outage_to": row["est_restoration"] or "", # "" = unknown / until further notice
    }

def push(url: str, token: str, rows: List[dict]) -> None:
    payload = [to_payload(r) for r in rows]
    r = httpx.post(
        url,
        json=payload,
        headers={"X-Ingest-Token": token, "X-Scraper-Source": "eac"},
        timeout=30,
    )
    r.raise_for_status()
    print(
        f"[{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S %Z')}] "
        f"pushed {len(payload)} rows -> {r.status_code} {r.text[:200]}",
        file=sys.stderr,
    )
    print(payload)

def cycle(url: str, token: str) -> None:
    rows = get_outages()          # partial data on failure is fine
    push(url, token, rows)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single push, then exit")
    ap.add_argument("--interval", type=int, default=300, help="loop gap seconds")
    args = ap.parse_args()

    url = os.environ.get("INGEST_URL")
    token = os.environ.get("INGEST_TOKEN")
    if not url or not token:
        print("set INGEST_URL and INGEST_TOKEN", file=sys.stderr)
        sys.exit(1)

    if args.once:
        cycle(url, token)
        return

    while True:
        try:
            cycle(url, token)
        except Exception as e:
            print(f"cycle error: {e}", file=sys.stderr)   # never die; retry next tick
        time.sleep(args.interval + random.uniform(0, args.interval * 0.1))

if __name__ == "__main__":
    main()