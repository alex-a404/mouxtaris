"""
EOA Lefkosia / NDLGO (ndlgo.org.cy) water-interruption scraper.

Unlike EOA Pafos, this is NOT a WordPress post feed -- it's a single page
(https://ndlgo.org.cy/water-supply/breakdowns-maintenance/) with three
Elementor/PowerPack HTML <table>s that always show the CURRENTLY ACTIVE
situation:

  - "Έκτακτη Διακοπή Ύδρευσης"          (emergency breakdown)   -> fault
  - "Προγραμματισμένες Διακοπές Ύδρευσης" (scheduled interruption) -> scheduled
  - "Τρέχουσες Βλάβες"                   (current breakdowns)   -> fault

Rows disappear from these tables once resolved -- there's no post id, no
permalink, no publish date. That means this scraper must push the FULL
current snapshot every cycle (like aik-service/eac_scrape.py), not just
new items (like eoa_pafos_scrape.py): the Go dispatcher's Reconcile() marks
anything absent from a push's payload as resolved, so pushing only deltas
would wrongly resolve still-open outages on the very next cycle.

No WAF/cookie gate was found on this host (plain GETs return 200), and
both /wp-json/ and any /feed/ path are unusable (401 / 404) -- so this is
plain HTML table scraping.

Discovery (scrape the 3 tables into rows) is separate from extraction (ask
a local LLM to turn each Greek row into structured fields) is separate from
push (POST the full current snapshot to /ingest/eoa, same shape as
/ingest/eac, outage_type "water"). The LLM's job is narrow: normalize the
town name, split out street/zone detail, and resolve dates (DD/MM/YY(YY),
or phrases like "εντός της ημέρας") into YYYY-MM-DD -- outage_cause is
assigned deterministically from which table a row came from, not inferred.

A row's LLM extraction is cached by a content hash (there's no stable id to
key on), so an unchanged row costs zero LLM calls on later cycles -- but
the snapshot pushed each cycle always contains every row currently on the
page, cache hit or not.

Env:
  INGEST_URL     e.g. http://localhost:8080/ingest/eoa
  INGEST_TOKEN   shared secret, sent as X-Ingest-Token (must match Oracle)
  OLLAMA_URL     default http://localhost:11434
  OLLAMA_MODEL   default qwen2.5:7b-instruct
  CACHE_STORE    path to the row-extraction cache (default: eoa_lefkosia_cache.json,
                 next to this script)

  python eoa_lefkosia_scrape.py             # loop forever, push every cycle
  python eoa_lefkosia_scrape.py --once      # single pass, then exit
"""

import argparse, json, os, random, re, sys, time
from datetime import date, datetime
from hashlib import sha1
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

PAGE_URL = "https://ndlgo.org.cy/water-supply/breakdowns-maintenance/"
DISTRICT = "Lefkosia"

# (substring match against the section's <h4>, outage_cause it implies)
SECTIONS = [
    ("Έκτακτη Διακοπή", "fault"),
    ("Προγραμματισμένες Διακοπές", "scheduled"),
    ("Τρέχουσες Βλάβες", "fault"),
]

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}
TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)
RETRIES = 4

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
# CPU inference is slow, and a cold model load alone can take minutes -- keep
# the read timeout generous and configurable (see eoa_pafos_scrape.py, which
# hit this in production).
LLM_TIMEOUT = httpx.Timeout(
    connect=5.0, read=float(os.environ.get("OLLAMA_TIMEOUT", "600")), write=10.0, pool=10.0
)
# Keep the model resident between calls so a poll cycle after the first
# doesn't pay a cold-load again.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

TZ = ZoneInfo("Asia/Nicosia")
WS = re.compile(r"\s+")
CACHE_STORE = Path(os.environ.get("CACHE_STORE", Path(__file__).with_name("eoa_lefkosia_cache.json")))


def clean(s: str) -> str:
    return WS.sub(" ", s or "").strip()


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def fetch(client: httpx.Client, url: str) -> Optional[str]:
    for attempt in range(RETRIES):
        try:
            r = client.get(url)
            r.raise_for_status()
            return r.text
        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as e:
            if attempt < RETRIES - 1:
                wait = min(2 ** attempt + random.uniform(0, 1), 30)
                print(f"  fetch {url} attempt {attempt + 1} failed "
                      f"({type(e).__name__}); retry in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"  fetch {url} gave up after {RETRIES} attempts "
                      f"({type(e).__name__})", file=sys.stderr)
    return None


def section_cause(heading: str) -> Optional[str]:
    for needle, cause in SECTIONS:
        if needle in heading:
            return cause
    return None


DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})")


def parse_ddmmyy(s: str) -> Optional[date]:
    m = DATE_RE.search(s)
    if not m:
        return None
    d, mo, y = m.groups()
    y = int(y) + 2000 if len(y) == 2 else int(y)
    try:
        return date(y, int(mo), int(d))
    except ValueError:
        return None


def row_start_date(pairs: List[tuple]) -> Optional[date]:
    """The outage-start column, whatever it's called this section ("ΩΡΑ
    ΔΙΑΚΟΠΗΣ" / "ΕΚΤΙΜΩΜΕΝΗ ΩΡΑ ΔΙΑΚΟΠΗΣ") -- excludes the restoration
    column, which also contains the word ΔΙΑΚΟΠΗΣ."""
    for h, v in pairs:
        if "ΔΙΑΚΟΠΗΣ" in h and "ΕΠΑΝΑΦΟΡ" not in h:
            d = parse_ddmmyy(v)
            if d:
                return d
    return None


def parse_page(html: str) -> List[dict]:
    """Every <h4> that matches a known section is followed by a <table>;
    each <tbody> <tr> becomes one row item, keyed by a content hash since
    the site gives no stable id."""
    soup = BeautifulSoup(html, "html.parser")
    today = datetime.now(TZ).date()
    items = []
    for h in soup.find_all("h4"):
        heading = clean(h.get_text(" "))
        cause = section_cause(heading)
        if cause is None:
            continue
        table = h.find_next("table")
        if table is None:
            continue
        headers = [clean(th.get_text(" ")) for th in table.select("thead th")]
        body = table.find("tbody")
        if body is None:
            continue
        for tr in body.find_all("tr"):
            cells = [clean(td.get_text(" ")) for td in tr.find_all("td")]
            if not any(cells):
                continue
            pairs = list(zip(headers, cells)) if headers else [(f"col{i}", c) for i, c in enumerate(cells)]
            # "scheduled" rows are tied to a calendar date -- once it's past,
            # the event is moot even if the site never removes the row (its
            # own frontend hides these client-side; we have to do it
            # ourselves since we only fetch raw HTML). Fault/breakdown rows
            # are NOT filtered this way: a fault can legitimately still be
            # open days after it started.
            if cause == "scheduled":
                start = row_start_date(pairs)
                if start is not None and start < today:
                    continue
            row_text = " | ".join(f"{h}: {v}" for h, v in pairs if v)
            row_hash = sha1(f"{heading}|{row_text}".encode("utf-8")).hexdigest()[:16]
            items.append({
                "row_hash": row_hash,
                "section": heading,
                "cause": cause,
                "row_text": row_text,
            })
    return items


SYSTEM_PROMPT = """You extract structured water-outage data from a single row of a Greek \
table published by NDLGO (the Nicosia District Local Government Organisation).

Output ONLY a JSON object of the form:
{"outages": [
  {
    "town_village": "...",
    "area_subdistrict": "...",
    "part_of_area": "...",
    "outage_from_date": "YYYY-MM-DD",
    "outage_from_time": "HH:MM" or "",
    "outage_to_date": "YYYY-MM-DD" or "",
    "outage_to_time": "HH:MM" or ""
  }
]}

Field rules:
- town_village: the municipality/community name (e.g. "Λατσιά", "Μάμμαρι"), in Greek
  exactly as written, WITHOUT any parenthetical reference code (e.g. drop "(περ.21)").
  Required -- if you cannot find one, omit that outage from the array entirely.
- area_subdistrict: specific street name(s) if given (e.g. "Ηπείρου, Σταύρου Βενιζέλου").
  If several streets are listed, join them with ", ". Else "".
- part_of_area: a broader named zone that isn't a street (e.g. "Νέος Οικισμός και
  Βιομηχανική Περιοχή"), or any other descriptive qualifier. Else "".
- Dates in the row are given as DD/MM/YY or DD/MM/YYYY -- a 2-digit year "26" means
  2026. Convert to YYYY-MM-DD. You are also given today's date as a reference for any
  relative phrasing.
- The restoration column is sometimes a phrase instead of a date:
  - "Εντός της ημέρας" (within the same day) -> same date as outage_from_date, time "".
  - "Μέχρι νεωτέρας" or similar (until further notice / not yet known) -> leave
    outage_to_date and outage_to_time as "".
  - "Μέχρι το πρωί" / "το μεσημέρι" / "το απόγευμα" / "το βράδυ" (by morning / noon /
    afternoon / evening) -> same date as outage_from_date, with an approximate clock
    time: πρωί "09:00", μεσημέρι "12:00", απόγευμα "17:00", βράδυ "20:00".
- Times: this table rarely gives real clock times -- only fill outage_from_time /
  outage_to_time if an actual HH:MM appears in the text. Leave "" otherwise.
- Almost every row describes exactly one location; only include multiple objects in
  "outages" if the text clearly names multiple unrelated places.

Example 1:
Today's date: 2026-08-05
Row: ΔΗΜΟΣ/ΚΟΙΝΟΤΗΤΑ: Λατσιά (περ.21) | ΑΝΑΦΟΡΑ: Ενημερώνουμε οτι έχουμε κλειστά νερά στα \
ΛΑΤΣΙΑ, ΗΠΕΙΡΟΥ, ΣΤΑΥΡΟΥ ΒΕΝΙΖΕΛΟΥ λόγω βλάβης σε κεντρικό αγωγό. | ΩΡΑ ΔΙΑΚΟΠΗΣ: 05/08/26 | \
ΕΚΤΙΜΩΜΕΝΟΣ ΧΡΟΝΟΣ ΕΠΑΝΑΦΟΡΑΣ: 05/08/26
Output: {"outages": [{"town_village": "Λατσιά", "area_subdistrict": "Ηπείρου, Σταύρου Βενιζέλου", \
"part_of_area": "", "outage_from_date": "2026-08-05", "outage_from_time": "", \
"outage_to_date": "2026-08-05", "outage_to_time": ""}]}

Example 2:
Today's date: 2026-06-01
Row: ΔΗΜΟΣ/ΚΟΙΝΟΤΗΤΑ: Μάμμαρι | ΕΠΗΡΕΑΖΟΜΕΝΕΣ ΟΔΟΙ/ΣΗΜΕΙΑ: Νέος Οικισμός και Βιομηχανική \
Περιοχή | ΛΟΓΟΣ ΔΙΑΚΟΠΗΣ: θα πραγματοποιηθούν εργασίες καθαρισμού υδατόπυργων που \
εξυπηρετούν τον νέο οικισμό και τη βιομηχανική περιοχή Μαμμαρίου | ΕΚΤΙΜΩΜΕΝΗ ΩΡΑ \
ΔΙΑΚΟΠΗΣ: 03/06/26 | ΕΚΤΙΜΩΜΕΝΟΣ ΧΡΟΝΟΣ ΕΠΑΝΑΦΟΡΑΣ: Εντός της ημέρας
Output: {"outages": [{"town_village": "Μάμμαρι", "area_subdistrict": "", \
"part_of_area": "Νέος Οικισμός και Βιομηχανική Περιοχή", "outage_from_date": "2026-06-03", \
"outage_from_time": "", "outage_to_date": "2026-06-03", "outage_to_time": ""}]}

Example 3:
Today's date: 2026-08-26
Row: ΔΗΜΟΣ/ΚΟΙΝΟΤΗΤΑ: Αλάμπρα | ΑΝΑΦΟΡΑ: Ενημερώνουμε οτι έχουμε κλειστά νερά στην ΑΛΑΜΠΡΑ \
(περ.30) , ΟΛΟ ΤΟ ΧΩΡΙΟ λόγω βλάβης σε κεντρικό αγωγό. Εκτιμούμε ότι θα διορθωθεί μέχρι το \
μεσημέρι. | ΩΡΑ ΔΙΑΚΟΠΗΣ: 26/08/26 | ΕΚΤΙΜΩΜΕΝΟΣ ΧΡΟΝΟΣ ΕΠΑΝΑΦΟΡΑΣ: Μέχρι το μεσημέρι
Output: {"outages": [{"town_village": "Αλάμπρα", "area_subdistrict": "", \
"part_of_area": "ΟΛΟ ΤΟ ΧΩΡΙΟ", "outage_from_date": "2026-08-26", "outage_from_time": "", \
"outage_to_date": "2026-08-26", "outage_to_time": "12:00"}]}
"""


def call_llm(client: httpx.Client, row_text: str) -> Optional[list]:
    user_content = f"Today's date: {date.today().isoformat()}\nRow: {row_text}"
    try:
        r = client.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                "format": "json",
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
                "options": {"temperature": 0.1},
            },
        )
        r.raise_for_status()
    except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.TransportError) as e:
        print(f"  llm call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    content = r.json().get("message", {}).get("content", "")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        print(f"  llm returned non-JSON: {content[:200]!r}", file=sys.stderr)
        return None

    outages = data.get("outages")
    if not isinstance(outages, list) or not outages:
        print("  llm found no outages for row", file=sys.stderr)
        return None
    return outages


def localize(date_s: str, time_s: str) -> str:
    date_s, time_s = clean(date_s), clean(time_s) or "00:00"
    if not date_s:
        return ""
    try:
        return datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ).isoformat()
    except ValueError:
        print(f"  bad date/time from llm: {date_s!r} {time_s!r}", file=sys.stderr)
        return ""


def to_payloads(outages: list, cause: str) -> List[dict]:
    payloads = []
    for o in outages:
        if not isinstance(o, dict):
            continue
        town = clean(o.get("town_village", ""))
        if not town:
            continue  # unusable without a place to resolve against
        payloads.append({
            "source": "eoa_lefkosia",
            "district": DISTRICT,
            "town_village": town,
            "area_subdistrict": clean(o.get("area_subdistrict", "")),
            "part_of_area": clean(o.get("part_of_area", "")),
            "outage_type": "water",
            "outage_cause": cause,
            "outage_from": localize(o.get("outage_from_date", ""), o.get("outage_from_time", "")),
            "outage_to": localize(o.get("outage_to_date", ""), o.get("outage_to_time", "")),
        })
    return payloads


def load_cache() -> dict:
    if CACHE_STORE.exists():
        try:
            return json.loads(CACHE_STORE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_cache(cache: dict) -> None:
    CACHE_STORE.write_text(json.dumps(cache, ensure_ascii=False))


def push(url: str, token: str, payloads: List[dict]) -> None:
    headers = {"X-Ingest-Token": token, "X-Scraper-Source": "eoa_lefkosia"}
    r = httpx.post(url, json=payloads, headers=headers, timeout=30)
    r.raise_for_status()
    print(
        f"[{now_str()}] pushed {len(payloads)} rows -> {r.status_code} {r.text[:200]}",
        file=sys.stderr,
    )
    print(json.dumps(payloads, ensure_ascii=False))


def cycle(ingest_url: str, ingest_token: str) -> None:
    with httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=TIMEOUT) as client:
        html = fetch(client, PAGE_URL)
    if html is None:
        print(f"[{now_str()}] fetch failed, skipping cycle", file=sys.stderr)
        return

    rows = parse_page(html)
    cache = load_cache()

    payloads: List[dict] = []
    fresh_hashes = set()
    with httpx.Client(timeout=LLM_TIMEOUT) as llm_client:
        for row in rows:
            fresh_hashes.add(row["row_hash"])
            cached = cache.get(row["row_hash"])
            if cached is None:
                outages = call_llm(llm_client, row["row_text"])
                if outages is None:
                    continue  # not cached -- retried next cycle, and not in this snapshot
                cache[row["row_hash"]] = outages
                cached = outages
            payloads += to_payloads(cached, row["cause"])

    # Snapshot semantics: the Go side resolves anything missing from a push,
    # so the cache must not grow unbounded with rows that fell off the page.
    for stale in set(cache) - fresh_hashes:
        del cache[stale]
    save_cache(cache)

    if not payloads:
        print(f"[{now_str()}] 0 active outage(s)", file=sys.stderr)

    # Always push the full current snapshot (even empty), so Reconcile()
    # correctly resolves anything that just disappeared from the page.
    push(ingest_url, ingest_token, payloads)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--interval", type=int, default=300, help="loop gap seconds")
    args = ap.parse_args()

    ingest_url = os.environ.get("INGEST_URL")
    ingest_token = os.environ.get("INGEST_TOKEN")
    if not ingest_url or not ingest_token:
        print("set INGEST_URL and INGEST_TOKEN", file=sys.stderr)
        sys.exit(1)

    if args.once:
        cycle(ingest_url, ingest_token)
        return

    while True:
        try:
            cycle(ingest_url, ingest_token)
        except Exception as e:
            print(f"cycle error: {e}", file=sys.stderr)  # never die; retry next tick
        time.sleep(args.interval + random.uniform(0, args.interval * 0.1))


if __name__ == "__main__":
    main()
