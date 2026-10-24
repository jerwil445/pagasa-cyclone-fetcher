import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import requests
from services.weather_import_lock import serialized_weather_import


PAGASA_WEATHER_URL = "https://bagong.pagasa.dost.gov.ph/weather"
PAGASA_WEATHER_ADVISORY_URL = "https://bagong.pagasa.dost.gov.ph/weather/weather-advisory"
_PAGASA_HOST_SUFFIX = ".pagasa.dost.gov.ph"


class _ForecastTableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._heading_parts = None
        self._waiting_for_forecast_table = False
        self._in_forecast_table = False
        self._in_cell = False
        self._cell_parts = []
        self._row = []
        self._headers = []
        self.rows = []
        self.pdf_links = []
        self.text_parts = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "a":
            href = attributes.get("href", "")
            if href.lower().endswith(".pdf"):
                self.pdf_links.append(urljoin(PAGASA_WEATHER_URL, href))
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_parts = []
        elif tag == "table" and self._waiting_for_forecast_table:
            self._waiting_for_forecast_table = False
            self._in_forecast_table = True
        elif self._in_forecast_table and tag == "tr":
            self._row = []
        elif self._in_forecast_table and tag in {"td", "th"}:
            self._in_cell = True
            self._cell_parts = []

    def handle_data(self, data):
        self.text_parts.append(data)
        if self._heading_parts is not None:
            self._heading_parts.append(data)
        if self._in_cell:
            self._cell_parts.append(data)

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self._heading_parts is not None:
            heading = " ".join(" ".join(self._heading_parts).split()).casefold()
            self._waiting_for_forecast_table = heading == "forecast weather conditions"
            self._heading_parts = None
        elif self._in_forecast_table and tag in {"td", "th"} and self._in_cell:
            value = " ".join(" ".join(self._cell_parts).split())
            self._row.append(value)
            self._in_cell = False
            self._cell_parts = []
        elif self._in_forecast_table and tag == "tr" and self._row:
            if not self._headers:
                self._headers = [value.casefold() for value in self._row]
            else:
                self.rows.append(dict(zip(self._headers, self._row)))
            self._row = []
        elif tag == "table" and self._in_forecast_table:
            self._in_forecast_table = False


class _WeatherAdvisoryParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._capture_depth = 0
        self.text_parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "div":
            classes = set((attributes.get("class") or "").split())
            if self._capture_depth == 0 and {"article-content", "weather-advisory"}.issubset(classes):
                self._capture_depth = 1
            elif self._capture_depth:
                self._capture_depth += 1
        elif self._capture_depth and tag == "a":
            href = attributes.get("href", "")
            if href:
                self.links.append(urljoin(PAGASA_WEATHER_ADVISORY_URL, href))
        elif self._capture_depth and tag in {"br", "p", "li", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")

    def handle_data(self, data):
        if self._capture_depth:
            self.text_parts.append(data)

    def handle_endtag(self, tag):
        if not self._capture_depth:
            return
        if tag in {"p", "li", "h1", "h2", "h3", "h4"}:
            self.text_parts.append("\n")
        elif tag == "div":
            self._capture_depth -= 1


def parse_pagasa_weather_advisory_page(html: str) -> dict:
    parser = _WeatherAdvisoryParser()
    parser.feed(html)
    advisory_text = " ".join(" ".join(parser.text_parts).split())
    no_advisory = bool(re.search(r"no\s+(?:weather\s+)?advisory\s+issued", advisory_text, re.IGNORECASE))
    source_url = next((
        link for link in parser.links
        if urlsplit(link).hostname
        and (urlsplit(link).hostname == "pagasa.dost.gov.ph"
             or urlsplit(link).hostname.endswith(_PAGASA_HOST_SUFFIX))
    ), PAGASA_WEATHER_ADVISORY_URL)
    if no_advisory or not advisory_text:
        return {
            "active": False,
            "advisory_text": advisory_text,
            "warnings": [],
            "source_url": source_url,
            "page_url": PAGASA_WEATHER_ADVISORY_URL,
        }

    level_pattern = re.compile(
        r"\b(?P<level>YELLOW|ORANGE|RED)\s+(?:(?:HEAVY\s+)?RAINFALL\s+)?WARNING\b"
        r"|\b(?:HEAVY\s+)?RAINFALL\s+(?P<level_after>YELLOW|ORANGE|RED)\s+WARNING\b",
        re.IGNORECASE,
    )
    level_match = level_pattern.search(advisory_text)
    issue_match = re.search(
        r"Issued at\s*[:\-]?\s*(.+?)(?=\s+(?:Weather|Rainfall|Advisory)\b|$)",
        advisory_text,
        re.IGNORECASE,
    )
    issue_text = " ".join(issue_match.group(1).split()) if issue_match else ""
    published_at = None
    for date_format in ("%I:%M %p, %d %B %Y", "%I:%M %p, %d %B, %Y"):
        if not issue_text:
            break
        try:
            published_at = datetime.strptime(issue_text, date_format).replace(
                tzinfo=timezone(timedelta(hours=8))
            )
            break
        except ValueError:
            continue

    title_match = re.search(
        r"\b(?:PAGASA\s+)?((?:WEATHER|RAINFALL)\s+ADVISORY(?:\s+NO\.?\s*\d+)?)",
        advisory_text,
        re.IGNORECASE,
    )
    title = title_match.group(1).title() if title_match else "PAGASA Weather Advisory"
    warnings = []
    if level_match:
        level = (level_match.group("level") or level_match.group("level_after")).upper()
        locations_match = re.search(
            r"\b(?:areas? affected|affected areas|places affected|locations? affected)\s*[:\-]\s*(.+?)(?=\s+(?:Issued at|Advisory No\.?|Next update|For more information)\b|$)",
            advisory_text,
            re.IGNORECASE,
        )
        if not locations_match:
            warning_suffix = advisory_text[level_match.end():]
            locations_match = re.search(
                r"\b(?:over|for)\s+(.+?)(?=\s+(?:Issued at|Advisory No\.?|Next update|For more information)\b|[.;]|$)",
                warning_suffix,
                re.IGNORECASE,
            )
        location = " ".join(locations_match.group(1).split()).strip(" .;:-") if locations_match else ""
        title = f"PAGASA {level} Rainfall Warning"
        warnings.append({
            "title": title,
            "level": level,
            "location": location or "Location not specified by PAGASA",
            "advisory_text": advisory_text,
            "issue_text": issue_text,
            "published_at": published_at,
            "source_url": source_url,
        })
    else:
        location_match = re.search(
            r"\b(?:areas? affected|affected areas|places affected|locations? affected)\s*[:\-]\s*(.+?)(?=\s+(?:Issued at|Advisory No\.?|Next update|For more information)\b|$)",
            advisory_text,
            re.IGNORECASE,
        )
        warnings.append({
            "title": title,
            "level": "ADVISORY",
            "location": " ".join(location_match.group(1).split()).strip(" .;:-") if location_match else "Location not specified by PAGASA",
            "advisory_text": advisory_text,
            "issue_text": issue_text,
            "published_at": published_at,
            "source_url": source_url,
        })

    return {
        "active": True,
        "advisory_text": advisory_text,
        "warnings": warnings,
        "source_url": source_url,
        "page_url": PAGASA_WEATHER_ADVISORY_URL,
        "published_at": published_at,
    }


def fetch_pagasa_weather_advisories(timeout: float = 15.0) -> dict:
    response = requests.get(
        PAGASA_WEATHER_ADVISORY_URL,
        timeout=timeout,
        headers={"User-Agent": "FloodSystem/1.0 (PAGASA official weather advisory reader)"},
    )
    response.raise_for_status()
    final_host = urlsplit(response.url).hostname or ""
    if final_host != "pagasa.dost.gov.ph" and not final_host.endswith(_PAGASA_HOST_SUFFIX):
        raise ValueError("PAGASA redirected to a non-official domain; advisory import was stopped.")
    return parse_pagasa_weather_advisory_page(response.text)


def _normalize_location(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).split())


def location_matches_exactly(official_location: str, configured_target: str) -> bool:
    target = _normalize_location(configured_target)
    if not target:
        return False
    locations = [
        _normalize_location(part)
        for part in re.split(r"[,;|\n]|\s+and\s+", official_location or "", flags=re.IGNORECASE)
    ]
    return target in locations or target == _normalize_location(official_location)




def sync_pagasa_weather_advisories(application, advisories=None) -> dict:
    """Import official PAGASA advisories for review, without activating hardware."""
    from database.db import db
    from models.ndrrmc_alert import NDRRMCAlert
    from services.audit_service import SystemLogService

    advisories = advisories if advisories is not None else fetch_pagasa_weather_advisories()
    if not advisories.get("active"):
        with application.app_context():
            stale = NDRRMCAlert.query.filter_by(
                source="PAGASA Official Website",
                alert_type="OFFICIAL PAGASA WEATHER ADVISORY",
                is_active=True,
            ).all()
            for advisory in stale:
                advisory.is_active = False
                advisory.severity = "EXPIRED"
                advisory.analysis_result = "PAGASA reports no active Weather Advisory."
            db.session.commit()
            result = {
                "active": False,
                "imported": 0,
                "updated": 0,
                "expired": len(stale),
                "siren_triggered": 0,
                "source_url": advisories.get("source_url", PAGASA_WEATHER_ADVISORY_URL),
                "message": "PAGASA reports no active Weather Advisory.",
            }
            application.config["PAGASA_WARNING_STATUS"] = result
            SystemLogService.log(
                "INFO",
                "PAGASA_WARNING",
                result["message"],
                details=json.dumps(result, ensure_ascii=False),
            )
            return result

    imported = 0
    updated = 0
    expired = 0
    siren_statuses = []
    parsed_warnings = advisories.get("warnings", [])
    active_keys = set()

    with application.app_context():
        pdf_records = []
        for warning in parsed_warnings:
            location = warning["location"]
            alert_type = "OFFICIAL PAGASA WEATHER ADVISORY"
            source_name = "PAGASA Official Website"
            existing = NDRRMCAlert.query.filter_by(
                title=warning["title"],
                source=source_name,
                location=location,
                alert_type=alert_type,
            ).first()
            severity = {"RED": "CRITICAL", "ORANGE": "WARNING", "YELLOW": "WARNING"}.get(warning["level"], "ADVISORY")
            published_at = warning.get("published_at")
            if published_at and published_at.tzinfo:
                published_at = published_at.astimezone(timezone.utc).replace(tzinfo=None)
            published_at = published_at or datetime.utcnow()
            active_keys.add((warning["title"], source_name, location, alert_type))

            if existing:
                changed = any((
                    existing.message != warning["advisory_text"],
                    existing.source_url != warning["source_url"],
                    existing.severity != severity,
                    not existing.is_active,
                ))
                existing.message = warning["advisory_text"]
                existing.source_url = warning["source_url"]
                existing.published_at = published_at
                existing.severity = severity
                existing.is_active = True
                if changed:
                    existing.received_at = datetime.utcnow()
                    updated += 1
                pdf_records.append(existing)
            else:
                record = NDRRMCAlert(
                    title=warning["title"],
                    message=warning["advisory_text"],
                    alert_type=alert_type,
                    severity=severity,
                    location=location,
                    source=source_name,
                    source_url=warning["source_url"],
                    relevance="HIGH",
                    analysis_result="Imported from the official PAGASA Weather Advisory page.",
                    published_at=published_at,
                    received_at=datetime.utcnow(),
                    is_active=True,
                )
                db.session.add(record)
                pdf_records.append(record)
                imported += 1

        if parsed_warnings:
            active = NDRRMCAlert.query.filter_by(
                source="PAGASA Official Website",
                alert_type="OFFICIAL PAGASA WEATHER ADVISORY",
                is_active=True,
            ).all()
            for advisory in active:
                key = (advisory.title, advisory.source, advisory.location, advisory.alert_type)
                if key not in active_keys:
                    advisory.is_active = False
                    advisory.severity = "EXPIRED"
                    advisory.analysis_result = "Replaced by a newer active PAGASA Weather Advisory."
                    expired += 1
        db.session.commit()

        from services.weather_pdf_service import archive_records
        archive_records([('alert', r.id, r.published_at, r.title, r.source_url) for r in pdf_records])

        raw_targets = application.config.get("PAGASA_SIREN_TARGET_LOCATIONS", "")
        targets = [item.strip() for item in re.split(r"[|\n]", raw_targets) if item.strip()]
        for warning in parsed_warnings:
            if warning["level"] not in {"YELLOW", "ORANGE", "RED"}:
                siren_statuses.append("SUPPRESSED_NO_WARNING_LEVEL")
                continue
            matching_target = next((
                target for target in targets
                if location_matches_exactly(warning["location"], target)
            ), None)
            if not matching_target:
                siren_statuses.append("SUPPRESSED_LOCATION_MISMATCH")
                continue
            # Weather is information only; there is no weather approval workflow.
            siren_statuses.append("WEATHER_INFORMATION_ONLY")

    result = {
        "active": True,
        "imported": imported,
        "updated": updated,
        "expired": expired,
        "siren_triggered": sum(status == "DISPATCHED" for status in siren_statuses),
        "siren_statuses": siren_statuses,
        "source_url": advisories.get("source_url", PAGASA_WEATHER_ADVISORY_URL),
        "message": f"PAGASA advisory sync: {imported} new, {updated} updated, {expired} expired; siren statuses: {', '.join(siren_statuses) or 'none'}.",
    }
    with application.app_context():
        SystemLogService.log(
            "WARNING" if result["siren_triggered"] else "INFO",
            "PAGASA_WARNING",
            result["message"],
            details=json.dumps(result, ensure_ascii=False),
        )
        application.config["PAGASA_WARNING_STATUS"] = result
    return result


def parse_pagasa_weather_page(html: str, page_url: str = PAGASA_WEATHER_URL) -> dict:
    parser = _ForecastTableParser()
    parser.feed(html)
    if not parser.rows:
        raise ValueError("PAGASA forecast table was not found in the official page.")

    page_text = " ".join(" ".join(parser.text_parts).split())
    page_text = re.sub(r"\s+,", ",", page_text)
    issue_match = re.search(
        r"Issued\s+at\s*:\s*(\d{1,2}:\d{2}\s*[AP]M,\s*\d{1,2}\s+[A-Za-z]+,?\s+\d{4})",
        page_text, re.IGNORECASE,
    )
    issue_text = " ".join(issue_match.group(1).split()) if issue_match else ""
    issued_at = None
    if issue_text:
        for date_format in ("%I:%M %p, %d %B %Y", "%I:%M %p, %d %B, %Y"):
            try:
                issued_at = datetime.strptime(issue_text, date_format).replace(
                    tzinfo=timezone(timedelta(hours=8))
                )
                break
            except ValueError:
                continue

    if issued_at is None:
        raise ValueError("PAGASA forecast issue date is missing or unreadable; import was stopped.")

    rainfall_rows = []
    for row in parser.rows:
        weather = row.get("weather condition", "")
        impacts = row.get("impacts", "")
        if re.search(r"rain|precipitation", f"{weather} {impacts}", re.IGNORECASE):
            rainfall_rows.append({
                "location": row.get("place", ""),
                "weather_condition": weather,
                "caused_by": row.get("caused by", ""),
                "impacts": impacts,
                "severity": "ADVISORY",
            })

    official_pdf = next((
        link for link in parser.pdf_links
        if urlsplit(link).hostname
        and (urlsplit(link).hostname == "pagasa.dost.gov.ph"
             or urlsplit(link).hostname.endswith(_PAGASA_HOST_SUFFIX))
    ), None)
    source_url = official_pdf or page_url
    if not rainfall_rows:
        raise ValueError("The official PAGASA forecast currently has no rainfall-related regional rows.")

    return {
        "title": f"PAGASA Daily Weather Forecast{f' - {issue_text}' if issue_text else ''}",
        "published_at": issued_at,
        "issue_text": issue_text,
        "source_url": source_url,
        "page_url": page_url,
        "rows": rainfall_rows,
    }


def fetch_pagasa_rainfall_forecast(timeout: float = 15.0) -> dict:
    response = requests.get(
        PAGASA_WEATHER_URL,
        timeout=timeout,
        headers={"User-Agent": "FloodSystem/1.0 (PAGASA public weather page reader)"},
    )
    response.raise_for_status()
    final_host = urlsplit(response.url).hostname or ""
    if final_host != "pagasa.dost.gov.ph" and not final_host.endswith(_PAGASA_HOST_SUFFIX):
        raise ValueError("PAGASA redirected to a non-official domain; import was stopped.")
    return parse_pagasa_weather_page(response.text, PAGASA_WEATHER_URL)


@serialized_weather_import
def sync_pagasa_rainfall_forecast(application, user_id=None, forecast=None) -> dict:
    """Import or update official PAGASA rainfall forecast rows idempotently."""
    from database.db import db
    from models.ndrrmc_alert import NDRRMCAlert
    from services.audit_service import AuditService, SystemLogService
    from services.ndrrmc_service import NDRRMCService

    with application.app_context():
        forecast = forecast or fetch_pagasa_rainfall_forecast()
        published_at = forecast["published_at"]
        if published_at and published_at.tzinfo:
            published_at = published_at.astimezone(timezone.utc).replace(tzinfo=None)
        published_at = published_at or datetime.utcnow()
        title = forecast["title"]
        source_name = "PAGASA Official Website"
        alert_type = "OFFICIAL REGIONAL RAINFALL FORECAST"
        imported = 0
        updated = 0

        pdf_records = []

        try:
            for row in forecast["rows"]:
                location = row["location"]
                message = "\n".join((
                    f"Weather condition: {row['weather_condition']}",
                    f"Caused by: {row['caused_by']}",
                    f"Impacts: {row['impacts']}",
                ))
                existing = NDRRMCAlert.query.filter_by(
                    title=title,
                    source=source_name,
                    location=location,
                    alert_type=alert_type,
                ).first()
                matched = [keyword for keyword in NDRRMCService.LOCAL_KEYWORDS if keyword in location.lower()]
                relevance = (
                    "DIRECT_MATCH" if any(word in location.lower() for word in ("cabadbaran", "csucc"))
                    else "HIGH" if matched
                    else "LOW"
                )
                if existing:
                    changed = any((
                        existing.message != message,
                        existing.source_url != forecast["source_url"],
                        existing.published_at != published_at,
                        existing.relevance != relevance,
                    ))
                    if changed:
                        existing.message = message
                        existing.source_url = forecast["source_url"]
                        existing.published_at = published_at
                        existing.received_at = datetime.utcnow()
                        existing.relevance = relevance
                        updated += 1
                    pdf_records.append(existing)
                    continue

                record = NDRRMCAlert(
                    title=title,
                    message=message,
                    alert_type=alert_type,
                    severity="ADVISORY",
                    location=location,
                    source=source_name,
                    source_url=forecast["source_url"],
                    relevance=relevance,
                    analysis_result=(
                        "Imported from PAGASA's official regional daily weather forecast. "
                        "This is a forecast, not a color-coded Yellow/Orange/Red rainfall warning."
                    ),
                    published_at=published_at,
                    received_at=datetime.utcnow(),
                )
                db.session.add(record)
                pdf_records.append(record)
                imported += 1

            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        from services.weather_pdf_service import archive_records
        archive_records([('alert', r.id, r.published_at, r.title, forecast['source_url']) for r in pdf_records])

        result = {
            "event": "PAGASA_OFFICIAL_RAINFALL_FORECAST_SYNC",
            "source_url": forecast["source_url"],
            "page_url": forecast["page_url"],
            "published_at": published_at.isoformat(),
            "imported": imported,
            "updated": updated,
            "warning_level": "NOT_PROVIDED_BY_SOURCE",
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "message": f"PAGASA forecast checked: {imported} new regional row(s), {updated} updated.",
        }
        SystemLogService.log(
            "INFO",
            "PAGASA_IMPORT",
            f"Imported {imported} and updated {updated} official PAGASA regional rainfall forecast rows.",
            details=json.dumps(result, ensure_ascii=False),
        )
        if user_id is not None:
            AuditService.log_action(
                "SYNC_PAGASA_RAINFALL_FORECAST",
                "PAGASA",
                f"Imported {imported} and updated {updated} regional rainfall forecast rows from the official PAGASA site.",
                user_id,
            )
        application.config["PAGASA_SYNC_STATUS"] = {
            "success": True,
            **result,
        }
        return result
