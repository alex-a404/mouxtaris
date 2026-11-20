# EOA Pafos (eoap.org.cy) water-interruption announcement scraper.

import argparse, json, os, random, re, sys, time, unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import anthropic
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

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
LLM_MAX_TOKENS = 16000
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT", "120"))
LLM_MAX_ATTEMPTS = 3  # per item per process; then give up on it and move on
FALLBACK_BETA = "server-side-fallback-2026-07-01"

TZ = ZoneInfo("Asia/Nicosia")
NS = {"content": "http://purl.org/rss/1.0/modules/content/"}

WS = re.compile(r"\s+")
GUID_ID_RE = re.compile(r"[?&]p=(\d+)")
SEEN_STORE = Path(os.environ.get("SEEN_STORE", Path(__file__).with_name("eoa_pafos_seen.json")))


def clean(s: str) -> str:
    return WS.sub(" ", s or "").strip()


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("ς", "σ")


_VOWELS = set("αεηιουω")


def _stem(w: str) -> str:
    """Crude Greek stem of an already-folded word: drop a final σ/ν, then
    trailing vowels, keeping at least 3 letters. This maps the case/number
    forms of a place name onto one prefix -- Άγιος/Αγίου/Άγιο -> "αγι",
    Πολεμίδια/Πολεμιδιών -> "πολεμιδι", Πάφος/Πάφου -> "παφ" -- which a
    fixed-length prefix cannot do for short names (the 5-letter "αγιοσ"
    was its own "stem" and never matched the genitive "αγιου")."""
    if len(w) > 3 and w[-1] in "σν":
        w = w[:-1]
    while len(w) > 3 and w[-1] in _VOWELS:
        w = w[:-1]
    return w


def grounded(candidate: str, source: str) -> bool:
    """True if every word of `candidate` has a stem that actually occurs in
    `source`. Guards against the LLM inventing a place name (e.g. echoing a
    few-shot example from the prompt) instead of reading the announcement --
    uses a stem, not an exact match, so a Greek case ending (nominative vs.
    genitive) doesn't cause a false negative."""
    src = _fold(source)
    for word in re.findall(r"[^\W\d_]+", candidate):
        w = _fold(word)
        if len(w) < 2:
            continue
        if _stem(w) not in src:
            return False
    return True


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
  in Greek, in its nominative (dictionary) form. Required -- if you cannot find one,
  omit that outage from the array entirely. Never invent or guess a place name from
  general knowledge of the district, and never reuse a name from these instructions'
  examples -- only output a name that is actually written in the Title or Body.
- area_subdistrict: the NAMED sub-area of that town if the text gives one -- a
  neighbourhood/quarter ("περιοχή Χ", "ενορία Χ"), a municipal district ("Δημοτικό
  Διαμέρισμα Χ" -> "Χ"), or a village inside a merged municipality -- as a proper
  place name in nominative form. NOT a street. Only a name written in the text. Else "".
- part_of_area: the street name(s) affected, and any other descriptive qualifier that
  is not a place name (e.g. "area behind the old KEN"). If several streets are
  listed, include EVERY one, joined with ", ", not just the first.
  Write this field in Latin script, as on Cyprus road signs: transliterate every
  street name letter by letter (ELOT 743 -- "ΠΡΟΜΗΘΕΩΣ" -> "Promitheos", "Σταύρου
  Βενιζέλου" -> "Stavrou Venizelou", "Λεωφόρος Μακαρίου Γ'" -> "Makariou III Avenue")
  in Title Case, never translate a name's meaning, and translate the surrounding
  descriptive words into English ("ανατολικά της οδού Χ" -> "east of X street").
  Else "".
- outage_cause: "fault" if the text mentions a fault/breakdown (βλάβη), otherwise
  "scheduled".
- Dates: the text often gives relative days ("σήμερα" = today, "αύριο" = tomorrow,
  weekday names) instead of absolute dates. You are given the announcement's
  publish date -- use it as "today" to resolve these into absolute YYYY-MM-DD dates.
  If no restoration date is given at all (outage open-ended / "until further notice"),
  leave outage_to_date and outage_to_time as "".
- Times: fill *_time with the clock time when the text states one, or use the
  announcement's own "Ημερομηνία ανακοίνωσης" timestamp as the anchor for
  outage_from_time. When restoration is given only as a part of the day, use an
  approximate clock time: πρωί "09:00", μεσημέρι "12:00", νωρίς το απόγευμα "15:00",
  απόγευμα "17:00", βράδυ "20:00". Otherwise leave the time field "".
- One object per distinct place. An announcement can affect several
  municipalities/communities, or several named neighbourhoods of one municipality.
  Output one object for EVERY distinct (town_village, area_subdistrict) pair the
  text names, each carrying only the streets that belong to it and the same
  dates/times. Never merge two places into one object and never drop one. The
  "outages" value is always an array, even for a single place.

Example 1:
Announcement publish date: 2026-07-31
Title: Διακοπή Yδροδότησης – Δήμος Ακάμα
Body: Ενημερώνουμε το κοινό ότι σήμερα, Παρασκευή 31/07/2026, υπάρχει διακοπή \
υδροδότησης στην Πέγεια, στην οδό Αγίας Ειρήνης. Η υδροδότηση αναμένεται να \
επανέλθει αύριο, Σάββατο 01/08/2026.
Output: {"outages": [{"town_village": "Πέγεια", "area_subdistrict": "", \
"part_of_area": "Agias Eirinis", "outage_cause": "scheduled", "outage_from_date": "2026-07-31", \
"outage_from_time": "", "outage_to_date": "2026-08-01", "outage_to_time": ""}]}

Example 2:
Announcement publish date: 2026-07-26
Title: Διακοπή Yδροδότησης – Δήμος Ιεροκηπίας
Body: Ενημερώνουμε το κοινό ότι σήμερα, Κυριακή 26/07/2026, υπάρχει διακοπή \
υδροδότησης στην Γεροσκήπου λόγω βλάβης. Η διακοπή επηρεάζει την περιοχή πίσω \
από το παλιό ΚΕΝ Γεροσκήπου. Η βλάβη θα επιδιορθωθεί αύριο πρωί. \
Ημερομηνία ανακοίνωσης: 26/07/2026, 14:56
Output: {"outages": [{"town_village": "Γεροσκήπου", "area_subdistrict": "", \
"part_of_area": "area behind the old KEN of Geroskipou", "outage_cause": "fault", \
"outage_from_date": "2026-07-26", "outage_from_time": "14:56", \
"outage_to_date": "2026-07-27", "outage_to_time": ""}]}

Example 3 (two villages in one announcement -> two objects):
Announcement publish date: 2026-08-10
Title: Διακοπή Yδροδότησης – Δήμος Πόλεως Χρυσοχούς
Body: Ενημερώνουμε το κοινό ότι αύριο, Τρίτη 11/08/2026, από τις 08:00 μέχρι τις 14:00 \
θα διακοπεί η υδροδότηση στα χωριά Γουδί και Χόλι λόγω εργασιών αντικατάστασης αγωγού.
Output: {"outages": [{"town_village": "Γουδί", "area_subdistrict": "", "part_of_area": "", \
"outage_cause": "scheduled", "outage_from_date": "2026-08-11", "outage_from_time": "08:00", \
"outage_to_date": "2026-08-11", "outage_to_time": "14:00"}, {"town_village": "Χόλι", \
"area_subdistrict": "", "part_of_area": "", "outage_cause": "scheduled", \
"outage_from_date": "2026-08-11", "outage_from_time": "08:00", "outage_to_date": "2026-08-11", \
"outage_to_time": "14:00"}]}
"""

OUTAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["outages"],
    "properties": {
        "outages": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["town_village", "area_subdistrict", "part_of_area", "outage_cause",
                             "outage_from_date", "outage_from_time", "outage_to_date", "outage_to_time"],
                "properties": {
                    "town_village": {"type": "string"},
                    "area_subdistrict": {"type": "string"},
                    "part_of_area": {"type": "string"},
                    "outage_cause": {"type": "string", "enum": ["fault", "scheduled"]},
                    "outage_from_date": {"type": "string"},
                    "outage_from_time": {"type": "string"},
                    "outage_to_date": {"type": "string"},
                    "outage_to_time": {"type": "string"},
                },
            },
        }
    },
}


def anthropic_client() -> anthropic.Anthropic:
    # Credentials resolve from ANTHROPIC_API_KEY. The SDK already retries
    # connection errors, 408/409/429 and 5xx with backoff.
    return anthropic.Anthropic(timeout=LLM_TIMEOUT_S, max_retries=3)


def _create(client: anthropic.Anthropic, user_content: str):
    kwargs = dict(
        model=ANTHROPIC_MODEL,
        max_tokens=LLM_MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_content}],
        output_config={"format": {"type": "json_schema", "schema": OUTAGE_SCHEMA}},
    )
    try:
        return client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    except anthropic.BadRequestError as e:
        # Only the fallback beta is optional -- if the API ever rejects it,
        # degrade to a plain request rather than losing the extraction.
        print(f"  llm request with refusal fallback rejected ({e.message}); "
              f"retrying without it", file=sys.stderr)
        return client.beta.messages.create(**kwargs)


def _extract(client: anthropic.Anthropic, user_content: str, label: str) -> Optional[list]:
    """One Claude call. Returns the outages list ([] when the model found no
    outage in the text) or None when the call itself failed, so the caller can
    leave the item for the next cycle."""
    try:
        r = _create(client, user_content)
    except anthropic.RateLimitError as e:
        print(f"  llm rate limited for {label}: {e.message}", file=sys.stderr)
        return None
    except anthropic.APIStatusError as e:
        print(f"  llm api error {e.status_code} for {label}: {e.message}", file=sys.stderr)
        return None
    except anthropic.APIConnectionError as e:  # includes APITimeoutError
        print(f"  llm connection error for {label}: {type(e).__name__}: {e}", file=sys.stderr)
        return None

    if r.stop_reason == "refusal":
        sd = getattr(r, "stop_details", None)
        print(f"  llm refused {label}: {getattr(sd, 'category', None)} "
              f"{getattr(sd, 'explanation', '')}", file=sys.stderr)
        return None
    if r.stop_reason == "max_tokens":
        print(f"  llm hit max_tokens for {label}", file=sys.stderr)
        return None

    u = r.usage
    print(f"  llm ok {label}: model={r.model} in={u.input_tokens} "
          f"cached={getattr(u, 'cache_read_input_tokens', 0) or 0} out={u.output_tokens} "
          f"req={r._request_id}", file=sys.stderr)

    text = next((b.text for b in r.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"  llm returned non-JSON for {label}: {text[:200]!r}", file=sys.stderr)
        return None
    outages = data.get("outages")
    if not isinstance(outages, list):
        print(f"  llm returned no outages array for {label}", file=sys.stderr)
        return None
    return outages


def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def call_llm(client: anthropic.Anthropic, item: dict) -> Optional[list]:
    """Extract outages for one announcement. [] means the model read it and
    found no outage (caller marks it seen); None means try again next cycle."""
    published_date = (item.get("published") or "")[:10]
    user_content = (
        f"Announcement publish date: {published_date or 'unknown'}\n"
        f"Title: {item['title']}\n"
        f"Body: {item['body']}"
    )
    label = f"post {item['post_id']}"
    outages = _extract(client, user_content, label)
    if outages is None:
        return None
    if not outages:
        print(f"  llm found no outages for {label}", file=sys.stderr)
        return []

    source_text = f"{item['title']} {item['body']}"
    kept = []
    for o in outages:
        if not isinstance(o, dict):
            continue
        if not grounded(o.get("town_village", ""), source_text):
            print(f"  llm hallucinated ungrounded town_village {o.get('town_village')!r} "
                  f"for {item['permalink']}, dropping", file=sys.stderr)
            continue
        # area_subdistrict now drives resolution first (see mouxtaris-bot
        # resolver), so a hallucinated neighbourhood could misroute the
        # notification. Unlike town_village it is optional: blank it and
        # keep the outage, which then resolves on town_village.
        sub = o.get("area_subdistrict", "")
        if sub and not grounded(sub, source_text):
            print(f"  llm hallucinated ungrounded area_subdistrict {sub!r} "
                  f"for {item['permalink']}, blanking", file=sys.stderr)
            o["area_subdistrict"] = ""
        kept.append(o)
    if not kept:
        print(f"  llm found no grounded outages for post {item['post_id']}", file=sys.stderr)
        return None
    return kept


def localize(date_s: str, time_s: str, default_time: str = "00:00") -> str:
    date_s, time_s = clean(date_s), clean(time_s) or default_time
    if not date_s:
        return ""
    try:
        return datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ).isoformat()
    except ValueError:
        print(f"  bad date/time from llm: {date_s!r} {time_s!r}", file=sys.stderr)
        return ""


def from_time_or_now(date_s: str, time_s: str) -> str:
    """The start time to use for outage_from: the one the text gives, else --
    when the outage starts today -- the moment we are reading the announcement
    (a fault announced this morning reads as 09:25, not 00:00), else midnight."""
    time_s = clean(time_s)
    if time_s:
        return time_s
    now = datetime.now(TZ)
    return now.strftime("%H:%M") if clean(date_s) == now.date().isoformat() else "00:00"


# Generic place-type words the model sometimes keeps in front of a neighbourhood
# name ("ενορία Αγίου Δημητρίου", "περιοχή Πάνθεα", "Δημοτικό Διαμέρισμα Κάτω
# Πολεμιδιών"). The resolver token-matches the whole string against area names,
# so the extra word dilutes the score; strip it so the bare name is compared.
_SUB_PREFIX = re.compile(
    r"^(?:(?:η|το|στην|στον|στο|της|του)\s+)?"
    r"(?:ενορ[ιί]α|περιοχ[ηή]|συνοικ[ιί]α|γειτονι[αά]|δημοτικ[οό]\s+διαμ[εέ]ρισμα|δ\.?\s*δ\.?|κοιν[οό]τητα|δ[ηή]μος)"
    r"\s+(?:(?:της|του|των)\s+)?",
    re.IGNORECASE,
)


def clean_subdistrict(s: str) -> str:
    s = clean(s)
    return clean(_SUB_PREFIX.sub("", s)) or s

def already_over(outage_to_iso: str) -> bool:
    """True if outage_to is a real timestamp and it's already in the past --
    a defensive net against pushing a resolved outage as newly "created"
    (seen for real on the eoa_lefkosia source: a resolved row that the site
    never removed/hid got re-extracted and pushed 2+ days stale)."""
    if not outage_to_iso:
        return False
    try:
        return datetime.fromisoformat(outage_to_iso) < datetime.now(TZ)
    except ValueError:
        return False


def to_payloads(outages: list) -> List[dict]:
    payloads = []
    for o in outages:
        if not isinstance(o, dict):
            continue
        town = clean(o.get("town_village", ""))
        if not town:
            continue  # unusable without a place to resolve against
        outage_to = localize(o.get("outage_to_date", ""), o.get("outage_to_time", ""), "23:59")
        if already_over(outage_to):
            print(f"  skipping already-over outage for {town!r} (outage_to={outage_to})",
                  file=sys.stderr)
            continue
        cause = o.get("outage_cause")
        payloads.append({
            "source": "eoa_pafos",
            "district": DISTRICT,
            "town_village": town,
            "area_subdistrict": clean_subdistrict(o.get("area_subdistrict", "")),
            "part_of_area": clean(o.get("part_of_area", "")),
            "outage_type": "water",
            "outage_cause": cause if cause in ("fault", "scheduled") else "scheduled",
            "outage_from": localize(o.get("outage_from_date", ""),
                                    from_time_or_now(o.get("outage_from_date", ""), o.get("outage_from_time", ""))),
            # a restoration date with no time means "within that day": end of day,
            # never 00:00, which would read as restored before the outage began
            "outage_to": outage_to,
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


_llm_failures: dict = {}  # item id -> failed llm attempts in this process


def cycle(pages: int, ingest_url: str, ingest_token: str) -> None:
    fresh = get_new_announcements(pages)
    if not fresh:
        print(f"[{now_str()}] 0 new announcement(s)", file=sys.stderr)
        return
    print(fresh)
    payloads: List[dict] = []
    done_ids: list = []
    with anthropic_client() as llm_client:
        for it in fresh:
            key = it["post_id"]
            outages = call_llm(llm_client, it)
            if outages is None:
                # Call failed or nothing grounded: leave unseen so it is retried
                # next cycle -- but not forever, every retry is a paid request.
                n = _llm_failures[key] = _llm_failures.get(key, 0) + 1
                if n >= LLM_MAX_ATTEMPTS:
                    print(f"  post {key}: giving up after {n} failed llm attempts, marking seen",
                          file=sys.stderr)
                    done_ids.append(key)
                continue
            batch = to_payloads(outages)
            if not batch:
                print(f"  post {key}: no outage/location extracted, marking seen", file=sys.stderr)
                done_ids.append(key)
                continue
            payloads += batch
            done_ids.append(key)

    if payloads:
        # push() raises on failure, so nothing below runs and the whole batch
        # (including LLM successes) retries next cycle rather than being lost.
        push(ingest_url, ingest_token, payloads)
    else:
        print(f"[{now_str()}] {len(fresh)} new announcement(s), 0 to push", file=sys.stderr)

    # Mark seen: everything pushed, plus items that definitively had nothing to push.
    if done_ids:
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
