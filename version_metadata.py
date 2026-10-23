"""Targeted, manual upgrades for saved CivitAI/Red version statistics.

Never discover models or replace files/media here. Unknown version values are
not borrowed from a listing or the other provider. Transient failures leave the
snapshot eligible for retry; unavailable source versions are marked checked.
"""
from copy import deepcopy
from datetime import datetime, timezone
from urllib.parse import quote

from source_metrics import mapping, versions, version_statistics, count

VERSION = 1
SOURCES = ("civitai", "civitaired")
FIELDS = ("downloads", "likes", "created", "updated")


def _complete(record):
    return all(count(record.get(field)) is not None for field in FIELDS[:2]) and all(record.get(field) for field in FIELDS[2:])


def current(snapshot):
    records = versions(snapshot)
    marker = mapping(mapping(snapshot.get("card_data")).get("version_metrics_upgrade"))
    return (bool(records) and all(_complete(v) for v in records)) or (
        marker.get("version") == VERSION and marker.get("status") in {"complete", "checked_unavailable"}
    )


def normalize(record):
    result = dict(record)
    raw = version_statistics(record)
    for field in FIELDS:
        if result.get(field) in (None, "") and raw.get(field) not in (None, ""):
            result[field] = raw[field]
    return result


class MetadataRequestError(RuntimeError):
    def __init__(self, status):
        self.status = int(status)
        super().__init__(f"Metadata request returned HTTP {self.status}")


def _request(session, source, url, **kwargs):
    from scanners.http_retry import get_with_backoff
    # Source credentials are never redirected to another host.
    response = get_with_backoff(
        session, url, provider="Library " + source, label="saved version metadata",
        max_retries=1, timeout=(5, 20), pace_key="CivitAI Red" if source == "civitaired" else "CivitAI.com",
        min_interval=1.0, cancellable=False, allow_redirects=False, **kwargs,
    )
    try:
        if response.status_code in {404, 410}:
            return None
        if response.status_code != 200:
            raise MetadataRequestError(response.status_code)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid version metadata response")
        return payload
    finally:
        response.close()


def _session(source):
    import requests
    from secrets_manager import get_source_token
    session = requests.Session()
    session.headers.update({"User-Agent": "AbyssBeacon/1.5.1", "Accept": "application/json"})
    if source == "civitaired":
        from scanners.civitaired import _credentials
        for key, value in _credentials().items():
            session.cookies.set(key, value, domain="civitai.red", path="/")
    else:
        token = get_source_token("civitai")
        if token:
            session.headers["Authorization"] = "Bearer " + token
    return session


def upgrade(snapshot, source, model_key):
    """Return (copy, request_count); caller persists only after full success.

    Requests target each missing saved version, or one exact model when the
    legacy snapshot has no versions. Per-version responses must confirm identity.
    """
    if source not in SOURCES:
        raise ValueError("Unsupported version metadata source")
    result = deepcopy(snapshot)
    card = dict(mapping(result.get("card_data")))
    records = [normalize(v) for v in versions(result)]
    if not records:
        # Older cards often retained exact version identities on download files
        # before they stored a complete version-summary list.
        seeds = {}
        files = result.get("files") if isinstance(result.get("files"), list) else []
        for item in files:
            if not isinstance(item, dict) or not item.get("version_id"):
                continue
            identity = str(item["version_id"])
            seeds[identity] = {"id": item["version_id"], "name": item.get("version") or item.get("version_name") or identity,
                               "base_model": item.get("base_model") or result.get("architecture") or result.get("base_model") or ""}
        if not seeds and card.get("version_id"):
            identity = str(card["version_id"])
            seeds[identity] = {"id": card["version_id"], "name": card.get("version_name") or identity,
                               "base_model": card.get("base_model") or result.get("architecture") or ""}
        records = list(seeds.values())
    requests_used = 0
    unavailable = False
    host = "https://civitai.red" if source == "civitaired" else "https://civitai.com"
    pending = [v for v in records if not _complete(v)]
    if pending or not records:
        with _session(source) as session:
            if not records:
                url = host + "/api/v1/models/" + quote(str(model_key), safe="")
                payload = _request(session, source, url)
                requests_used += 1
                if payload is None:
                    unavailable = True
                else:
                    if str(payload.get("id") or "") != str(model_key):
                        raise ValueError("Model metadata identity mismatch")
                    raw_versions = payload.get("modelVersions")
                    if not isinstance(raw_versions, list) or not raw_versions:
                        raise ValueError("Model response contains no version metadata")
                    from scanners import civitai, civitaired
                    provider = civitaired if source == "civitaired" else civitai
                    records = [normalize(provider._version_summary(v)) for v in raw_versions if isinstance(v, dict)]
                    unavailable = any(not _complete(v) for v in records)
            else:
                for record in pending:
                    version_id = record.get("id")
                    if not version_id:
                        unavailable = True
                        continue
                    payload = _request(session, source, host + "/api/v1/model-versions/" + quote(str(version_id), safe=""))
                    requests_used += 1
                    if payload is None:
                        unavailable = True
                        continue
                    if str(payload.get("id") or "") != str(version_id):
                        raise ValueError("Version metadata identity mismatch")
                    owner = payload.get("modelId")
                    if owner is not None and str(owner) != str(model_key):
                        raise ValueError("Version metadata belongs to another model")
                    values = normalize(payload)
                    # Preserve names, download files, description and media. This
                    # upgrade fills only the newly required statistics and dates.
                    for field in FIELDS:
                        if record.get(field) in (None, "") and values.get(field) not in (None, ""):
                            record[field] = values[field]
                    unavailable |= not _complete(record)
    card["versions"] = records
    card["version_metrics_upgrade"] = {
        "version": VERSION, "status": "checked_unavailable" if unavailable else "complete",
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    result["card_data"] = card
    return result, requests_used
