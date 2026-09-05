import sys
import threading

_status_lock = threading.RLock()
_terminal_lock = threading.RLock()
_terminal_width = 0
_terminal_active = False

scan_running = False

scan_progress = {
    "status": "idle",
    "source": "",
    "current": "",
    "processed": 0,
    "added": 0,
    "updated": 0,
    "media": 0,
    "images": 0,
    "videos": 0,
    "started": "",
    "finished": "",
    "message": "",
    "sources": {},
}


def update_status(
    status=None,
    source=None,
    current=None,
    processed=None,
    added=None,
    updated=None,
    media=None,
    images=None,
    videos=None,
    message=None,
    sources=None,
):
    global scan_progress

    with _status_lock:
        if status is not None:
            scan_progress["status"] = status
        if source is not None:
            scan_progress["source"] = source
        if current is not None:
            scan_progress["current"] = current
        if added is not None:
            scan_progress["added"] = added
        if message is not None:
            scan_progress["message"] = message
        if processed is not None:
            scan_progress["processed"] = processed
        if updated is not None:
            scan_progress["updated"] = updated
        if media is not None:
            scan_progress["media"] = media
        if images is not None:
            scan_progress["images"] = images
        if videos is not None:
            scan_progress["videos"] = videos
        if sources is not None:
            scan_progress["sources"] = {
                str(name): dict(value or {})
                for name, value in dict(sources).items()
            }


def initialize_sources(source_names):
    """Publish the selected source list before workers start.

    The UI uses this to show every selected provider at once instead of only the
    last worker that happened to report progress.
    """
    with _status_lock:
        scan_progress["sources"] = {
            str(name): {
                "status": "scanning",
                "processed": 0,
                "added": 0,
                "updated": 0,
                "images": 0,
                "videos": 0,
                "message": "",
                "progress_current": 0,
                "progress_total": 0,
                "progress_stage": "",
                "progress_label": "",
            }
            for name in source_names
        }


def update_source_progress(
    source,
    status=None,
    processed=None,
    added=None,
    updated=None,
    images=None,
    videos=None,
    message=None,
    progress_current=None,
    progress_total=None,
    progress_stage=None,
    progress_label=None,
):
    with _status_lock:
        sources = scan_progress.setdefault("sources", {})
        item = sources.setdefault(str(source), {
            "status": "scanning",
            "processed": 0,
            "added": 0,
            "updated": 0,
            "images": 0,
            "videos": 0,
            "message": "",
            "progress_current": 0,
            "progress_total": 0,
            "progress_stage": "",
            "progress_label": "",
        })
        if status is not None:
            item["status"] = status
        if processed is not None:
            item["processed"] = processed
        if added is not None:
            item["added"] = added
        if updated is not None:
            item["updated"] = updated
        if images is not None:
            item["images"] = images
        if videos is not None:
            item["videos"] = videos
        if message is not None:
            item["message"] = message
        if progress_current is not None:
            item["progress_current"] = max(0, int(progress_current or 0))
        if progress_total is not None:
            item["progress_total"] = max(0, int(progress_total or 0))
        if progress_stage is not None:
            item["progress_stage"] = str(progress_stage or "")
        if progress_label is not None:
            item["progress_label"] = str(progress_label or "")


def single_source_active(source=None):
    with _status_lock:
        sources = scan_progress.get("sources", {}) or {}
        if len(sources) != 1:
            return False
        if source is None:
            return True
        return str(source) in sources


def write_terminal_progress(text, finalize=False):
    """Rewrite one terminal line in place without creating log spam.

    This is intentionally a single-line primitive. Callers should only use it
    when one source is active; concurrent source scans keep ordinary line logs.
    """
    global _terminal_width, _terminal_active
    text = str(text or "")
    with _terminal_lock:
        width = max(_terminal_width, len(text))
        try:
            sys.stdout.write("\r" + text.ljust(width))
            if finalize:
                sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            return
        _terminal_width = 0 if finalize else width
        _terminal_active = not finalize


def finish_terminal_progress():
    """Terminate an active in-place progress line before normal line output."""
    global _terminal_width, _terminal_active
    with _terminal_lock:
        if not _terminal_active:
            return
        try:
            sys.stdout.write("\n")
            sys.stdout.flush()
        except Exception:
            pass
        _terminal_width = 0
        _terminal_active = False


def reset_status():
    global scan_progress

    finish_terminal_progress()
    with _status_lock:
        scan_progress = {
            "status": "idle",
            "source": "",
            "current": "",
            "processed": 0,
            "added": 0,
            "updated": 0,
            "media": 0,
            "images": 0,
            "videos": 0,
            "message": "",
            "sources": {},
        }


def get_status():
    # Return a deep-enough snapshot so Flask/JS never observes a dictionary
    # halfway through an update while multiple source workers report progress.
    with _status_lock:
        result = dict(scan_progress)
        result["sources"] = {
            key: dict(value)
            for key, value in scan_progress.get("sources", {}).items()
        }
        return result


_source_health = {}


def update_source_health(source, status, message=""):
    with _status_lock:
        _source_health[source] = {"status": status, "message": message}


def get_source_health():
    with _status_lock:
        return {k: dict(v) for k, v in _source_health.items()}


def reset_source_health():
    with _status_lock:
        _source_health.clear()
