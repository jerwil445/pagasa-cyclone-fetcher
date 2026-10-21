"""Run with python -m services.cyclone_cloud_job on a hosted GitHub runner.

Only requests is required. Never imports app.py or local database/hardware code.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from services.cyclone_parser import fetch_page, parse_page, SOURCE_URL
from services.cyclone_cloud_transport import call_api, project_url


def encode_item(item):
    result = dict(item)
    result['issue_time'] = item['issue_time'].isoformat()
    return result


def run(base, writer_key):
    base = project_url(base)
    now = datetime.now(timezone.utc)
    try:
        items = parse_page(fetch_page())
        for item in items:
            if item['issue_time'] > now + timedelta(minutes=15):
                raise ValueError('Future bulletin issue time; check source.')
            if len(item['text']) > 150_000:
                raise ValueError('Unexpected oversized bulletin.')
        payloads = [encode_item(item) for item in items]
        # One immutable observation includes all concurrent storms, or explicit no-bulletin.
        payload = {'version': 1, 'source_url': SOURCE_URL, 'bulletins': payloads}
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        fingerprint = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        call_api(base, writer_key, 'rpc/store_cyclone_observation', {
            'p_fingerprint': fingerprint, 'p_payload': payload, 'p_checked_at': now.isoformat()})
        print(f'Cloud cyclone check saved: {len(items)} bulletin(s); duplicates ignored. No siren actions.')
        return 0
    except Exception:
        # Do not log URLs containing credentials, request headers, or source bodies.
        try:
            call_api(base, writer_key, 'rpc/cyclone_fetch_failed', {'p_checked_at': now.isoformat()})
        except Exception:
            pass
        print('Cyclone fetch/storage failed. Existing archive retained; inspect GitHub run and cloud configuration.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(run(os.environ.get('SUPABASE_URL', ''), os.environ.get('SUPABASE_SECRET_KEY', '')))
