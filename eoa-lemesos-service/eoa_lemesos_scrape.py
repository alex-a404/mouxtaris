# EOA Lemesos (eoalemesos.org.cy) water-interruption announcement scraper.
#
# Same shape as eoa_pafos_scrape.py (discovery -> LLM extraction -> push,
# dedup by a seen-store so only new content gets pushed) but eoalemesos.org.cy
# is not WordPress: no RSS feed, no wp-json. The /el/faults listing is a
# plain paginated HTML page (page 1 at /el/faults, older pages at
# /el/faults/2, /el/faults/3, ... -- past the last page it 200s with an
# empty list rather than 404ing). Each entry links to a /el/news-details/...
# permalink, which is also the only stable id available (no numeric post id
# like WP's ?p=N), and the listing only shows a truncated excerpt -- the
# full body has to be fetched from the detail page.
#
# EOA Lemesos also keeps ONE post per day and edits it in place, appending
# each new fault area (and revised restoration times) to the same permalink
# through the day. A permalink-only seen-set never notices that, so the
# store keeps a content hash per permalink and recent posts are re-fetched
# every cycle; when the hash changes the post is re-extracted and only the
# outages not already pushed for it go out (see payload_key).

import argparse, json, os, random, re, sys, time, unicodedata
from datetime import date, datetime
from hashlib import sha1
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import anthropic
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

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
LLM_MAX_TOKENS = 16000
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT", "120"))
LLM_MAX_ATTEMPTS = 3  # per item per process; then give up on it and move on
FALLBACK_BETA = "server-side-fallback-2026-07-01"

TZ = ZoneInfo("Asia/Nicosia")

WS = re.compile(r"\s+")
SEEN_STORE = Path(os.environ.get("SEEN_STORE", Path(__file__).with_name("eoa_lemesos_seen.json")))

# Seen posts listed with a date this many days old (or newer) are re-fetched
# every cycle to catch in-place edits; older posts are not edited any more.
RECHECK_DAYS = 1


def clean(s: str) -> str:
    return WS.sub(" ", s or "").strip()


def content_hash(title: str, body: str) -> str:
    return sha1(f"{clean(title)}\n{body}".encode("utf-8")).hexdigest()[:16]


def payload_key(p: dict) -> str:
    """Identity of one pushed outage: place, start date and restoration time.
    A re-extracted post pushes only the payloads whose key it has not pushed
    before, so an edit that adds one area (or moves one restoration time)
    does not re-notify the subscribers of every area the post already
    announced. Deliberately NOT part of the key: the street list (the model
    may word it slightly differently each extraction) and the start clock
    time (defaulted to "now" when the text gives none, see from_time_or_now)
    -- either would re-key an unchanged area on every edit."""
    return "|".join(clean(str(p.get(k, ""))).lower() for k in
                    ("town_village", "area_subdistrict", "outage_to")) + "|" + clean(p.get("outage_from", ""))[:10]


def is_recent(listing_date_raw: str) -> bool:
    d = parse_listing_date(listing_date_raw)
    if d is None:
        return True  # unknown date: err on the side of re-checking
    return (datetime.now(TZ).date() - date.fromisoformat(d)).days <= RECHECK_DAYS


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
  "Πολεμίδια"), in Greek, in its nominative (dictionary) form. Required -- if you
  cannot find one, omit that outage from the array entirely. Never invent or guess a
  place name from general knowledge of the district, and never reuse a name from these
  instructions' examples -- only output a name that is actually written in the
  Title or Body given to you.
- area_subdistrict: the NAMED sub-area of that town if the text gives one -- a
  neighbourhood/quarter ("περιοχή Χ", "ενορία Χ", "συνοικία Χ"), a municipal district
  ("Δημοτικό Διαμέρισμα Χ" -> "Χ"), or a village inside a merged municipality -- as a
  proper place name in nominative form (e.g. "Κάτω Πολεμίδια"). NOT a street. Only
  a name written in the text. Else "".
- part_of_area: the street name(s) affected, and any other descriptive qualifier
  that is not a place name (e.g. "east of Saronikou street", "bounded by streets
  X, Y"). Announcements often list several streets as a bullet/line-per-street block
  ("επηρεάζονται οι παρακάτω οδοί: ..." followed by one street per line) -- include
  EVERY one, joined with ", ", not just the first.
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
  If no restoration date/time is given at all (outage open-ended / crews still
  working), leave outage_to_date and outage_to_time as "".
- Times: fill *_time with the clock time when the text states one (e.g. "μέχρι τις
  18.00" -> "18:00"). When restoration is given only as a part of the day, use an
  approximate clock time: πρωί "09:00", μεσημέρι "12:00", νωρίς το απόγευμα "15:00",
  απόγευμα "17:00", βράδυ "20:00". Vague words like "σύντομα" (soon) are not times --
  leave "".
- One object per distinct place. An announcement can affect several
  municipalities/communities, or several named neighbourhoods of one municipality.
  Output one object for EVERY distinct (town_village, area_subdistrict) pair the
  text names, each carrying only the streets that belong to it and the same
  dates/times. Never merge two places into one object and never drop one. The
  "outages" value is always an array, even for a single place.

Example 1:
Announcement publish date: 2026-05-24
Title: Διακοπή νερού λόγω βλάβης σε κεντρικό αγωγό ύδρευσης σε περιοχή της Κοινότητας Ζακακίου
Body: Ενημερώνεται το κοινό ότι λόγω βλάβης σε κεντρικό αγωγό ύδρευσης, έχει διακοπεί \
η υδροδότηση σε μεγάλη περιοχή της Κοινότητας Ζακακίου. Επηρεάζεται η περιοχή που \
περικλείεται από τις εξής οδούς: ανατολικά της οδού Σαρωνικού. Τα συνεργεία μας \
βρίσκονται ήδη εκεί για την αποκατάσταση της βλάβης. Οι εργασίες επιδιόρθωσης \
αναμένεται να ολοκληρωθούν μέχρι τις 18.00 σήμερα, 24/5/2026.
Output: {"outages": [{"town_village": "Ζακάκι", "area_subdistrict": "", \
"part_of_area": "east of Saronikou street", "outage_cause": "fault", \
"outage_from_date": "2026-05-24", "outage_from_time": "", "outage_to_date": "2026-05-24", \
"outage_to_time": "18:00"}]}

Example 2 (two municipalities in one announcement -> two objects):
Announcement publish date: 2026-07-14
Title: Ανακοίνωση Αρ. 171/2026 - Προγραμματισμένη διακοπή νερού στον Άγιο Αθανάσιο και στη Μέσα Γειτονιά
Body: Ενημερώνεται το κοινό ότι αύριο, Τρίτη 15/7/2026, θα πραγματοποιηθεί \
προγραμματισμένη διακοπή υδροδότησης λόγω εργασιών συντήρησης. Στον Δήμο Αγίου \
Αθανασίου επηρεάζονται οι οδοί Μεσολογγίου και Αθηνών. Στη Μέσα Γειτονιά επηρεάζεται \
η οδός Αρχιεπισκόπου Μακαρίου Γ'. Η υδροδότηση αναμένεται να αποκατασταθεί το ίδιο βράδυ.
Output: {"outages": [{"town_village": "Άγιος Αθανάσιος", "area_subdistrict": "", \
"part_of_area": "Mesolongiou, Athinon", "outage_cause": "scheduled", \
"outage_from_date": "2026-07-15", "outage_from_time": "", "outage_to_date": "2026-07-15", \
"outage_to_time": ""}, {"town_village": "Μέσα Γειτονιά", "area_subdistrict": "", \
"part_of_area": "Archiepiskopou Makariou III", "outage_cause": "scheduled", \
"outage_from_date": "2026-07-15", "outage_from_time": "", "outage_to_date": "2026-07-15", \
"outage_to_time": ""}]}

Example 3 (municipal district -> area_subdistrict, streets -> part_of_area):
Announcement publish date: 2026-09-07
Title: Ανακοίνωση - Διακοπή νερού λόγω βλάβης σε μέρος του Δημοτικού Διαμερίσματος \
Κάτω Πολεμιδιών
Body: Ενημερώνεται το κοινό ότι, λόγω βλάβης στο υδρευτικό δίκτυο, έχει διακοπεί η \
υδροδότηση σε μέρος του Δημοτικού Διαμερίσματος Κάτω Πολεμιδιών (Δήμος Πολεμιδιών). \
Από τη βλάβη επηρεάζονται οι παρακάτω οδοί: Δρόμος αριθμός 97 Δερκυλίδας Δρόμος \
αριθμός 95 Παναγή Κουταλιανού. Τα συνεργεία μας βρίσκονται ήδη εκεί για την \
αποκατάσταση της βλάβης. Υπολογίζεται αποκατάσταση της βλάβης και της παροχής νερού \
σήμερα, 7 Σεπτεμβρίου, γύρω στις 3:00 μ.μ.
Output: {"outages": [{"town_village": "Πολεμίδια", "area_subdistrict": "Κάτω Πολεμίδια", \
"part_of_area": "Road No. 97, Derkylidas, Road No. 95, Panagi Koutalianou", \
"outage_cause": "fault", "outage_from_date": "2026-09-07", "outage_from_time": "", \
"outage_to_date": "2026-09-07", "outage_to_time": "15:00"}]}
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
    label = item['permalink']
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
        print(f"  llm found no grounded outages for {item['permalink']}", file=sys.stderr)
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
            "source": "eoa_lemesos",
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
    headers = {"X-Ingest-Token": token, "X-Scraper-Source": "eoa_lemesos"}
    r = httpx.post(url, json=payloads, headers=headers, timeout=30)
    r.raise_for_status()
    print(
        f"[{now_str()}] pushed {len(payloads)} rows -> {r.status_code} {r.text[:200]}",
        file=sys.stderr,
    )
    print(json.dumps(payloads, ensure_ascii=False))


def load_seen() -> dict:
    """permalink -> {"hash": content hash of the title+body last extracted,
    "sent": [payload keys pushed for it so far]}. Entries from the older
    list-only store come back with hash None; the next cycle fills that in
    from the live page without pushing (the post was announced when first
    seen)."""
    if SEEN_STORE.exists():
        try:
            data = json.loads(SEEN_STORE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        if isinstance(data, list):
            return {k: {"hash": None, "sent": []} for k in data}
        if isinstance(data, dict):
            return data
    return {}


def save_seen(seen: dict) -> None:
    SEEN_STORE.write_text(json.dumps(seen, ensure_ascii=False, sort_keys=True, indent=1))


def get_new_announcements(client: httpx.Client, pages: int, seen: dict) -> List[dict]:
    """Announcements that need extracting: never-seen permalinks, plus recent
    seen ones whose detail page no longer matches the content hash on record
    (edited in place). Each item carries its store entry as "prev" (None when
    new) so the caller can push only what it has not pushed before. Entries
    that still lack a hash get one here (mutating `seen`) without being
    returned."""
    listed = fetch_pages(client, pages)
    out = []
    for it in listed:
        prev = seen.get(it["permalink"])
        if prev is not None and not is_recent(it["listing_date_raw"]):
            continue
        html = fetch(client, it["permalink"], referer=LISTING)
        if html is None:
            continue  # retried next cycle
        time.sleep(1)
        detail = parse_detail(html)
        h = content_hash(it["title"], detail["body"])
        if prev is not None:
            if prev.get("hash") is None:
                prev["hash"] = h  # baseline for an entry migrated from the old store
                continue
            if prev["hash"] == h:
                continue
            print(f"  {it['permalink']}: edited since last extraction", file=sys.stderr)
        date_raw = detail["date_raw"] or it["listing_date_raw"]
        published = parse_listing_date(date_raw)
        out.append({
            "permalink": it["permalink"],
            "title": it["title"],
            "published": f"{published}T00:00:00" if published else None,
            "published_raw": date_raw,
            "body": detail["body"],
            "hash": h,
            "prev": prev,
        })
    return out


_llm_failures: dict = {}  # item id -> failed llm attempts in this process


def cycle(pages: int, ingest_url: str, ingest_token: str) -> None:
    seen = load_seen()
    with httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True, timeout=TIMEOUT) as client:
        fresh = get_new_announcements(client, pages, seen)
    if not fresh:
        print(f"[{now_str()}] 0 new/edited announcement(s)", file=sys.stderr)
        save_seen(seen)  # may carry freshly baselined hashes
        return
    print(fresh)
    payloads: List[dict] = []
    done: list = []  # (permalink, content hash, payload keys pushed) to record
    with anthropic_client() as llm_client:
        for it in fresh:
            key = it["permalink"]
            outages = call_llm(llm_client, it)
            if outages is None:
                # Call failed or nothing grounded: leave the store as is so it
                # is retried next cycle -- but not forever, every retry is a
                # paid request.
                n = _llm_failures[key] = _llm_failures.get(key, 0) + 1
                if n >= LLM_MAX_ATTEMPTS:
                    print(f"  {key}: giving up after {n} failed llm attempts, marking seen",
                          file=sys.stderr)
                    done.append((key, it["hash"], []))
                continue
            already = set((it["prev"] or {}).get("sent", []))
            batch = [p for p in to_payloads(outages) if payload_key(p) not in already]
            if not batch:
                print(f"  {key}: nothing new to push, marking seen", file=sys.stderr)
            payloads += batch
            done.append((key, it["hash"], [payload_key(p) for p in batch]))

    if payloads:
        # push() raises on failure, so nothing below runs and the whole batch
        # (including LLM successes) retries next cycle rather than being lost.
        push(ingest_url, ingest_token, payloads)
    else:
        print(f"[{now_str()}] {len(fresh)} new/edited announcement(s), 0 to push", file=sys.stderr)

    # Record: the content each pushed/settled post was extracted from, and
    # which payloads have gone out for it, so a later edit pushes only the delta.
    for key, h, keys in done:
        entry = seen.setdefault(key, {"hash": None, "sent": []})
        entry["hash"] = h
        entry["sent"] = sorted(set(entry.get("sent", [])) | set(keys))
    save_seen(seen)


def main() -> None:
    # Under systemd stdout is a pipe, so without this the payload/item dumps sit
    # in an 8 KiB buffer for days and land in the journal next to unrelated,
    # unbuffered stderr lines -- useless for reading what happened when.
    sys.stdout.reconfigure(line_buffering=True)
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
