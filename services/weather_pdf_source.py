"""Equipment-independent official PDF downloading and issue verification."""
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.parse import urljoin, urlsplit
import requests

PH = timezone(timedelta(hours=8))
MAX_FILE = 8 * 1024 * 1024

def official_pdf_url(url):
    try:
        p = urlsplit(url or '')
        return (p.scheme == 'https' and not p.username and not p.password
                and p.port in (None, 443) and (p.hostname == 'pagasa.dost.gov.ph'
                or (p.hostname or '').endswith('.pagasa.dost.gov.ph')
                or (p.hostname == 'd3l5rdm090s0mj.cloudfront.net'
                    and p.path == '/public-files/prsd-cms/mindanao/regional_forecast.pdf'))
                and p.path.lower().endswith('.pdf'))
    except ValueError:
        return False


def download_pdf(url):
    for _ in range(4):
        if not official_pdf_url(url):
            raise ValueError('No official PDF link is available.')
        with requests.get(url, stream=True, timeout=(5, 20), allow_redirects=False,
                          headers={'User-Agent': 'FloodSystem/1.0 PDF archive'}) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = urljoin(url, response.headers.get('Location', ''))
                continue
            response.raise_for_status()
            if int(response.headers.get('Content-Length') or 0) > MAX_FILE:
                raise ValueError('PDF exceeds the 8 MiB file limit.')
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_FILE:
                    raise ValueError('PDF exceeds the 8 MiB file limit.')
            if not data.startswith(b'%PDF-') or b'%%EOF' not in data[-2048:]:
                raise ValueError('Source did not return a complete PDF.')
            return bytes(data)
    raise ValueError('Too many PDF redirects.')


def verify_issue(data, issue, title):
    """Fail closed: require an explicit issue date/time, not forecast validity."""
    from pypdf import PdfReader
    reader = PdfReader(BytesIO(data), strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 30:
        raise ValueError('Encrypted or unusually large PDF cannot be verified.')
    text = ' '.join((page.extract_text() or '') for page in reader.pages[:2])
    text = ' '.join(text.replace('\u00a0', ' ').split())
    pattern = (r'Issued\s+(?:at|on)\s*:?\s*(\d{1,2}(?::\d{2})?\s*[AP]\.?M\.?)'
               r'\s*[,;]?\s*(?:today\s*[,;]?\s*)?(\d{1,2}\s+[A-Za-z]+\s*,?\s*\d{4})')
    match = re.search(pattern, text, re.I)
    if not match or issue is None:
        raise ValueError('PDF issue date/time could not be verified; not attached.')
    clock = re.sub(r'[.\s]', '', match[1]).upper()
    if ':' not in clock:
        clock = clock[:-2] + ':00' + clock[-2:]
    date = re.sub(r'\s*,\s*', ' ', match[2])
    parsed = None
    for fmt in ('%I:%M%p %d %B %Y', '%I:%M%p %d %b %Y'):
        try:
            parsed = datetime.strptime(clock + ' ' + date, fmt).replace(tzinfo=PH)
            break
        except ValueError:
            pass
    expected = issue.replace(tzinfo=timezone.utc) if issue.tzinfo is None else issue
    if parsed is None or parsed.astimezone(timezone.utc) != expected.astimezone(timezone.utc):
        raise ValueError('PDF issue time differs from the bulletin; not attached.')
    if 'Daily Weather Forecast' in title and not re.search(r'\bWEATHER\s+FORECAST\b', text, re.I):
        raise ValueError('PDF is not identifiable as a weather forecast; not attached.')
    if re.search(r'Rainfall\s+(?:Warning|Advisory)', title, re.I):
        if not re.search(r'Rainfall\s+(?:Warning|Advisory)', text, re.I):
            raise ValueError('PDF is not identifiable as a rainfall bulletin; not attached.')
        number = re.search(r'(?:No\.?|#)\s*(\d+)', title, re.I)
        if number and not re.search(r'(?:No\.?|#)\s*' + number[1] + r'\b', text, re.I):
            raise ValueError('PDF rainfall bulletin number differs; not attached.')
    if 'Bulletin #' in title:
        storm = title.split(' |')[0]
        number = re.search(r'Bulletin #(\d+)', title)
        if not re.search(r'\b' + re.escape(storm) + r'\b', text, re.I) or (number and not re.search(
                r'BULLETIN\s*(?:NO\.?|#)?\s*' + number[1] + r'\b', text, re.I)):
            raise ValueError('PDF cyclone identity differs from the saved bulletin.')
