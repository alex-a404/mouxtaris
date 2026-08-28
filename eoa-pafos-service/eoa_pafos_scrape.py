# EOA Pafos (eoap.org.cy) water-interruption announcement scraper.

import argparse, json, os, random, re, sys, time
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

ROOT = "https://eoap.org.cy/"
FEED = "https://eoap.org.cy/category/diakopes-ydrodotisis/feed/"
DISTRICT = "Pafos"  # eoap.org.cy is the Pafos District Local Government Organisation

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
# CPU inference is slow, and a cold model load (weights into RAM) alone can
# take minutes -- keep the read timeout generous and configurable.
LLM_TIMEOUT = httpx.Timeout(
    connect=5.0, read=float(os.environ.get("OLLAMA_TIMEOUT", "600")), write=10.0, pool=10.0
)
# Keep the model resident between calls: Ollama's default keep_alive (5m) is
# shorter than this script's default --interval (15m), so without this every
# poll cycle would pay the cold-load cost again, not just the first one.
OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "30m")

TZ = ZoneInfo("Asia/Nicosia")
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}

WS = re.compile(r"\s+")
GUID_ID_RE = re.compile(r"[?&]p=(\d+)")
SEEN_STORE = Path(os.environ.get("SEEN_STORE", Path(__file__).with_name("eoa_pafos_seen.json")))


def clean(s: str) -> str:
    return WS.sub(" ", s or "").strip()


BOILERPLATE_RE = re.compile(r"εμφανίστηκε πρώτα στο", re.I)


def html_to_text(html: str) -> str:
    """content:encoded is block-level HTML; keep paragraph breaks, drop tags.

    Every item's body has a "Το άρθρο X εμφανίστηκε πρώτα στο Y" (the post
    X first appeared on Y) paragraph auto-appended by a feed plugin -- it's
    not part of the announcement, so drop it.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    parts = [clean(p.get_text(" ")) for p in soup.find_all(["p", "li"])]
    parts = [p for p in parts if p and not BOILERPLATE_RE.search(p)]
    return "\n".join(parts) if parts else clean(soup.get_text(" "))


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


def parse_feed(xml_text: str) -> List[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        title = clean(item.findtext("title"))
        link = clean(item.findtext("link"))
        guid = clean(item.findtext("guid") or "")
        m = GUID_ID_RE.search(guid)
        post_id = int(m.group(1)) if m else None

        pub_raw = clean(item.findtext("pubDate"))
        published = None
        if pub_raw:
            try:
                published = parsedate_to_datetime(pub_raw).astimezone(TZ).isoformat()
            except (TypeError, ValueError):
                pass

        body_html = item.findtext("content:encoded", namespaces=NS)
        if not body_html:
            body_html = item.findtext("description")
        body = html_to_text(body_html)

        if post_id is None or not link:
            continue  # unusable without a stable id / permalink

        out.append({
            "source": "eoa_pafos",
            "district": DISTRICT,
            "post_id": post_id,
            "title": title,
            "permalink": link,
            "published": published,
            "published_raw": pub_raw,
            "body": body,
        })
    return out


def fetch_pages(client: httpx.Client, pages: int) -> List[dict]:
    fetch(client, ROOT)  # warm-up: acquire cookies
    out = []
    for page in range(1, pages + 1):
        url = FEED if page == 1 else f"{FEED}?paged={page}"
        xml_text = fetch(client, url, referer=ROOT)
        if xml_text is None:
            break
        items = parse_feed(xml_text)
        if not items:
            break  # ran past the last page
        out += items
        time.sleep(1)
    return out


SYSTEM_PROMPT = """You extract structured water-outage data from Greek announcements \
published by the Pafos District Local Government Organisation (EOA Pafos).

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
- town_village: the municipality/village named in the text (e.g. "Πέγεια", "Γεροσκήπου"),
  in Greek exactly as written there. Required -- if you cannot find one, omit that
  outage from the array entirely.
- area_subdistrict: a specific street/neighbourhood name if one is given (e.g. "οδό
  Αγίας Ειρήνης"), else "".
- part_of_area: any other descriptive qualifier that isn't a street name (e.g. "περιοχή
  πίσω από το παλιό ΚΕΝ"), else "".
- outage_cause: "fault" if the text mentions a fault/breakdown (βλάβη), otherwise
  "scheduled".
- Dates: the text often gives relative days ("σήμερα" = today, "αύριο" = tomorrow,
  weekday names) instead of absolute dates. You are given the announcement's
  publish date -- use it as "today" to resolve these into absolute YYYY-MM-DD dates.
  If no restoration date is given at all (outage open-ended / "until further notice"),
  leave outage_to_date and outage_to_time as "".
- Times: only fill *_time if the text states an actual clock time or the
  announcement's own "Ημερομηνία ανακοίνωσης" timestamp is the best available
  anchor for outage_from_time. Vague words like "πρωί" (morning) are not clock
  times -- leave the time field "" for those.
- Most announcements describe exactly one location; only include multiple objects
  in "outages" if the text clearly names multiple distinct places.

Example 1:
Announcement publish date: 2026-07-31
Title: Διακοπή Yδροδότησης – Δήμος Ακάμα
Body: Ενημερώνουμε το κοινό ότι σήμερα, Παρασκευή 31/07/2026, υπάρχει διακοπή \
υδροδότησης στην Πέγεια, στην οδό Αγίας Ειρήνης. Η υδροδότηση αναμένεται να \
επανέλθει αύριο, Σάββατο 01/08/2026.
Output: {"outages": [{"town_village": "Πέγεια", "area_subdistrict": "Αγίας Ειρήνης", \
"part_of_area": "", "outage_cause": "scheduled", "outage_from_date": "2026-07-31", \
"outage_from_time": "", "outage_to_date": "2026-08-01", "outage_to_time": ""}]}

Example 2:
Announcement publish date: 2026-07-26
Title: Διακοπή Yδροδότησης – Δήμος Ιεροκηπίας
Body: Ενημερώνουμε το κοινό ότι σήμερα, Κυριακή 26/07/2026, υπάρχει διακοπή \
υδροδότησης στην Γεροσκήπου λόγω βλάβης. Η διακοπή επηρεάζει την περιοχή πίσω \
από το παλιό ΚΕΝ Γεροσκήπου. Η βλάβη θα επιδιορθωθεί αύριο πρωί. \
Ημερομηνία ανακοίνωσης: 26/07/2026, 14:56
Output: {"outages": [{"town_village": "Γεροσκήπου", "area_subdistrict": "", \
"part_of_area": "περιοχή πίσω από το παλιό ΚΕΝ Γεροσκήπου", "outage_cause": "fault", \
"outage_from_date": "2026-07-26", "outage_from_time": "14:56", \
"outage_to_date": "2026-07-27", "outage_to_time": ""}]}
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
        print(f"  llm call failed for post {item['post_id']}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return None

    content = r.json().get("message", {}).get("content", "")
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        print(f"  llm returned non-JSON for post {item['post_id']}: {content[:200]!r}",
              file=sys.stderr)
        return None

    outages = data.get("outages")
    if not isinstance(outages, list) or not outages:
        print(f"  llm found no outages for post {item['post_id']}", file=sys.stderr)
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
            "source": "eoa_pafos",
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
    headers = {"X-Ingest-Token": token, "X-Scraper-Source": "eoa_pafos"}
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


def get_new_announcements(pages: int) -> List[dict]:
    with httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=TIMEOUT) as client:
        items = fetch_pages(client, pages)

    seen = load_seen()
    return [it for it in items if it["post_id"] not in seen]


def cycle(pages: int, ingest_url: str, ingest_token: str) -> None:
    fresh = get_new_announcements(pages)
    if not fresh:
        print(f"[{now_str()}] 0 new announcement(s)", file=sys.stderr)
        return
    print(fresh)
    payloads: List[dict] = []
    done_ids: List[int] = []
    with httpx.Client(timeout=LLM_TIMEOUT) as llm_client:
        for it in fresh:
            outages = call_llm(llm_client, it)
            if outages is None:
                continue  # left unseen -- retried next cycle
            batch = to_payloads(outages)
            if not batch:
                print(f"  post {it['post_id']}: no usable location extracted, skipping",
                      file=sys.stderr)
                continue
            payloads += batch
            done_ids.append(it["post_id"])

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
                     help="feed pages to walk per cycle (10 posts/page); "
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
