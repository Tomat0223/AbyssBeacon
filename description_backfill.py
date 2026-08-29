"""Source-aware description recovery for existing AbyssBeacon records."""
import json
import requests

from scanners.common import metadata
from scanners.http_retry import get_with_backoff
from secrets_manager import get_source_token


def _clean(value):
    return metadata.extract_description({"description": value or ""})


def _card(row):
    try:
        value = row["card_data"] or "{}"
        return json.loads(value) if isinstance(value, str) else (value or {})
    except Exception:
        return {}


def _hf(row):
    model_id = row["model_key"] or ""
    if not model_id:
        return ""
    headers={"User-Agent":"AbyssBeacon/1.0"}
    token=get_source_token("huggingface")
    if token: headers["Authorization"]=f"Bearer {token}"
    session=requests.Session(); session.headers.update(headers)
    r=get_with_backoff(session, f"https://huggingface.co/api/models/{model_id}", provider="Hugging Face", label=f"description {model_id}", timeout=15)
    if r.status_code == 200:
        desc=metadata.extract_description(r.json())
        if desc: return desc
    r=get_with_backoff(session, f"https://huggingface.co/{model_id}/raw/main/README.md", provider="Hugging Face", label=f"README {model_id}", timeout=15)
    return metadata.extract_description({"readme":r.text}) if r.status_code == 200 else ""


def _modelscope(row):
    from scanners.modelscope import get_details
    d=get_details(row["model_key"] or "")
    return metadata.extract_description(d) if d else ""


def _civitai(row):
    card=_card(row); model_id=card.get("civitai_id") or row["model_key"]
    if not model_id: return ""
    s=requests.Session(); s.headers.update({"User-Agent":"AbyssBeacon/1.0","Accept":"application/json"})
    token=get_source_token("civitai")
    if token: s.headers["Authorization"]=f"Bearer {token}"
    r=get_with_backoff(s, f"https://civitai.com/api/v1/models/{model_id}", provider="CivitAI", label=f"description {model_id}", timeout=20)
    if r.status_code != 200: return ""
    item=r.json() if isinstance(r.json(),dict) else {}
    versions=item.get("modelVersions") or []
    version=versions[0] if versions and isinstance(versions[0],dict) else {}
    return _clean(item.get("description") or version.get("description"))


def _red(row):
    from scanners.civitaired import _fetch_version_detail
    card=_card(row); version_id = card.get("version_id") or row["sha"]
    if not version_id: return ""
    d=_fetch_version_detail(version_id)
    return _clean(d.get("description")) if d else ""


FETCHERS={"huggingface":_hf,"modelscope":_modelscope,"civitai":_civitai,"civitaired":_red}

def fetch_description(row):
    fn=FETCHERS.get(str(row["source"] or "").lower())
    if not fn: return ""
    try: return fn(row) or ""
    except Exception: return ""
