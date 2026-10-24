"""PAGASA Mindanao rainfall-section parser. No DB or hardware side effects."""
import hashlib
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin

SOURCE_URL = "https://www.pagasa.dost.gov.ph/regional-forecast/minprsd"
PH = timezone(timedelta(hours=8))
RANK = {"YELLOW": 1, "ORANGE": 2, "RED": 3}
SITES = {"ampayon": "Ampayon, Butuan City", "csucc": "CSUCC, Cabadbaran City"}
COLOR = re.compile(r"(?<!\w)(YELLOW|ORANGE|RED)\s+(?:RAINFALL\s+)?WARNING(?:\s+LEVEL)?\s*[:\-]\s*", re.I)
TITLE = re.compile(r"^(?:Heavy\s+)?Rainfall\s+(?:Warning|Advisory)[^\n]*", re.I | re.M)
NO_WARNING = re.compile(r"\bno\s+(?:active\s+)?(?:heavy\s+)?rainfall\s+warning(?:s)?(?:\s+(?:is|are))?\s*(?:issued|in effect|as of|\.|$)", re.I)
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "embed"}


class RainfallSection(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []
        self.found = False
        self.ignore = 0
        self.pdf_links = []

    def handle_starttag(self, tag, attrs):
        if not self.depth:
            if dict(attrs).get("id") == "rainfalls":
                self.depth = 1
                self.found = True
            return
        if tag not in VOID:
            self.depth += 1
        if tag == 'a' and not self.ignore:
            href = dict(attrs).get('href', '')
            if href.lower().split('?')[0].endswith('.pdf'):
                self.pdf_links.append(urljoin(SOURCE_URL, href))
        if tag in {"script", "style"}:
            self.ignore += 1
        if tag in {"br", "div", "p", "li", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.depth and tag not in VOID:
            if tag in {"script", "style"} and self.ignore:
                self.ignore -= 1
            if tag in {"div", "p", "li"}:
                self.parts.append("\n")
            self.depth -= 1

    def handle_data(self, data):
        if self.depth and not self.ignore:
            self.parts.append(data)


def key(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def locations(text):
    """Parse top-level lists without treating children as independent regions."""
    result = []
    text = re.sub(r"\s+", " ", text).strip(" .:;")
    for item in re.split(r"[,;](?![^()]*\))|\band\b(?![^()]*\))", text, flags=re.I):
        item = item.strip(" #.")
        if not item:
            continue
        m = re.fullmatch(r"([^()]+?)(?:\(([^()]*)\))?", item)
        if not m:
            continue
        parent = m.group(1).strip()
        children = [v.strip(" #.") for v in re.split(r",|\band\b", m.group(2) or "", flags=re.I) if v.strip()]
        result.append({"name": parent, "areas": children})
    return result


def match_sites(areas):
    matched = set()
    butuan = {"butuan", "butuancity", "cityofbutuan"}
    cabad = {"cabadbaran", "cabadbarancity", "cityofcabadbaran"}
    for area in areas:
        name, children = key(area["name"]), {key(v) for v in area["areas"]}
        if name in butuan:
            if not children or "ampayon" in children:
                matched.add("ampayon")
        elif name in cabad:
            if not children or "csucc" in children:
                matched.add("csucc")
        elif name == "ampayon" and not children:
            matched.add("ampayon")
        elif name == "csucc" and not children:
            matched.add("csucc")
        elif name in {"caragaregion", "regionxiii", "region13"} and not children:
            matched.update(SITES)
        elif name == "agusandelnorte":
            # Butuan is administratively independent; require explicit inclusion.
            if not children or children & cabad:
                matched.add("csucc")
            if children & butuan:
                matched.add("ampayon")
        # In particular, ignore Caraga nested under DavaoOriental.
    return sorted(matched)


def coverage(text):
    text = re.split(r"\b(?:FLOODING|LANDSLIDES|The public|Meanwhile|Light to|Moderate to|Weather System|Issued at)\b",
                    text, maxsplit=1, flags=re.I)[0]
    text = re.split(r"\.(?:\s|$)", text, maxsplit=1)[0].strip()
    ambiguous = bool(re.search(r"\b(?:except|excluding|portions|parts of|nearby)\b", text, re.I))
    areas = locations(text)
    return areas, [] if ambiguous else match_sites(areas)


def parse_time(text):
    match = re.search(r"Issued\s*(?:at|:)\s*:?\s*(\d{1,2}:\d{2}\s*[AP]M)[,\s]+(\d{1,2})[,\s]+([A-Za-z]+)[,\s]+(\d{4})", text, re.I)
    if not match:
        raise ValueError("Rainfall bulletin issue time is missing or unreadable.")
    clock = re.sub(r"\s*([AP]M)$", r" \1", match[1], flags=re.I).upper()
    return datetime.strptime(f"{clock} {match[2]} {match[3]} {match[4]}", "%I:%M %p %d %B %Y").replace(tzinfo=PH)


def parse_mindanao(html):
    parser = RainfallSection()
    parser.feed(html)
    if not parser.found:
        raise ValueError("PAGASA rainfall section not found; prior bulletin retained.")
    text = "\n".join(" ".join(line.split()) for line in "".join(parser.parts).splitlines() if line.strip())
    if not text:
        raise ValueError("PAGASA rainfall section is empty.")
    titles = list(TITLE.finditer(text))
    if NO_WARNING.search(text) and not titles and not COLOR.search(text):
        return [{"bulletin_title": "No Active Rainfall Warning", "text": text,
                 "issue_time": None, "expires_at": None, "affected_locations": [],
                 "warning_levels": [], "target_levels": {}, "matched_sites": [],
                 "status": "NO_ACTIVE_WARNING", "fingerprint": hashlib.sha256(text.encode()).hexdigest()}]
    if not titles or NO_WARNING.search(text):
        raise ValueError("Ambiguous rainfall section; cannot identify a current bulletin.")
    bulletins = []
    for n, heading in enumerate(titles):
        body = text[heading.start():titles[n+1].start() if n+1 < len(titles) else len(text)].strip()
        issued = parse_time(body)
        # Local freshness cap, not a claim about official PAGASA expiry.
        expires = issued + timedelta(hours=3)
        next_time = re.search(r"next warning.*?at\s+(\d{1,2}:\d{2}\s*[AP]M)\s+(Today|Tomorrow)", body, re.I)
        if next_time:
            clock = datetime.strptime(re.sub(r"\s*([AP]M)$", r" \1", next_time[1], flags=re.I).upper(), "%I:%M %p")
            next_issue = issued.replace(hour=clock.hour, minute=clock.minute, second=0)
            if next_time[2].lower() == "tomorrow":
                next_issue += timedelta(days=1)
            if next_issue > issued:
                expires = min(expires, next_issue)
        matches = list(COLOR.finditer(body))
        if re.search(r"\b(?:lifted|cancelled|canceled|terminated|no longer)\b", body, re.I) and matches:
            raise ValueError("Cancellation wording requires review; no new automatic alarm.")
        if not matches and re.search(r"\b(?:YELLOW|ORANGE|RED)\s+(?:RAINFALL\s+)?WARNING\b", body, re.I):
            raise ValueError("Explicit warning has unsupported affected-area formatting.")
        levels, areas, targets = [], [], {}
        for i, match in enumerate(matches):
            level = match[1].upper()
            segment = body[match.end():matches[i+1].start() if i+1 < len(matches) else len(body)]
            affected, sites = coverage(segment)
            if not affected:
                raise ValueError("Warning color has no readable affected-area list.")
            areas.extend(affected)
            levels.append({"level": level, "affected_locations": affected, "matched_sites": sites})
            for site in sites:
                if RANK[level] > RANK.get(targets.get(site), 0):
                    targets[site] = level
        if not matches:
            # Informational advisories use hashtag lists; never derive a warning.
            advisory_body = re.split(r"Issued[^\n]*\n", body, maxsplit=1, flags=re.I)[-1]
            for group in re.findall(r"#([A-Za-z][A-Za-z0-9 .'-]*(?:\([^()]*\))?)", advisory_body):
                areas.extend(locations(group))
        sites = sorted(set(targets) | set(match_sites(areas)))
        status = "ACTIVE_WARNING" if matches else "INFORMATION_ONLY"
        bulletins.append({"bulletin_title": heading[0], "text": body, "issue_time": issued,
            "pdf_url": parser.pdf_links[0] if len(parser.pdf_links) == 1 else None,
            "expires_at": expires, "affected_locations": areas, "warning_levels": levels,
            "target_levels": targets, "matched_sites": sites, "status": status,
            "fingerprint": hashlib.sha256(body.encode()).hexdigest()})
    # Only the latest issue on the page can drive hardware; retain all parsed
    # bulletins as metadata at the caller if needed.
    latest = max(item["issue_time"] for item in bulletins)
    return [item for item in bulletins if item["issue_time"] == latest]
