import threading

_status_lock = threading.RLock()

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


def reset_status():
    global scan_progress

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
