"""Standalone cloud archive extension. python -B -m services.weather_cloud_job"""
import os
import requests
from services.cyclone_cloud_transport import project_url, call_api
from services.weather_archive_sources import (
    daily_records, regional_records, rainfall_records, cyclone_pdf_records, PDFCache, fingerprint)


def upload_pdf(base, key, digest, data):
    path = project_url(base) + '/storage/v1/object/pagasa-pdfs/' + digest + '.pdf'
    headers = {'apikey': key, 'Content-Type': 'application/pdf', 'x-upsert': 'false'}
    if key.startswith('eyJ'): headers['Authorization'] = 'Bearer ' + key
    # Never replace existing bytes. Hash-addressed duplicate objects are harmless.
    response = requests.post(path, data=data, headers=headers, timeout=(5, 30), allow_redirects=False)
    if response.status_code not in (200, 201, 409):
        # Storage can return 400 with the specific Duplicate error code.
        try: duplicate = response.status_code == 400 and response.json().get('error') == 'Duplicate'
        except ValueError: duplicate = False
        if not duplicate: raise ValueError('Private PDF upload failed.')


def run(base, key):
    base = project_url(base)
    if not key: raise ValueError('Cloud writer key missing.')
    failed = False
    cache = PDFCache()
    for name, load in [('daily', daily_records), ('regional', regional_records),
                       ('rainfall', rainfall_records), ('cyclone_pdf', cyclone_pdf_records)]:
        ok = True
        try:
            for value in load():
                item, supplied = value if isinstance(value, tuple) else (value, None)
                payload, data = cache.prepare(item, supplied)
                if data:
                    try:
                        exists = call_api(base,key,'rpc/weather_pdf_stored',{'p_sha':payload['pdf_sha256']})
                        if exists is not True:
                            used = call_api(base, key, 'rpc/weather_storage_usage', {})
                            if type(used) is not int or used + len(data) > 256 * 1048576:
                                raise ValueError('Cloud PDF storage cap reached.')
                            upload_pdf(base, key, payload['pdf_sha256'], data)
                    except Exception:
                        payload['pdf_sha256'] = None
                        payload['pdf_status'] = 'Cloud PDF upload/capacity failed; advisory retained.'
                        ok = False
                elif item.get('pdf_url') and item.get('issue_time'):
                    ok = False
                call_api(base, key, 'rpc/store_weather_record',
                         {'p_fingerprint': fingerprint(payload), 'p_payload': payload})
        except Exception:
            ok = False
        try: call_api(base, key, 'rpc/weather_source_checked', {'p_source': name, 'p_ok': ok})
        except Exception: ok = False
        print(f'Weather archive {name}: {"CHECKED" if ok else "FAILED/PARTIAL"}. No siren actions.')
        failed |= not ok
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(run(os.environ.get('SUPABASE_URL', ''), os.environ.get('SUPABASE_SECRET_KEY', '')))
