# EOA Lemesos (eoalemesos.org.cy) water-interruption announcement scraper.
#
# Same shape as eoa_pafos_scrape.py (discovery -> LLM extraction -> push,
# dedup by seen-set so only new announcements get pushed) but eoalemesos.org.cy
# is not WordPress: no RSS feed, no wp-json. The /el/faults listing is a
# plain paginated HTML page (page 1 at /el/faults, older pages at
# /el/faults/2, /el/faults/3, ... -- past the last page it 200s with an
# empty list rather than 404ing). Each entry links to a /el/news-details/...
# permalink, which is also the only stable id available (no numeric post id
# like WP's ?p=N), and the listing only shows a truncated excerpt -- the
# full body has to be fetched from the detail page.

import argparse, json, os, random, re, sys, time
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

ROOT = "https://eoalemesos.org.cy/"
LISTING = "https://eoalemesos.org.cy/el/faults"
DISTRICT = "Lemesos"

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "el-GR,el;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}
TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=10.0)
RETRIES = 4

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
LLM_TIMEOUT = httpx.Timeout(
    connect=5.0, read=float(os.environ.get("OLLAMA_TIMEOUT", "600")), write=10.0, pool=10.0
)
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

TZ = ZoneInfo("Asia/Nicosia")

WS = re.compile(r"\s+")
SEEN_STORE = Path(os.environ.get("SEEN_STORE", Path(__file__).with_name("eoa_lemesos_seen.json")))


def clean(s: str) -> str:
    return WS.sub(" ", s or "").strip()


def fetch(client: httpx.Client, url: str, referer: str | None = None) -> str | None:
    headers = {"Referer": referer} if referer else {}
    for attempt in range(RETRIES):
        try:
            r = client.get(url, headers=headers)
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


def parse_listing_date(raw: str) -> str | None:
    try:
        return datetime.strptime(clean(raw), "%d-%m-%Y").replace(tzinfo=TZ).date().isoformat()
    except ValueError:
        return None


def parse_listing(html: str) -> List[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for entry in soup.select("div.blog-entry"):
        a = entry.select_one("h4 a")
        if a is None or not a.get("href"):
            continue
        li = entry.select_one(".entry-meta li")
        out.append({
            "permalink": clean(a["href"]),
            "title": clean(a.get_text(" ")),
            "listing_date_raw": clean(li.get_text(" ")) if li else "",
        })
    return out


def fetch_pages(client: httpx.Client, pages: int) -> List[dict]:
    fetch(client, ROOT)  # warm-up: acquire cookies
    out = []
    for page in range(1, pages + 1):
        url = LISTING if page == 1 else f"{LISTING}/{page}"
        html = fetch(client, url, referer=ROOT)
        if html is None:
            break
        items = parse_listing(html)
        if not items:
            break  # ran past the last page
        out += items
        time.sleep(1)
    return out


def parse_detail(html: str) -> dict:
    """The detail page's whole date+title+body block lives in one
    <div class="mb40 pb40">, but the body itself is hand-authored per post
    and comes in at least two different shapes: plain <p>/<li> paragraphs,
    or Facebook-copy-paste markup where every line is its own
    <div dir="auto"> (bullets there are emoji <img>s with no text, so
    get_text() on those divs already drops them cleanly). Take the text of
    every leaf p/li/div -- i.e. one with no nested p/li/div of its own --
    so either shape (and a mix of both) comes out as one line per line."""
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one("div.mb40.pb40") or soup
    date_p = container.select_one("p.post-date")
    date_raw = clean(date_p.get_text(" ")) if date_p else ""
    if date_p is not None:
        date_p.decompose()
    title_wrap = container.select_one(".mb-3")
    if title_wrap is not None:
        title_wrap.decompose()

    lines = []
    for el in container.find_all(["p", "li", "div"]):
        if el.find(["p", "li", "div"]) is not None:
            continue  # not a leaf -- its text is captured via its children
        text = clean(el.get_text(" "))
        if text:
            lines.append(text)
    return {"date_raw": date_raw, "body": "\n".join(lines)}


SYSTEM_PROMPT = """You extract structured water-outage data from Greek announcements \
published by the Limassol District Organisation of Local Authorities (EOA Lemesos).

Output ONLY a JSON object of the form:
{"outages": [
  {
    "town_village": "...",
    "area_subdistrict": "...",
    "part_of_area": "...",
    "outage_cause": "fault" or "scheduled",
    "outage_from_date": "YYYY-MM-DD",
    "outage_from_time": "HH:MM" or "",
    "outage_to_date": "YYYY-MM-DD" or "",
    "outage_to_time": "HH:MM" or ""
  }
]}

Field rules:
- town_village: the municipality/village/community named in the text (e.g. "Ζακάκι",
  "Άγιος Αθανάσιος"), in Greek exactly as written there. Required -- if you cannot
  find one, omit that outage from the array entirely.
- area_subdistrict: a specific street name if one is given (e.g. "οδό Σαρωνικού"),
  else "".
- part_of_area: any other descriptive qualifier that isn't a street name (e.g. a
  named neighbourhood or a "bounded by streets X, Y, Z" description), else "".
- outage_cause: "fault" if the text mentions a fault/breakdown (βλάβη), otherwise
  "scheduled".
- Dates: the text often gives relative days ("σήμερα" = today, "αύριο" = tomorrow,
  weekday names) instead of absolute dates. You are given the announcement's
  publish date -- use it as "today" to resolve these into absolute YYYY-MM-DD dates.
  If no restoration date/time is given at all (outage open-ended / crews still
  working), leave outage_to_date and outage_to_time as "".
- Times: only fill *_time if the text states an actual clock time (e.g. "μέχρι τις
  18.00"). Vague words like "σύντομα" (soon) are not clock times -- leave "".
- Most announcements describe exactly one location; only include multiple objects
  in "outages" if the text clearly names multiple distinct places.

Example 1:
Announcement publish date: 2026-05-24
Title: Διακοπή νερού λόγω βλάβης σε κεντρικό αγωγό ύδρευσης σε περιοχή της Κοινότητας Ζακακίου
Body: Ενημερώνεται το κοινό ότι λόγω βλάβης σε κεντρικό αγωγό ύδρευσης, έχει διακοπεί \
η υδροδότηση σε μεγάλη περιοχή της Κοινότητας Ζακακίου. Επηρεάζεται η περιοχή που \
περικλείεται από τις εξής οδούς: ανατολικά της οδού Σαρωνικού. Τα συνεργεία μας \
βρίσκονται ήδη εκεί για την αποκατάσταση της βλάβης. Οι εργασίες επιδιόρθωσης \
αναμένεται να ολοκληρωθούν μέχρι τις 18.00 σήμερα, 24/5/2026.
Output: {"outages": [{"town_village": "Ζακάκι", "area_subdistrict": "Σαρωνικού", \
"part_of_area": "", "outage_cause": "fault", "outage_from_date": "2026-05-24", \
"outage_from_time": "", "outage_to_date": "2026-05-24", "outage_to_time": "18:00"}]}

Example 2:
Announcement publish date: 2026-07-14
Title: Ανακοίνωση Αρ. 171/2026 - Προγραμματισμένη διακοπή νερού στον Άγιο Αθανάσιο
Body: Ενημερώνεται το κοινό ότι αύριο, Τρίτη 15/7/2026, θα πραγματοποιηθεί \
προγραμματισμένη διακοπή υδροδότησης στην περιοχή του Δήμου Αμαθούντας, στην οδό \
Μεσολογγίου, λόγω εργασιών συντήρησης. Η υδροδότηση αναμένεται να αποκατασταθεί \
το ίδιο βράδυ.
Output: {"outages": [{"town_village": "Άγιος Αθανάσιος", "area_subdistrict": \
"Μεσολογγίου", "part_of_area": "Δήμος Αμαθούντας", "outage_cause": "scheduled", \
"outage_from_date": "2026-07-15", "outage_from_time": "", "outage_to_date": \
"2026-07-15", "outage_to_time": ""}]}
"""


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def call_llm(client: httpx.Client, item: dict) -> Optional[list]:
    published_date = (item.get("published") or "")[:10]
    user_content = (
        f"Announcement publish date: {published_date or 'unknown'}\n"
        f"Title: {item['title']}\n"
        f"Body: {item['body']}"
    )
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
        print(f"  llm call failed for {item['permalink']}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None

    content = r.json().get("message", {}).get("content", "")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        print(f"  llm returned non-JSON for {item['permalink']}: {content[:200]!r}",
              file=sys.stderr)
        return None

    outages = data.get("outages")
    if not isinstance(outages, list) or not outages:
        print(f"  llm found no outages for {item['permalink']}", file=sys.stderr)
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


def to_payloads(outages: list) -> List[dict]:
    payloads = []
    for o in outages:
        if not isinstance(o, dict):
            continue
        town = clean(o.get("town_village", ""))
        if not town:
            continue  # unusable without a place to resolve against
        cause = o.get("outage_cause")
        payloads.append({
            "source": "eoa_lemesos",
            "district": DISTRICT,
            "town_village": town,
            "area_subdistrict": clean(o.get("area_subdistrict", "")),
            "part_of_area": clean(o.get("part_of_area", "")),
            "outage_type": "water",
            "outage_cause": cause if cause in ("fault", "scheduled") else "scheduled",
            "outage_from": localize(o.get("outage_from_date", ""), o.get("outage_from_time", "")),
            "outage_to": localize(o.get("outage_to_date", ""), o.get("outage_to_time", "")),
        })
    return payloads


def push(url: str, token: str, payloads: List[dict]) -> None:
    headers = {"X-Ingest-Token": token, "X-Scraper-Source": "eoa_lemesos"}
    r = httpx.post(url, json=payloads, headers=headers, timeout=30)
    r.raise_for_status()
    print(
        f"[{now_str()}] pushed {len(payloads)} rows -> {r.status_code} {r.text[:200]}",
        file=sys.stderr,
    )
    print(json.dumps(payloads, ensure_ascii=False))


def load_seen() -> set:
    if SEEN_STORE.exists():
        try:
            return set(json.loads(SEEN_STORE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def save_seen(seen: set) -> None:
    SEEN_STORE.write_text(json.dumps(sorted(seen)))


def get_new_announcements(client: httpx.Client, pages: int) -> List[dict]:
    listed = fetch_pages(client, pages)
    seen = load_seen()
    fresh = [it for it in listed if it["permalink"] not in seen]

    out = []
    for it in fresh:
        html = fetch(client, it["permalink"], referer=LISTING)
        if html is None:
            continue  # left unseen -- retried next cycle
        detail = parse_detail(html)
        date_raw = detail["date_raw"] or it["listing_date_raw"]
        published = parse_listing_date(date_raw)
        out.append({
            "permalink": it["permalink"],
            "title": it["title"],
            "published": f"{published}T00:00:00" if published else None,
            "published_raw": date_raw,
            "body": detail["body"],
        })
        time.sleep(1)
    return out


def cycle(pages: int, ingest_url: str, ingest_token: str) -> None:
    with httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=TIMEOUT) as client:
        fresh = get_new_announcements(client, pages)

    if not fresh:
        print(f"[{now_str()}] 0 new announcement(s)", file=sys.stderr)
        return
    print(fresh)
    payloads: List[dict] = []
    done_ids: List[str] = []
    with httpx.Client(timeout=LLM_TIMEOUT) as llm_client:
        for it in fresh:
            outages = call_llm(llm_client, it)
            if outages is None:
                continue  # left unseen -- retried next cycle
            batch = to_payloads(outages)
            if not batch:
                print(f"  {it['permalink']}: no usable location extracted, skipping",
                      file=sys.stderr)
                continue
            payloads += batch
            done_ids.append(it["permalink"])

    if not payloads:
        print(f"[{now_str()}] {len(fresh)} new announcement(s), 0 extracted successfully",
              file=sys.stderr)
        return

    push(ingest_url, ingest_token, payloads)

    # Only mark extracted+pushed posts as seen -- if push() raised, we never
    # reach here, and the whole batch (including LLM successes) retries next
    # cycle rather than being lost.
    seen = load_seen()
    seen.update(done_ids)
    save_seen(seen)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="single pass, then exit")
    ap.add_argument("--interval", type=int, default=900, help="loop gap seconds")
    ap.add_argument("--pages", type=int, default=1,
                     help="listing pages to walk per cycle (10 posts/page); "
                          "use a higher value once for backfill")
    args = ap.parse_args()

    ingest_url = os.environ.get("INGEST_URL")
    ingest_token = os.environ.get("INGEST_TOKEN")
    if not ingest_url or not ingest_token:
        print("set INGEST_URL and INGEST_TOKEN", file=sys.stderr)
        sys.exit(1)

    if args.once:
        cycle(args.pages, ingest_url, ingest_token)
        return

    while True:
        try:
            cycle(args.pages, ingest_url, ingest_token)
        except Exception as e:
            print(f"cycle error: {e}", file=sys.stderr)  # never die; retry next tick
        time.sleep(args.interval + random.uniform(0, args.interval * 0.1))


if __name__ == "__main__":
    main()
