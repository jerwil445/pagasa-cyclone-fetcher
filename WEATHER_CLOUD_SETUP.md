# Daily Weather, Rainfall and PDF archive deployment

Status: code prepared locally, NOT deployed. Preserve your existing cyclone
tables, reader token, GitHub repository and repository secrets.

Targets: GitHub `jerwil445/pagasa-cyclone-fetcher`; Supabase
`fytiprvpbdnjiqfhjhpq`. Do not deploy into your other Supabase project.

## 1. Storage (already created by owner)

Confirm Storage > `pagasa-pdfs` exists and Public is OFF. Restrict MIME type to
`application/pdf` and per-file limit to 8 MB. Do not add public Storage policies.
No cloud PDF storage will work without the next steps.

## 2. Apply the additive SQL

Open **SQL Editor > New query** in the project above. Paste the complete contents
of `database/weather_cloud_setup.sql` and Run. This is a standalone setup script,
not a local database migration. It requires your existing `cyclone_cloud_readers`
table and uses its existing token hash. No new reader token is needed.

Expected: new `weather_cloud_records` and `weather_cloud_health` tables. Existing
cyclone records remain untouched. RLS enabled; no anonymous table access. Reader
RPC validates the random token before returning archive records.

Inspect Security Advisor after applying. Definer functions deliberately mediate
the existing custom reader-token design (not Supabase Auth users), have empty
search paths, and explicitly limited execute grants. Do not grant public table
access to resolve a permission error.

## 3. Deploy the private PDF access function

In Supabase **Edge Functions**, create/deploy a function named `weather-pdf`.
Use the complete file `supabase/functions/weather-pdf/index.ts` as `index.ts`.
Disable the gateway's **Verify JWT** check for THIS function only, because it
uses your existing random reader token, not a user JWT. The handler validates
the token using `weather_pdf_access` BEFORE issuing a 60-second signed PDF URL.
Leaving the gateway JWT check enabled can reject publishable-key clients.

The Supabase-hosted function uses the built-in `SUPABASE_URL` and
`SUPABASE_SERVICE_ROLE_KEY` environment variables. Do not put their values in
source code, chat, your laptop's .env or the GitHub repository.

If using an already installed CLI instead, first inspect `supabase --help` and
`supabase functions deploy --help` for your installed version, then deploy only
this function to the target project with JWT gateway verification disabled.
`supabase/config.toml` records the intended function setting.

## 4. Upload the sanitized fetcher package to GitHub

In the FloodSystem terminal:

```powershell
.\.venv\Scripts\python.exe -B scripts/package_weather_cloud.py
```

This prints a temporary ZIP path. Extract it on your laptop, then upload its
CONTENTS into the existing `pagasa-cyclone-fetcher` repository, preserving the
folders. Do not upload the ZIP as a single file and do not upload the entire
FloodSystem project. The script includes an explicit safe file list: no .env,
database contents, keys, camera configuration or firmware credentials.

The Python `pagasa_service.py` module is included for its pure parser; importing
it does not import Flask or hardware. Existing cyclone job still runs first;
the additional weather step runs independently even if the cyclone step fails.
Dependencies are pinned in `requirements-cloud.txt`.

Confirm GitHub **Settings > Secrets and variables > Actions** already contains
`SUPABASE_URL` and `SUPABASE_SECRET_KEY`. Reuse them. Never paste their values
into a workflow file. Commit to the default branch (`main`).

## 5. Test the cloud BEFORE enabling local cloud sync

Open **Actions > Archive official PAGASA cyclone bulletins > Run workflow**.
Check BOTH the original cyclone step and the new weather archive step. The new
step prints only source names and CHECKED / FAILED/PARTIAL, never secrets.

Verify:

- `weather_cloud_records` has daily, rainfall, and available cyclone-PDF records.
- `weather_cloud_health` has independent daily/regional/rainfall/cyclone_pdf checks.
- Storage > `pagasa-pdfs` has hash-named PDF files when verification succeeds.
- A second run does not duplicate the same payload or PDF.
- A scheduled run occurs; manual success does not prove the schedule is working.

If the workflow times out or sources fail, inspect the failed step and health
table. Do not interpret it as no rainfall/cyclone. GitHub requests a five-minute
cadence, but may delay/drop scheduled events. This is not guaranteed real-time
or a safety-certified warning feed.

## 6. Enable the local extension

Keep your existing `CYCLONE_CLOUD_URL`, `CYCLONE_CLOUD_PUBLISHABLE_KEY` and
`CYCLONE_CLOUD_READER_TOKEN` values in your UNTRACKED local .env. Add:

```dotenv
WEATHER_ARCHIVE_ENABLED=true
WEATHER_CLOUD_SYNC_ENABLED=true
WEATHER_ARCHIVE_INTERVAL_SECONDS=300
```

Restart your usual `python app.py` server. The new worker starts immediately,
then requests another cycle every five minutes. Do not start another copy of
the server. The existing national daily-weather worker can keep its schedule;
the new archive worker also fetches daily/rainfall directly every five minutes.
With cloud sync disabled, direct fetching still works while the laptop is ON.

The startup thread follows this project's existing `__main__` worker pattern.
If you instead launch with Gunicorn/Flask CLI or import `app`, these workers do
not start: configure a single dedicated scheduler process before deployment.

Check Weather Advisories > Overview > Weather and PDF archive. Reload for the
latest worker and cloud-source statuses. Daily Weather includes the regional
forecast PDF text; Rainfall retains warning colors only when explicitly stated.
Existing View PDF / Download buttons use the same permission-protected local
archive. Files download to your DEVICE only if you click Download; catch-up also
uses laptop SERVER storage for its local copy (which has its own capacity cap).

## 7. Laptop-OFF acceptance test

1. Record the latest cloud observation and stop the local server/laptop.
2. Wait for a real NEW source issue and a scheduled successful cloud run.
3. Verify its original issue time and actual PDF in Supabase, not just its link.
4. Start the server. Verify the missed issue appears in the appropriate table.
5. Open its archived PDF and check its issue time. Retry does not duplicate it.

No one should claim laptop-OFF operation is live until this test passes.

## Limits and troubleshooting

- A regional weather forecast is not a Yellow/Orange/Red rainfall warning.
- Rainfall records without an official PDF are retained as text, not fabricated PDFs.
- Only the exact regional forecast CDN host/path is additionally allowed, and
  the regional source must link it from the official PAGASA page. No blanket
  CloudFront allowlist. Redirects are checked before following.
- Every attached PDF must pass document identity + exact issue verification.
  Image-only PDFs without extractable issue text fail closed for manual review.
- Cloud storage cap: 256 MiB; text cap: 100 MB. These are protective caps, NOT a
  guarantee of free use. Monitor Supabase storage, egress and function usage and
  GitHub billing. No automatic deletion of old archives is implemented.
- The private bucket must be dedicated to this writer. Do not run multiple
  independent writers or public uploads: quota checks assume the workflow's
  existing single-writer concurrency group. Failed metadata writes may leave
  harmless orphaned hash files for later manual review; no automatic removal.
- Cloud cursor advances only after PDF integrity/attachment succeeds. A broken
  PDF/function or local capacity cap can pause catch-up at that record; the
  valid bulletin text is retained. Fix the cause and retry, not the cursor.
- The cloud reader recovers only observations captured AFTER deployment. It
  cannot recreate a PDF PAGASA replaced before the archive ever fetched it.
- All weather paths remain informational. No cloud code imports app.py, OpenCV,
  the local database, ESP32 services or siren-control modules.

Documentation: https://supabase.com/docs/guides/functions/deploy,
https://supabase.com/docs/guides/storage/security/access-control,
https://supabase.com/docs/guides/storage/serving/downloads
