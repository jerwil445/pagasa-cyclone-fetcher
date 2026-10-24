"""Standalone, bounded PAGASA readers. No Flask, DB, scheduler or hardware imports."""
import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urljoin, urlsplit
import requests
from services.cyclone_parser import fetch_page as cyclone_page, parse_page as parse_cyclones
from services.mindanao_parser import SOURCE_URL as MINDANAO_URL, parse_mindanao
from services.pagasa_service import PAGASA_WEATHER_URL, parse_pagasa_weather_page
from services.weather_pdf_source import official_pdf_url, download_pdf, verify_issue, PH

REGIONAL_PDF = 'https://d3l5rdm090s0mj.cloudfront.net/public-files/prsd-cms/mindanao/regional_forecast.pdf'


def fetch_html(url):
    for _ in range(4):
        p = urlsplit(url)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or not (
                p.hostname == 'pagasa.dost.gov.ph' or (p.hostname or '').endswith('.pagasa.dost.gov.ph')):
            raise ValueError('Untrusted weather page.')
        with requests.get(url, timeout=(5, 20), stream=True, allow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get('Location', '')); continue
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > 2_000_000: raise ValueError('Weather page too large.')
            return data.decode('utf-8', errors='replace')
    raise ValueError('Too many redirects.')


def record(kind, title, text, issued, source, pdf_url=None, **metadata):
    if issued and issued > datetime.now(timezone.utc): raise ValueError('Future weather issue.')
    if not text or len(text) > 150_000: raise ValueError('Invalid weather text.')
    return dict(version=1, kind=kind, title=title, text=text,
                issue_time=issued.astimezone(timezone.utc).isoformat() if issued else None,
                source_url=source, pdf_url=pdf_url, metadata=metadata)


def daily_records():
    forecast = parse_pagasa_weather_page(fetch_html(PAGASA_WEATHER_URL))
    for row in forecast['rows']:
        yield record('daily', forecast['title'], '\n'.join((
            'Weather condition: ' + row['weather_condition'], 'Caused by: ' + row['caused_by'],
            'Impacts: ' + row['impacts'])), forecast['published_at'], forecast['source_url'],
            forecast['source_url'] if official_pdf_url(forecast['source_url']) else None,
            location=row['location'])


class PDFLinks(HTMLParser):
    def __init__(self): super().__init__(); self.links = []
    def handle_starttag(self, tag, attrs):
        if tag in ('a', 'iframe', 'embed', 'object'):
            a = dict(attrs)
            for key in ('href', 'src', 'data'):
                link = urljoin(MINDANAO_URL, a.get(key, ''))
                if link.split('?')[0] == REGIONAL_PDF: self.links.append(link)


def regional_records():
    from pypdf import PdfReader
    links = PDFLinks(); links.feed(fetch_html(MINDANAO_URL))
    if not links.links: raise ValueError('Regional PDF not linked by official page.')
    url = links.links[0]
    data = download_pdf(url)
    reader = PdfReader(BytesIO(data), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 30: raise ValueError('Invalid PDF.')
    text = '\n'.join(page.extract_text() or '' for page in reader.pages)
    match = re.search(r'Issued\s+At\s*:\s*(\d{1,2}:\d{2}\s*[AP]M),?\s*(\d{1,2}\s+[A-Za-z]+),?\s*(\d{4})', text, re.I)
    if not match or not re.search(r'REGIONAL WEATHER FORECAST', text, re.I): raise ValueError('Regional issue missing.')
    issue = datetime.strptime(' '.join(match.groups()), '%I:%M %p %d %B %Y').replace(tzinfo=PH)
    title = 'PAGASA Mindanao Regional Weather Forecast - ' + issue.isoformat()
    verify_issue(data, issue, title)
    return [(record('daily', title, text, issue, MINDANAO_URL, url, location='Mindanao'), data)]


def rainfall_records():
    for item in parse_mindanao(fetch_html(MINDANAO_URL)):
        yield record('rainfall', item['bulletin_title'], item['text'], item['issue_time'], MINDANAO_URL,
            item.get('pdf_url'), status=item['status'], expires_at=item['expires_at'].isoformat() if item['expires_at'] else None,
            **{key: item[key] for key in ('warning_levels', 'target_levels', 'matched_sites', 'affected_locations')})


def cyclone_pdf_records():
    for item in parse_cyclones(cyclone_page()):
        title = f"{item['storm_name']} | Bulletin #{item['bulletin_number'] or '?'} | {item['issue_time'].isoformat()}"
        yield record('cyclone_pdf', title, item['text'], item['issue_time'], item['source_url'], item.get('pdf_url'))


def fingerprint(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def with_pdf(item, data=None):
    result = dict(item)
    result['pdf_sha256'] = None
    result['pdf_status'] = 'No verified official PDF available for this issue.'
    if item.get('pdf_url') and item.get('issue_time'):
        try:
            data = data or download_pdf(item['pdf_url'])
            verify_issue(data, datetime.fromisoformat(item['issue_time']), item['title'])
            result['pdf_sha256'] = hashlib.sha256(data).hexdigest()
            result['pdf_status'] = 'Archived - issue verified'
            return result, data
        except Exception:
            result['pdf_status'] = 'PDF unavailable or issue verification failed; advisory retained.'
    return result, None


class PDFCache:
    """One source download per URL/issue/document in each check cycle."""
    def __init__(self): self.values = {}

    def prepare(self,item,data=None):
        key=(item.get('pdf_url'),item.get('issue_time'),item['title'])
        if key not in self.values:
            result,content=with_pdf(item,data)
            self.values[key]=({k:result[k] for k in ('pdf_sha256','pdf_status')},content)
        fields,content=self.values[key]
        return {**item,**fields},content
