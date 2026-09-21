# EOA Lefkosia / NDLGO (ndlgo.org.cy) water-interruption scraper.

import argparse, json, os, random, re, sys, time, unicodedata
from datetime import date, datetime
from hashlib import sha1
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import anthropic
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

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")
LLM_MAX_TOKENS = 16000
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT", "120"))
LLM_MAX_ATTEMPTS = 3  # per item per process; then give up on it and move on
FALLBACK_BETA = "server-side-fallback-2026-07-01"

TZ = ZoneInfo("Asia/Nicosia")
WS = re.compile(r"\s+")
CACHE_STORE = Path(os.environ.get("CACHE_STORE", Path(__file__).with_name("eoa_lefkosia_cache.json")))


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


HIDDEN_CLASSES = {"elementor-hidden-desktop", "elementor-hidden-tablet", "elementor-hidden-mobile"}


def is_retired(node) -> bool:
    """NDLGO editors "retire" old entries by toggling Elementor's
    responsive-visibility widget to hidden-on-desktop/tablet/mobile instead
    of deleting the HTML, so a plain fetch still sees stale sections a
    browser never renders."""
    for _ in range(12):
        node = node.parent
        if node is None:
            return False
        if HIDDEN_CLASSES <= set(node.get("class") or []):
            return True
    return False


def parse_page(html: str) -> List[dict]:
    """Every <h4> that matches a known section is followed by a <table>;
    each <tbody> <tr> becomes one row item, keyed by a content hash since
    the site gives no stable id."""
    # html5lib (not html.parser/lxml) because at least one section's table
    # widget emits <tbody><td>...</td> rows with no <tr> wrapper -- only a
    # full HTML5 tree-construction parser recovers the implied <tr>.
    soup = BeautifulSoup(html, "html5lib")
    today = datetime.now(TZ).date()
    items = []
    for h in soup.find_all("h4"):
        heading = clean(h.get_text(" "))
        cause = section_cause(heading)
        if cause is None:
            continue
        table = h.find_next("table")
        if table is None or is_retired(table):
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
  in its nominative (dictionary) form, WITHOUT any parenthetical reference code (e.g.
  drop "(περ.21)"). Required -- if you cannot find one, omit that outage from the
  array entirely. Never invent or guess a place name from general knowledge, and never
  reuse a name from these instructions' examples -- only output a name actually
  written in the Row.
- area_subdistrict: the NAMED sub-area of that town if the row gives one -- a
  neighbourhood/quarter ("περιοχή Χ", "ενορία Χ", "συνοικία Χ") or a municipal
  district ("Δημοτικό Διαμέρισμα Χ" -> "Χ") -- as a proper place name in nominative
  form. NOT a street. Only a name written in the Row. Else "".
- part_of_area: the street name(s) affected (e.g. "Ipeirou, Stavrou Venizelou" -- if
  several, join ALL with ", "), and any other descriptive qualifier that is not a
  place name (e.g. "New Settlement and Industrial Area", "Whole village").
  Write this field in Latin script, as on Cyprus road signs: transliterate every
  street name letter by letter (ELOT 743 -- "ΠΡΟΜΗΘΕΩΣ" -> "Promitheos", "Σταύρου
  Βενιζέλου" -> "Stavrou Venizelou", "Λεωφόρος Μακαρίου Γ'" -> "Makariou III Avenue")
  in Title Case, never translate a name's meaning, and translate the surrounding
  descriptive words into English ("ανατολικά της οδού Χ" -> "east of X street").
  Else "".
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
- One object per distinct place. A row can affect several
  municipalities/communities, or several named neighbourhoods of one municipality.
  Output one object for EVERY distinct (town_village, area_subdistrict) pair the
  text names, each carrying only the streets that belong to it and the same
  dates/times. Never merge two places into one object and never drop one. The
  "outages" value is always an array, even for a single place.

Example 1:
Today's date: 2026-08-05
Row: ΔΗΜΟΣ/ΚΟΙΝΟΤΗΤΑ: Λατσιά (περ.21) | ΑΝΑΦΟΡΑ: Ενημερώνουμε οτι έχουμε κλειστά νερά στα \
ΛΑΤΣΙΑ, ΗΠΕΙΡΟΥ, ΣΤΑΥΡΟΥ ΒΕΝΙΖΕΛΟΥ λόγω βλάβης σε κεντρικό αγωγό. | ΩΡΑ ΔΙΑΚΟΠΗΣ: 05/08/26 | \
ΕΚΤΙΜΩΜΕΝΟΣ ΧΡΟΝΟΣ ΕΠΑΝΑΦΟΡΑΣ: 05/08/26
Output: {"outages": [{"town_village": "Λατσιά", "area_subdistrict": "", \
"part_of_area": "Ipeirou, Stavrou Venizelou", "outage_from_date": "2026-08-05", \
"outage_from_time": "", "outage_to_date": "2026-08-05", "outage_to_time": ""}]}

Example 2:
Today's date: 2026-06-01
Row: ΔΗΜΟΣ/ΚΟΙΝΟΤΗΤΑ: Μάμμαρι | ΕΠΗΡΕΑΖΟΜΕΝΕΣ ΟΔΟΙ/ΣΗΜΕΙΑ: Νέος Οικισμός και Βιομηχανική \
Περιοχή | ΛΟΓΟΣ ΔΙΑΚΟΠΗΣ: θα πραγματοποιηθούν εργασίες καθαρισμού υδατόπυργων που \
εξυπηρετούν τον νέο οικισμό και τη βιομηχανική περιοχή Μαμμαρίου | ΕΚΤΙΜΩΜΕΝΗ ΩΡΑ \
ΔΙΑΚΟΠΗΣ: 03/06/26 | ΕΚΤΙΜΩΜΕΝΟΣ ΧΡΟΝΟΣ ΕΠΑΝΑΦΟΡΑΣ: Εντός της ημέρας
Output: {"outages": [{"town_village": "Μάμμαρι", "area_subdistrict": "", \
"part_of_area": "New Settlement and Industrial Area", "outage_from_date": "2026-06-03", \
"outage_from_time": "", "outage_to_date": "2026-06-03", "outage_to_time": ""}]}

Example 3:
Today's date: 2026-08-26
Row: ΔΗΜΟΣ/ΚΟΙΝΟΤΗΤΑ: Αλάμπρα | ΑΝΑΦΟΡΑ: Ενημερώνουμε οτι έχουμε κλειστά νερά στην ΑΛΑΜΠΡΑ \
(περ.30) , ΟΛΟ ΤΟ ΧΩΡΙΟ λόγω βλάβης σε κεντρικό αγωγό. Εκτιμούμε ότι θα διορθωθεί μέχρι το \
μεσημέρι. | ΩΡΑ ΔΙΑΚΟΠΗΣ: 26/08/26 | ΕΚΤΙΜΩΜΕΝΟΣ ΧΡΟΝΟΣ ΕΠΑΝΑΦΟΡΑΣ: Μέχρι το μεσημέρι
Output: {"outages": [{"town_village": "Αλάμπρα", "area_subdistrict": "", \
"part_of_area": "Whole village", "outage_from_date": "2026-08-26", "outage_from_time": "", \
"outage_to_date": "2026-08-26", "outage_to_time": "12:00"}]}

Example 4 (two communities in one row -> two objects):
Today's date: 2026-07-02
Row: ΔΗΜΟΣ/ΚΟΙΝΟΤΗΤΑ: Δάλι (περ.12) | ΑΝΑΦΟΡΑ: Ενημερώνουμε οτι έχουμε κλειστά νερά στο \
ΔΑΛΙ και στο ΠΕΡΑ ΧΩΡΙΟ λόγω βλάβης στον κεντρικό αγωγό. | ΩΡΑ ΔΙΑΚΟΠΗΣ: 02/07/26 | \
ΕΚΤΙΜΩΜΕΝΟΣ ΧΡΟΝΟΣ ΕΠΑΝΑΦΟΡΑΣ: Μέχρι το απόγευμα
Output: {"outages": [{"town_village": "Δάλι", "area_subdistrict": "", "part_of_area": "", \
"outage_from_date": "2026-07-02", "outage_from_time": "", "outage_to_date": "2026-07-02", \
"outage_to_time": "17:00"}, {"town_village": "Πέρα Χωριό", "area_subdistrict": "", \
"part_of_area": "", "outage_from_date": "2026-07-02", "outage_from_time": "", \
"outage_to_date": "2026-07-02", "outage_to_time": "17:00"}]}
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
                "required": ["town_village", "area_subdistrict", "part_of_area",                              "outage_from_date", "outage_from_time", "outage_to_date", "outage_to_time"],
                "properties": {
                    "town_village": {"type": "string"},
                    "area_subdistrict": {"type": "string"},
                    "part_of_area": {"type": "string"},
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


def call_llm(client: anthropic.Anthropic, row_text: str) -> Optional[list]:
    """Extract outages for one table row. [] means the model read it and
    found no outage (cached as such); None means try again next cycle."""
    user_content = f"Today's date: {date.today().isoformat()}\nRow: {row_text}"
    outages = _extract(client, user_content, "row")
    if outages is None:
        return None
    if not outages:
        print("  llm found no outages for row", file=sys.stderr)
        return []

    kept = []
    for o in outages:
        if not isinstance(o, dict):
            continue
        if not grounded(o.get("town_village", ""), row_text):
            print(f"  llm hallucinated ungrounded town_village {o.get('town_village')!r}, "
                  f"dropping", file=sys.stderr)
            continue
        # area_subdistrict now drives resolution first (see mouxtaris-bot
        # resolver), so a hallucinated neighbourhood could misroute the
        # notification. Unlike town_village it is optional: blank it and
        # keep the outage, which then resolves on town_village.
        sub = o.get("area_subdistrict", "")
        if sub and not grounded(sub, row_text):
            print(f"  llm hallucinated ungrounded area_subdistrict {sub!r}, blanking",
                  file=sys.stderr)
            o["area_subdistrict"] = ""
        kept.append(o)
    if not kept:
        print("  llm found no grounded outages for row", file=sys.stderr)
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
    """True if outage_to is a real timestamp and it's already in the past.
    "Fault"/"Τρέχουσες Βλάβες" rows are deliberately NOT date-filtered in
    parse_page (a fault can legitimately still be open days after it
    started, with no end date given yet) -- but NDLGO doesn't reliably
    remove/hide a row once it IS resolved (is_retired only catches rows
    toggled hidden-on-desktop/tablet/mobile; a plain stale row with its own
    restoration date/time already passed slips through as still "current").
    Once the row itself states a restoration that has already happened,
    there's no ambiguity left: it's over, and pushing it would tell
    subscribers about an outage that ended days ago as if it just started
    (seen for real: Lakatamia 2026-09-16, Alampra 2026-09-18 -- both pushed
    as "created" 2+ days stale)."""
    if not outage_to_iso:
        return False
    try:
        return datetime.fromisoformat(outage_to_iso) < datetime.now(TZ)
    except ValueError:
        return False


def to_payloads(outages: list, cause: str) -> List[dict]:
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
        payloads.append({
            "source": "eoa_lefkosia",
            "district": DISTRICT,
            "town_village": town,
            "area_subdistrict": clean_subdistrict(o.get("area_subdistrict", "")),
            "part_of_area": clean(o.get("part_of_area", "")),
            "outage_type": "water",
            "outage_cause": cause,
            "outage_from": localize(o.get("outage_from_date", ""), o.get("outage_from_time", "")),
            # a restoration date with no time means "within that day": end of day,
            # never 00:00, which would read as restored before the outage began
            "outage_to": outage_to,
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


_llm_failures: dict = {}  # row hash -> failed llm attempts in this process


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
    with anthropic_client() as llm_client:
        for row in rows:
            fresh_hashes.add(row["row_hash"])
            cached = cache.get(row["row_hash"])
            if cached is None:
                outages = call_llm(llm_client, row["row_text"])
                if outages is None:
                    # Not cached, so retried next cycle (and absent from this
                    # snapshot) -- but not forever, every retry is a paid request.
                    n = _llm_failures[row["row_hash"]] = _llm_failures.get(row["row_hash"], 0) + 1
                    if n < LLM_MAX_ATTEMPTS:
                        continue
                    print(f"  row: giving up after {n} failed llm attempts, caching as empty",
                          file=sys.stderr)
                    outages = []
                for o in outages:
                    if isinstance(o, dict) and not clean(o.get("outage_from_time", "")):
                        # Stamp the start time once, at first sight: this cached
                        # row is re-pushed every cycle and its start must not
                        # drift with the clock (that would re-key it each push).
                        o["outage_from_time"] = from_time_or_now(o.get("outage_from_date", ""), "")
                cache[row["row_hash"]] = outages
                cached = outages
            payloads += to_payloads(cached, row["cause"])

    # Snapshot semantics: the Go side resolves anything missing from a push,
    # so the cache must not grow unbounded with rows that fell off the page.
    for stale in set(cache) - fresh_hashes:
        del cache[stale]
    for stale in set(_llm_failures) - fresh_hashes:
        del _llm_failures[stale]
    save_cache(cache)

    if not payloads:
        print(f"[{now_str()}] 0 active outage(s)", file=sys.stderr)

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
