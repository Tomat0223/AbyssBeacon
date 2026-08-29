from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import threading
import time
import uuid


_LOCK = threading.RLock()
_JOBS = OrderedDict()
_COMPLETE_TTL = 12.0
_CANCELED_TTL = 5.0
_MAX_JOBS = 100
_STATE_FILE = Path(__file__).resolve().parent / "app_config" / "active_downloads.json"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _public_job(job):
    return {k: v for k, v in job.items() if not k.startswith("_")}


def _renamed_part_path(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    renamed = re.sub(
        r"(?i)(^|[\\/])ModelRadar-Other(?=([\\/]|$))",
        lambda match: match.group(1) + "AbyssBeacon-Other",
        raw,
    )
    return re.sub(
        r"(?i)(^|[\\/])ModelRadar(?=([\\/]|$))",
        lambda match: match.group(1) + "AbyssBeacon",
        renamed,
    )


def _sync_part_size(job):
    """Use the on-disk .part file as the source of truth for saved progress."""
    path = str(job.get("part_path") or "").strip()
    if path:
        renamed = _renamed_part_path(path)
        try:
            if renamed != path and not Path(path).exists() and Path(renamed).exists():
                path = renamed
                job["part_path"] = renamed
        except Exception:
            pass
    if not path:
        return
    try:
        part = Path(path)
        if part.is_file():
            size = max(0, int(part.stat().st_size))
            job["downloaded_bytes"] = size
            job["_last_bytes"] = size
    except Exception:
        pass


def _save_locked():
    """Persist unfinished/failed jobs so a AbyssBeacon restart can offer Resume."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        keep = []
        for job in _JOBS.values():
            if job.get("status") in {"starting", "downloading", "installing", "canceling", "pausing", "paused", "failed"}:
                keep.append(_public_job(job))
        temp = _STATE_FILE.with_suffix(".json.tmp")
        temp.write_text(json.dumps({"jobs": keep}, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(_STATE_FILE)
    except Exception:
        # Download tracking must never be able to break the downloader itself.
        pass


def _load_saved():
    try:
        payload = json.loads(_STATE_FILE.read_text(encoding="utf-8")) if _STATE_FILE.exists() else {}
    except Exception:
        payload = {}
    for saved in payload.get("jobs", []) if isinstance(payload, dict) else []:
        if not isinstance(saved, dict) or not saved.get("id"):
            continue
        job = dict(saved)
        old_status = str(job.get("status") or "")
        if old_status in {"starting", "downloading", "installing", "canceling", "pausing"}:
            job["status"] = "paused"
            job["stage"] = "Paused"
            job["error"] = "AbyssBeacon stopped before this download finished. Resume to continue the partial file."
            job["speed_bps"] = 0.0
            job["updated_at"] = _now_iso()
        _sync_part_size(job)
        job["_last_bytes"] = int(job.get("downloaded_bytes") or 0)
        job["_last_mono"] = time.monotonic()
        job["_cancel_requested"] = False
        job["_pause_requested"] = False
        # Older builds could persist two rows for the same download when the
        # green button was clicked while a paused job already existed. Collapse
        # those duplicate records on startup without touching the shared .part.
        retry_key = str(job.get("retry_url") or "").strip()
        identity = (int(job.get("model_id") or 0), str(job.get("source") or "").lower(), retry_key or str(job.get("filename") or "").casefold())
        duplicate_id = next((jid for jid, existing in _JOBS.items() if (
            int(existing.get("model_id") or 0),
            str(existing.get("source") or "").lower(),
            str(existing.get("retry_url") or "").strip() or str(existing.get("filename") or "").casefold(),
        ) == identity), None)
        if duplicate_id:
            _JOBS.pop(duplicate_id, None)
        _JOBS[str(job["id"])] = job


def _clean_locked():
    now = time.monotonic()
    remove = []
    for job_id, job in _JOBS.items():
        status = job.get("status")
        finished_age = now - float(job.get("_finished_mono") or now)
        if status == "complete" and finished_age > _COMPLETE_TTL:
            remove.append(job_id)
        elif status == "canceled" and finished_age > _CANCELED_TTL:
            remove.append(job_id)
    for job_id in remove:
        _JOBS.pop(job_id, None)

    while len(_JOBS) > _MAX_JOBS:
        key = next((k for k, v in _JOBS.items() if v.get("status") == "complete"), None)
        if key is None:
            key = next(iter(_JOBS), None)
        if key is None:
            break
        _JOBS.pop(key, None)


def create_job(*, model_id, model_name, source, filename, retry_url="", total_bytes=0):
    job_id = uuid.uuid4().hex
    now = _now_iso()
    with _LOCK:
        _clean_locked()
        _JOBS[job_id] = {
            "id": job_id,
            "model_id": int(model_id or 0),
            "model_name": str(model_name or "Model"),
            "source": str(source or ""),
            "filename": str(filename or "Model file"),
            "retry_url": str(retry_url or ""),
            "status": "starting",
            "stage": "Starting",
            "downloaded_bytes": 0,
            "total_bytes": max(0, int(total_bytes or 0)),
            "speed_bps": 0.0,
            "error": "",
            "part_path": "",
            "created_at": now,
            "updated_at": now,
            "_last_bytes": 0,
            "_last_mono": time.monotonic(),
            "_cancel_requested": False,
            "_pause_requested": False,
        }
        _JOBS.move_to_end(job_id)
        _save_locked()
    return job_id



def find_matching(*, model_id, source, filename, retry_url=""):
    """Return the newest unfinished job for the same model/source/file."""
    target_model = int(model_id or 0)
    target_source = str(source or "").strip().lower()
    target_filename = str(filename or "").strip().casefold()
    target_retry = str(retry_url or "").strip()
    with _LOCK:
        for job in reversed(list(_JOBS.values())):
            if int(job.get("model_id") or 0) != target_model:
                continue
            if str(job.get("source") or "").strip().lower() != target_source:
                continue
            job_retry = str(job.get("retry_url") or "").strip()
            if target_retry and job_retry:
                if job_retry != target_retry:
                    continue
            elif str(job.get("filename") or "").strip().casefold() != target_filename:
                continue
            if job.get("status") in {"starting", "downloading", "installing", "canceling", "pausing", "paused", "failed"}:
                _sync_part_size(job)
                return _public_job(job)
    return None


def reactivate(job_id):
    """Reuse an existing paused/failed job for a real resume attempt."""
    with _LOCK:
        job = _JOBS.get(str(job_id))
        if not job or job.get("status") not in {"paused", "failed"}:
            return False
        _sync_part_size(job)
        job["_cancel_requested"] = False
        job["_pause_requested"] = False
        job["_last_bytes"] = int(job.get("downloaded_bytes") or 0)
        job["_last_mono"] = time.monotonic()
        job.update(status="starting", stage="Resuming", speed_bps=0.0, error="", updated_at=_now_iso())
        _JOBS.move_to_end(str(job_id))
        _save_locked()
        return True

def update(job_id, *, status=None, stage=None, downloaded_bytes=None, total_bytes=None, error=None, filename=None, part_path=None):
    with _LOCK:
        job = _JOBS.get(str(job_id))
        if not job:
            return
        now_mono = time.monotonic()
        if downloaded_bytes is not None:
            downloaded = max(0, int(downloaded_bytes or 0))
            elapsed = max(0.001, now_mono - float(job.get("_last_mono") or now_mono))
            delta = max(0, downloaded - int(job.get("_last_bytes") or 0))
            instant = delta / elapsed
            old = float(job.get("speed_bps") or 0.0)
            job["speed_bps"] = instant if old <= 0 else (old * 0.65 + instant * 0.35)
            job["downloaded_bytes"] = downloaded
            job["_last_bytes"] = downloaded
            job["_last_mono"] = now_mono
        if total_bytes is not None:
            job["total_bytes"] = max(0, int(total_bytes or 0))
        if status is not None: job["status"] = str(status)
        if stage is not None: job["stage"] = str(stage)
        if error is not None: job["error"] = str(error or "")
        if filename is not None: job["filename"] = str(filename or job.get("filename") or "Model file")
        if part_path is not None:
            job["part_path"] = str(part_path or "")
        job["updated_at"] = _now_iso()
        _JOBS.move_to_end(str(job_id))
        _save_locked()


def complete(job_id):
    with _LOCK:
        job = _JOBS.get(str(job_id))
        if not job: return
        total = int(job.get("total_bytes") or 0)
        if total and int(job.get("downloaded_bytes") or 0) < total: job["downloaded_bytes"] = total
        job.update(status="complete", stage="Complete", speed_bps=0.0, error="", updated_at=_now_iso(), _finished_mono=time.monotonic())
        _JOBS.move_to_end(str(job_id)); _save_locked()


def fail(job_id, message):
    with _LOCK:
        job = _JOBS.get(str(job_id))
        if not job: return
        job.update(status="failed", stage="Failed", speed_bps=0.0, error=str(message or "Download failed."), updated_at=_now_iso())
        _JOBS.move_to_end(str(job_id)); _save_locked()



def request_pause(job_id):
    with _LOCK:
        job = _JOBS.get(str(job_id))
        if not job or job.get("status") not in {"starting", "downloading"}:
            return False
        job["_pause_requested"] = True
        job.update(status="pausing", stage="Pausing…", speed_bps=0.0, updated_at=_now_iso())
        _JOBS.move_to_end(str(job_id)); _save_locked(); return True


def pause_requested(job_id):
    with _LOCK:
        job = _JOBS.get(str(job_id)); return bool(job and job.get("_pause_requested"))


def paused(job_id, message="Download paused. Resume to continue the partial file."):
    with _LOCK:
        job = _JOBS.get(str(job_id))
        if not job: return
        job["_pause_requested"] = False
        job.update(status="paused", stage="Paused", speed_bps=0.0, error=str(message or ""), updated_at=_now_iso())
        _JOBS.move_to_end(str(job_id)); _save_locked()

def request_cancel(job_id):
    with _LOCK:
        job = _JOBS.get(str(job_id))
        if not job or job.get("status") not in {"starting", "downloading", "installing"}: return False
        job["_cancel_requested"] = True
        job.update(status="canceling", stage="Canceling…", updated_at=_now_iso())
        _JOBS.move_to_end(str(job_id)); _save_locked(); return True


def cancel_requested(job_id):
    with _LOCK:
        job = _JOBS.get(str(job_id)); return bool(job and job.get("_cancel_requested"))


def canceled(job_id):
    with _LOCK:
        job = _JOBS.get(str(job_id))
        if not job: return
        job.update(status="canceled", stage="Canceled", speed_bps=0.0, error="", updated_at=_now_iso(), _finished_mono=time.monotonic())
        _JOBS.move_to_end(str(job_id)); _save_locked()


def dismiss(job_id):
    with _LOCK:
        result = _JOBS.pop(str(job_id), None) is not None
        _save_locked(); return result


def discard(job_id):
    """Forget a paused/failed job and permanently remove its saved .part file."""
    with _LOCK:
        job = _JOBS.pop(str(job_id), None)
        if not job:
            _save_locked()
            return False
        part_path = str(job.get("part_path") or "").strip()
        if part_path:
            try:
                Path(part_path).unlink(missing_ok=True)
            except Exception:
                # Put the job back if we could not safely discard its bytes.
                _JOBS[str(job_id)] = job
                _save_locked()
                return False
        _save_locked()
        return True


def snapshot():
    with _LOCK:
        _clean_locked()
        items=[]; order={"failed":0,"paused":0,"pausing":1,"downloading":1,"installing":1,"starting":1,"complete":2}
        for job in reversed(list(_JOBS.values())):
            if job.get("status") in {"paused", "failed"}:
                _sync_part_size(job)
            public=_public_job(job); total=int(public.get("total_bytes") or 0); downloaded=int(public.get("downloaded_bytes") or 0)
            public["percent"] = min(100.0,max(0.0,downloaded*100.0/total)) if total>0 else None
            items.append(public)
        items.sort(key=lambda x:(order.get(x.get("status"),1),x.get("created_at") or ""), reverse=False)
        active_count=sum(1 for i in items if i.get("status") in {"starting","downloading","installing","canceling","pausing"})
        failed_count=sum(1 for i in items if i.get("status")=="failed")
        return {"items":items,"active_count":active_count,"failed_count":failed_count}


_load_saved()
