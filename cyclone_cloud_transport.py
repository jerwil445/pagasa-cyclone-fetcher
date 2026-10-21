"""Small bounded Supabase REST transport; no Flask, local DB, or equipment."""
import json
import re
import time

import requests


def project_url(value):
    value = (value or '').rstrip('/')
    if not re.fullmatch(r'https://[a-z0-9-]+\.supabase\.co', value):
        raise ValueError('Configure the HTTPS Supabase project URL, without a path.')
    return value


def call_api(base, key, path, payload, *, limit=4_000_000):
    """Retry idempotent RPC/upserts, never redirect credentials or log bodies."""
    url = project_url(base) + '/rest/v1/' + path
    if not key:
        raise ValueError('Supabase API key is not configured.')
    headers = {'apikey': key, 'Content-Type': 'application/json',
               'Prefer': 'resolution=ignore-duplicates,return=minimal'}
    if key.startswith('eyJ'):  # legacy anon/service_role JWT keys
        headers['Authorization'] = 'Bearer ' + key
    for attempt in range(3):
        try:
            with requests.post(url, headers=headers, json=payload, timeout=(5, 25),
                               allow_redirects=False, stream=True) as response:
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                if not 200 <= response.status_code < 300:
                    raise ValueError(f'Cloud API request failed (HTTP {response.status_code}).')
                raw = bytearray()
                for chunk in response.iter_content(65536):
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise ValueError('Cloud response exceeds the size limit.')
                return json.loads(raw) if raw else None
        except (requests.Timeout, requests.ConnectionError):
            if attempt == 2:
                raise ValueError('Cloud connection failed; previous records retained.') from None
            time.sleep(1 + attempt)
