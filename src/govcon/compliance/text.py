"""Deterministic text helpers shared by the compliance modules.

Nothing here calls a model. These functions parse only what the source text
states; an unparsed value is absent rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+(?:[.-][a-z0-9]+)*")
_STOPWORDS = frozenset(
    """a an the and or of to in on for by with within from at as is are be been being this that these those it its
    shall must will may should can all any each other such than then there their they we our you your which who
    not no per into upon under over after before if when where while also only more most same""".split()
)

WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
}

MANDATORY_MARKERS = re.compile(
    r"\b(shall|must|is required|are required|required to|will be rejected|will not be considered|"
    r"mandatory|no later than|is mandatory|failure to\b)",
    re.IGNORECASE,
)
_GOVERNMENT_SUBJECT = re.compile(r"^\s*(the\s+)?(government|contracting officer|agency)\b[^.]{0,40}\b(shall|will|may)\b", re.IGNORECASE)
_NONRESPONSIVE = re.compile(
    r"(non-?responsive|will be rejected|will not be considered|ineligible for award|may be rejected|"
    r"shall be rejected|will be eliminated)",
    re.IGNORECASE,
)

_TYPE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("amendment_acknowledgment", re.compile(r"acknowledg\w*[^.]{0,60}amendment|amendment[^.]{0,60}acknowledg", re.I)),
    ("signature", re.compile(r"\b(sign(ed|ature)?|countersign)\b", re.I)),
    ("page_limit", re.compile(r"\bpages?\b[^.]{0,40}(limit|exceed|maximum)|(limit|exceed|maximum|no more than)[^.]{0,40}\bpages?\b", re.I)),
    ("country_of_origin", re.compile(r"country of origin|buy american|domestic end product|specialty metals|trade agreements|berry amendment|place of manufacture", re.I)),
    ("cybersecurity", re.compile(r"nist sp 800-171|cyber|covered defense information|cmmc|safeguard", re.I)),
    ("set_aside", re.compile(r"set-?aside|small business|sdvosb|hubzone|8\(a\)|wosb", re.I)),
    ("certification", re.compile(r"certif\w+|iso 9001|registration in sam|sam registration|system for award management", re.I)),
    ("representation", re.compile(r"representation|reps and certs", re.I)),
    ("past_performance", re.compile(r"past[- ]performance|references?\b", re.I)),
    ("pricing", re.compile(r"\bpric(e|es|ing)\b|clin|unit cost|price schedule|bid schedule", re.I)),
    ("delivery", re.compile(r"\bdeliver(y|ed)?\b|\bf\.?o\.?b\b|lead time|ship(ping|ment)?\b|packag|marking|label", re.I)),
    ("formatting", re.compile(r"\b(pdf|docx|xlsx|file (name|format|type|size)|font|margin|format)\b", re.I)),
    ("submission", re.compile(r"\b(submi(t|ssion|tted)|offers? (are )?due|quotes? (are )?due|proposals? (are )?due|received by|e-?mail(ed)? to|portal)\b", re.I)),
    ("technical", re.compile(r"\b(specification|drawing|conform|mil-std|mil-spec|astm|performance|capabilit)", re.I)),
    ("administrative", re.compile(r"\b(form|sf[- ]?\d+|dd[- ]?\d+|cage|uei)\b", re.I)),
)

_CRITICAL_TYPES = frozenset({"signature", "amendment_acknowledgment", "set_aside", "certification", "representation", "country_of_origin"})
_HIGH_TYPES = frozenset({"delivery", "pricing", "page_limit", "cybersecurity", "past_performance", "technical", "administrative"})


def normalize_ws(value: str | None) -> str:
    return _WS.sub(" ", value or "").strip()


def tokens(value: str | None) -> set[str]:
    return {t for t in _TOKEN.findall((value or "").lower()) if t.isdigit() or (t not in _STOPWORDS and len(t) > 1)}


def numbers(value: str | None) -> set[str]:
    found = set(re.findall(r"\b\d+(?:\.\d+)?\b", (value or "").replace(",", "")))
    lowered = (value or "").lower()
    for word, number in WORD_NUMBERS.items():
        if re.search(rf"\b{word}\b", lowered):
            found.add(str(number))
    return found


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(inner: set[str], outer: set[str]) -> float:
    if not inner:
        return 0.0
    return len(inner & outer) / len(inner)


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences and keep table/bullet lines as their own units."""
    units: list[str] = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "\t" in stripped or stripped.startswith(("-", "*", "•")) or stripped.startswith("[Table"):
            units.append(normalize_ws(stripped.replace("\t", " | ")))
            continue
        units.append(stripped)
    joined: list[str] = []
    buffer = ""
    for unit in units:
        if " | " in unit or unit.startswith(("-", "*", "•", "[")):
            if buffer:
                joined.append(buffer)
                buffer = ""
            joined.append(unit)
            continue
        buffer = f"{buffer} {unit}".strip() if buffer else unit
    if buffer:
        joined.append(buffer)
    sentences: list[str] = []
    for block in joined:
        if " | " in block:
            sentences.append(block)
            continue
        for part in re.split(r"(?<=[.!?;])\s+(?=[A-Z(\d])", block):
            part = normalize_ws(part)
            if len(part) >= 12:
                sentences.append(part)
    return sentences


def is_mandatory_language(sentence: str) -> bool:
    return bool(MANDATORY_MARKERS.search(sentence))


def mandatory_flag(sentence: str) -> bool | None:
    """True for offeror obligations; None when only the Government is the subject."""
    if _GOVERNMENT_SUBJECT.search(sentence):
        return None
    return True if is_mandatory_language(sentence) else None


def classify_type(text: str) -> str:
    for name, pattern in _TYPE_RULES:
        if pattern.search(text or ""):
            return name
    return "other"


def estimate_severity(text: str, requirement_type: str | None) -> str:
    if _NONRESPONSIVE.search(text or ""):
        return "critical"
    if requirement_type == "submission" and re.search(r"due|no later than|deadline|received by", text or "", re.I):
        return "critical"
    if requirement_type in _CRITICAL_TYPES:
        return "critical"
    if requirement_type in _HIGH_TYPES:
        return "high"
    if requirement_type in {"formatting", "submission"}:
        return "medium"
    return "medium"


_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE = re.compile(rf"\b((?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}/\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}})\b", re.I)
_TIME = re.compile(r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2}|\d{4}\s*hours?)\b", re.I)
_TZ = re.compile(r"\b(EST|EDT|ET|CST|CDT|CT|MST|MDT|MT|PST|PDT|PT|UTC|GMT|Z|Eastern|Central|Mountain|Pacific|local time)\b(?:\s+time)?", re.I)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_FORM = re.compile(r"\b(SF|DD|OF)[- ]?(\d{2,4}[A-Z]?)\b")
_CLIN_QTY = re.compile(r"\bCLIN\s*(\d{4}[A-Z]{0,2})\b[^.\n]{0,80}?\b(?:qty|quantity)\s*(?:of|:|=)?\s*([\d,]+)", re.I)
_CLIN_QTY_TABLE = re.compile(r"^\s*(\d{4}[A-Z]{0,2})\s*\|[^|\n]*\|\s*([\d,]+)\s*(?:\||$)", re.I | re.M)
_DELIVERY_DAYS = re.compile(
    r"(?:deliver\w*|delivery|ship\w*)[^.]{0,80}?\b(?:within|in|no later than|nlt)\s+(\d+|[a-z]+)\s*(?:\((\d+)\)\s*)?(calendar|business|working)?\s*days",
    re.I,
)
_DAYS_ARO = re.compile(r"\b(\d+)\s*(calendar|business|working)?\s*days\s*(?:after|from)\s*(?:date of )?(?:award|receipt of order|aro|contract award)", re.I)
_PAGE_LIMIT = re.compile(
    r"(?:not (?:to )?exceed|shall not exceed|no more than|maximum of|limited to|limit of)\s+(\d+|[a-z]+)\s*(?:\((\d+)\)\s*)?(?:single-sided\s+|double-sided\s+)?pages?"
    r"|\b(\d+)[- ]page\s+(?:limit|maximum)",
    re.I,
)
_FILE_TYPES = re.compile(r"\b(PDF|DOCX|XLSX|Word|Excel)\b(?:\s+(?:format|files?|documents?))", re.I)
_FILE_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(MB|megabytes?)\b", re.I)
_COUNT_NOUN = re.compile(
    r"\b(?:provide|submit|include|list|furnish)\s+(?:at least|a minimum of|no fewer than)?\s*(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:\(\d+\)\s+)?"
    r"(?:[a-z-]+\s+){0,2}?(references?|certificates?|examples?|contracts?|samples?|resumes?)\b",
    re.I,
)
_PORTAL = re.compile(r"\b(PIEE|SAM\.gov|DIBBS|FedConnect|eBuy|Procurement Integrated Enterprise Environment)\b", re.I)


def _as_int(raw: str | None) -> int | None:
    if raw is None:
        return None
    raw = raw.strip().lower()
    if raw.isdigit():
        return int(raw)
    return WORD_NUMBERS.get(raw)


def extract_key_values(text: str) -> dict:
    """Parse explicit, machine-checkable facts from requirement or source text."""
    text = text or ""
    values: dict = {}
    match = _DELIVERY_DAYS.search(text)
    if match:
        days = _as_int(match.group(2)) or _as_int(match.group(1))
        if days is not None:
            values["delivery_days"] = days
            if match.group(3):
                values["delivery_day_basis"] = match.group(3).lower()
    elif (match := _DAYS_ARO.search(text)) and re.search(r"deliver|ship", text, re.I):
        values["delivery_days"] = int(match.group(1))
        if match.group(2):
            values["delivery_day_basis"] = match.group(2).lower()
    match = _PAGE_LIMIT.search(text)
    if match:
        limit = _as_int(match.group(2)) or _as_int(match.group(1)) or _as_int(match.group(3))
        if limit is not None:
            values["page_limit"] = limit
    if re.search(r"\b(due|received|submitted|no later than|deadline|closing)\b", text, re.I):
        date = _DATE.search(text)
        if date:
            values["response_deadline_date"] = normalize_ws(date.group(1))
            time = _TIME.search(text)
            if time:
                values["response_deadline_time"] = normalize_ws(time.group(1))
            tz = _TZ.search(text)
            if tz:
                values["deadline_timezone"] = tz.group(1).upper() if len(tz.group(1)) <= 3 else tz.group(1).title()
    email = _EMAIL.search(text)
    if email and re.search(r"\b(submit|send|e-?mail|deliver)\w*\b", text, re.I):
        values["recipient_email"] = email.group(0).lower()
    forms = sorted({f"{m.group(1).upper()} {m.group(2).upper()}" for m in _FORM.finditer(text)})
    if forms:
        values["forms"] = forms
    clin_quantities = {m.group(1).upper(): int(m.group(2).replace(",", "")) for m in _CLIN_QTY.finditer(text)}
    clin_quantities.update({m.group(1).upper(): int(m.group(2).replace(",", "")) for m in _CLIN_QTY_TABLE.finditer(text)})
    if clin_quantities:
        values["clin_quantities"] = clin_quantities
    file_types = sorted({m.group(1).upper().replace("WORD", "DOCX").replace("EXCEL", "XLSX") for m in _FILE_TYPES.finditer(text)})
    if file_types:
        values["allowed_file_types"] = file_types
    size = _FILE_SIZE.search(text)
    if size and re.search(r"\b(size|exceed|larger|limit)\b", text, re.I):
        values["max_file_size_mb"] = float(size.group(1))
    count = _COUNT_NOUN.search(text)
    if count:
        n = _as_int(count.group(1))
        if n is not None:
            values["required_count"] = n
            values["count_noun"] = count.group(2).lower().rstrip("s")
    portal = _PORTAL.search(text)
    if portal and re.search(r"\bsubmi", text, re.I):
        values["submission_portal"] = portal.group(1)
    if re.search(r"acknowledg\w*[^.]{0,60}amendment", text, re.I):
        numbers_found = re.findall(r"\b(?:amendment\s+(?:no\.?\s*)?)?(\d{4})\b", text, re.I)
        if numbers_found:
            values["amendments_to_acknowledge"] = sorted(set(numbers_found))
    return values


@dataclass(frozen=True)
class ClauseReference:
    family: str
    number: str
    date_text: str | None
    modifier: str | None
    raw: str


_CLAUSE = re.compile(
    r"\b(?:(FAR|DFARS|DLAD)\s+)?((?:252|5452|52)\.\d{3}-\d{1,4})\b"
    r"(?:[^\n(]{0,120}?\((?P<date>(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\.?\s+\d{4})\))?"
    r"(?P<mod>[^\n]{0,20}?\b(?:ALT(?:ERNATE)?\s+[IVX]+|DEVIATION(?:\s+\d{4}-O\d+)?)\b)?",
    re.IGNORECASE,
)


def find_clause_references(text: str) -> list[ClauseReference]:
    found: dict[tuple[str, str], ClauseReference] = {}
    for match in _CLAUSE.finditer(text or ""):
        number = match.group(2)
        explicit = (match.group(1) or "").upper()
        if number.startswith("252."):
            family = "DFARS"
        elif number.startswith("5452."):
            family = "DLAD"
        elif explicit in {"DLAD"} or re.match(r"52\.\d{3}-9\d{3}$", number):
            family = "DLAD"
        else:
            family = "FAR"
        date_text = match.group("date")
        modifier = normalize_ws(match.group("mod")).upper() if match.group("mod") else None
        key = (family, number)
        existing = found.get(key)
        if existing is None or (date_text and not existing.date_text) or (modifier and not existing.modifier):
            found[key] = ClauseReference(family, number, date_text.upper() if date_text else None, modifier, normalize_ws(match.group(0)))
    return list(found.values())


def quote_in_text(quote: str | None, text: str | None) -> bool:
    """True when the quote is present in the text (whitespace/case-insensitive, or ≥90% token containment)."""
    if not quote or not text:
        return False
    q = normalize_ws(quote).lower()
    t = normalize_ws(text).lower()
    if q and q in t:
        return True
    q_tokens = tokens(q)
    if len(q_tokens) < 5:
        return False
    return containment(q_tokens, tokens(t)) >= 0.9
