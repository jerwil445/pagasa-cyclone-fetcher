"""Read-only PAGASA cyclone importing. No ESP32/alert service dependencies."""
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests

SOURCE_URL = 'https://www.pagasa.dost.gov.ph/tropical-cyclone/severe-weather-bulletin'
ALERT_TYPE = 'OFFICIAL TROPICAL CYCLONE BULLETIN'
SOURCE = 'PAGASA Official Website'
PH = timezone(timedelta(hours=8))
VOID = {'br', 'img', 'hr', 'input', 'meta', 'link', 'source', 'wbr', 'area', 'embed'}
BLOCK = {'div', 'p', 'br', 'li', 'h3', 'h4', 'h5', 'td', 'th', 'tr', 'dt', 'dd'}


def official_url(url):
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    return (parts.scheme == 'https' and not parts.username and not parts.password
            and parts.port in (None, 443)
            and (host == 'pagasa.dost.gov.ph' or host.endswith('.pagasa.dost.gov.ph')))


def fetch_page():
    url = SOURCE_URL
    for _ in range(4):
        if not official_url(url):
            raise ValueError('Non-official PAGASA redirect blocked.')
        with requests.get(url, timeout=(5, 20), allow_redirects=False, stream=True,
                          headers={'User-Agent': 'FloodSystem/1.0 PAGASA cyclone reader'}) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get('Location', ''))
                continue
            response.raise_for_status()
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > 2_000_000:
                    raise ValueError('Unexpected oversized PAGASA cyclone page.')
                chunks.append(chunk)
            return b''.join(chunks).decode('utf-8', errors='replace')
    raise ValueError('Too many PAGASA redirects.')


class CyclonePage(HTMLParser):
    """Read only cyclone content, not navigation labels or script strings."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = self.scope = self.panel_depth = self.ignore = 0
        self.panels, self.headers, self.parts = [], {}, []
        self.panel = None
        self.heading = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag not in VOID:
            self.depth += 1
        if 'tropical-cyclone-weather-bulletin-page' in attrs.get('class', '').split():
            self.scope = self.depth
        if tag in ('script', 'style'):
            self.ignore += 1
        if tag == 'a' and attrs.get('data-header'):
            self.headers[attrs.get('href', '').lstrip('#')] = attrs['data-header']
        if tag == 'div' and re.fullmatch(r'tcwb-\d+', attrs.get('id', '')):
            self.panel = {'id': attrs['id'], 'parts': [], 'headings': [], 'pdf_url': None}
            self.panel_depth = self.depth
        if self.panel and not self.ignore:
            if tag in BLOCK:
                self.panel['parts'].append('\n')
            if tag in ('h3', 'h5'):
                self.heading = [tag, []]
            if tag == 'a':
                link = urljoin(SOURCE_URL, attrs.get('href', ''))
                if urlsplit(link).path.lower().endswith('.pdf') and official_url(link):
                    self.panel['pdf_url'] = link
        if self.scope and tag in BLOCK:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.ignore:
            return
        if self.scope:
            self.parts.append(data)
        if self.panel:
            self.panel['parts'].append(data)
            if self.heading:
                self.heading[1].append(data)

    def handle_endtag(self, tag):
        if self.panel and not self.ignore:
            if self.heading and tag == self.heading[0]:
                self.panel['headings'].append(' '.join(''.join(self.heading[1]).split()))
                self.heading = None
            if tag in BLOCK:
                self.panel['parts'].append('\n')
            if self.depth == self.panel_depth and tag == 'div':
                self.panels.append(self.panel)
                self.panel = None
        if tag in ('script', 'style') and self.ignore:
            self.ignore -= 1
        if self.scope and tag in BLOCK:
            self.parts.append('\n')
        if tag not in VOID:
            if self.depth == self.scope:
                self.scope = 0
            self.depth = max(0, self.depth-1)


def clean(parts):
    return '\n'.join(line.strip() for line in ''.join(parts).splitlines() if line.strip())


def section(text, label, following):
    match = re.search(r'^' + label + r'\s*[:|]?\s*\n(.+?)(?=^\s*(?:' + following + r')\s*[:|]?\s*$|\Z)',
                      text, re.I | re.M | re.S)
    return match[1].strip() if match else None


def parse_page(html):
    page = CyclonePage()
    page.feed(html)
    items = []
    for panel in page.panels:
        heading = next((text for text in panel['headings'] if re.match(
            r'^(Super Typhoon|Severe Tropical Storm|Tropical Storm|Tropical Depression|Typhoon)\s', text, re.I)), '')
        storm = re.fullmatch(r'(Super Typhoon|Severe Tropical Storm|Tropical Storm|Tropical Depression|Typhoon)\s+["“\']?([A-Za-z][A-Za-z0-9 -]{1,50})["”\']?(?:\s*[({].*[)}])?', heading, re.I)
        issued = next((text for text in panel['headings'] if text.lower().startswith('issued at')), '')
        issued = re.sub(r'^Issued at\s+', '', issued, flags=re.I).replace(',', '').strip()
        if not storm:
            raise ValueError('Cyclone name/classification could not be read from the official heading.')
        try:
            issue_time = datetime.strptime(issued.upper(), '%I:%M %p %d %B %Y').replace(tzinfo=PH).astimezone(timezone.utc)
        except ValueError as exc:
            raise ValueError('Cyclone bulletin issue time is missing or unrecognized.') from exc
        text = clean(panel['parts'])
        header = page.headers.get(panel['id'], '')
        number = re.search(r'(?:#|(?:NR|NO)\.?\s*)(\d+)', header, re.I)
        position = section(text, r'Location of (?:Eye/center|Center)', r'Movement|Strength|Forecast Position|Wind Signal')
        movement = section(text, r'Movement', r'Strength|Forecast Position|Wind Signal')
        strength = section(text, r'Strength', r'Forecast Position|Wind Signal')
        affected = section(text, r'Affected Areas', r'Meteorological Condition|Impact of the Wind|Precautionary Measures')
        headline = next((value for value in panel['headings'] if value != heading and not re.match(r'Issued at|\(Valid', value, re.I)), '')
        observed = headline + '\n' + (position or '')
        # Forecast lines saying "will exit" or "OUTSIDE PAR" are not observations.
        par = r'(?:the\s+)?(?:PAR\b|Philippine Area of Responsibility\b)'
        par_status = ('OUTSIDE PAR' if re.search(r'\b(?:(?:has (?:already )?(?:exited|left)|is (?:now )?outside)\s+' + par + r'|outside\s+' + par + r')', observed, re.I)
                      else 'ENTERED PAR' if re.search(r'\b(?:has (?:already )?entered|is (?:now )?(?:inside|within))\s+' + par, observed, re.I)
                      else 'Not explicitly stated')
        items.append(dict(storm_name=storm[2].strip().upper(), classification=storm[1].title(),
            bulletin_number=number[1] if number else None, issue_time=issue_time, text=text,
            position=position, movement=movement, strength=strength, affected_areas=affected,
            par_status=par_status, next_issue=next((value for value in panel['headings'] if value.startswith('(Valid')), None),
            pdf_url=panel['pdf_url'], source_url=SOURCE_URL))
    if items:
        return items
    scope = clean(page.parts)
    if re.search(r'\bno\s+(?:active\s+)?tropical cyclone(?:\s+bulletin)?(?:s)?\s+(?:within|inside|in|is|was|issued|currently|has been issued)', scope, re.I):
        return []
    raise ValueError('PAGASA cyclone content was not recognized; this is not proof of no active cyclone.')


