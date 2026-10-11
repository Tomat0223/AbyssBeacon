"""Authorize Red downloads in the user's default browser via a local companion."""
import hmac
import json
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_VERIFY_LOCK = threading.Lock()
_BRIDGE_PATH = "/abyssbeacon-red-bridge"


def _storage_url(value):
    parsed = urlparse(str(value or ""))
    approved = (
        parsed.hostname == "b2.civitai.com"
        and parsed.path.startswith("/file/civitai-modelfiles/")
    ) or parsed.hostname == "civitai-delivery-worker-prod.5ac0637cfd0766c97916cefa3764fbdf.r2.cloudflarestorage.com"
    return (parsed.scheme == "https" and approved
            and parsed.port in (None, 443)
            and not parsed.username and not parsed.password)


def resolve_download(target_url, referer="", progress_callback=None, timeout=300):
    """Receive one selected-file link; leave transfer and folders to installer."""
    target = urlparse(str(target_url or ""))
    file_id = parse_qs(target.query).get("fileId", [])
    version_id = target.path.removeprefix("/api/download/models/")
    if (target.scheme != "https" or target.hostname != "civitai.red"
            or not version_id.isdigit() or len(file_id) != 1 or not file_id[0].isdigit()):
        raise RuntimeError("Browser verification requires an exact CivitAI Red file link. Reload the model first.")
    if not _VERIFY_LOCK.acquire(blocking=False):
        raise RuntimeError("Another file is awaiting browser authorization. Finish it, then retry this download.")
    server = None
    thread = None
    token = secrets.token_urlsafe(32)
    captured = threading.Event()
    attached = threading.Event()
    state = {"url": ""}
    state_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log the token or signed storage URL.

        def reply(self, status, payload, kind="application/json"):
            data = (json.dumps(payload) if kind == "application/json" else payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", kind + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def authorized(self):
            return self.valid_host() and hmac.compare_digest(
                str(self.headers.get("Authorization") or ""), "Bearer " + token
            )

        def do_GET(self):
            if not self.valid_host():
                self.reply(403, {"error": "Invalid host"})
                return
            if self.path == _BRIDGE_PATH + "/start":
                self.reply(200, '''<!doctype html><html><head><meta charset="utf-8">
<meta name="abyssbeacon-red-bridge" content="1"><title>AbyssBeacon browser authorization</title>
</head><body style="font:18px system-ui;max-width:760px;margin:70px auto;padding:24px">
<h1>Connecting to AbyssBeacon</h1><p id="bridge-status">The Firefox companion will open the selected CivitAI Red file using your existing browser session.</p>
<p>If this page stays here, load the AbyssBeacon Firefox companion through <strong>about:debugging → This Firefox → Load Temporary Add-on</strong>, then refresh this page.</p>
<p>Your file will be installed by AbyssBeacon using your configured folders. Keep AbyssBeacon running.</p></body></html>''', "text/html")
                return
            if self.path != _BRIDGE_PATH + "/pending" or not self.authorized():
                self.reply(403, {"error": "Unauthorized"})
                return
            if captured.is_set():
                self.reply(409, {"error": "Already handed off"})
                return
            attached.set()
            self.reply(200, {"target_url": target_url, "version_id": version_id,
                             "file_id": file_id[0], "timeout_seconds": timeout})

        def do_POST(self):
            if self.path == _BRIDGE_PATH + "/diagnostic" and self.authorized():
                try:
                    length = int(self.headers.get("Content-Length") or "0")
                    if not 0 < length <= 4096:
                        raise ValueError()
                    payload = json.loads(self.rfile.read(length))
                    event = payload.get("event")
                    if event not in {"paired", "request", "headers", "redirect", "storage", "handoff-start", "handoff-result"}:
                        raise ValueError()
                    fields = [event, "expected=" + version_id + "/" + file_id[0]]
                    for key in ("host", "destination_host", "request_id", "observed_version", "observed_file", "extension"):
                        value = str(payload.get(key) or "")
                        if value and re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", value):
                            fields.append(key + "=" + value)
                    for key in ("tab_id", "status"):
                        value = payload.get(key)
                        if isinstance(value, int) and not isinstance(value, bool):
                            fields.append(key + "=" + str(value))
                    for key in ("matched", "tracked", "allowed_storage", "success"):
                        if isinstance(payload.get(key), bool):
                            fields.append(key + "=" + str(payload[key]))
                    print("RED COMPANION:", " ".join(fields))
                    self.reply(200, {"success": True})
                except (ValueError, TypeError, AttributeError):
                    self.reply(400, {"error": "Invalid diagnostic"})
                return
            if self.path != _BRIDGE_PATH + "/capture" or not self.authorized():
                self.reply(403, {"error": "Unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
                if not 0 < length <= 32768:
                    raise ValueError()
                payload = json.loads(self.rfile.read(length))
                url = payload.get("url", "")
                if (str(payload.get("version_id")) != version_id
                        or str(payload.get("file_id")) != file_id[0] or not _storage_url(url)):
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                self.reply(400, {"error": "Selected file or storage URL did not match"})
                return
            with state_lock:
                if state["url"] and state["url"] != url:
                    self.reply(409, {"error": "Already handed off"})
                    return
                state["url"] = url
            # Acknowledge before waking the installer; otherwise it could close
            # the listener while the extension is still waiting for confirmation.
            self.reply(200, {"success": True})
            captured.set()

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if callable(progress_callback):
            progress_callback(stage="Waiting for Firefox companion")
        print("CivitAI Red: opening your default browser. The Firefox companion will hand the file back to the local installer.")
        url = f"http://127.0.0.1:{server.server_port}{_BRIDGE_PATH}/start#{token}"
        if not webbrowser.open(url, new=2):
            raise RuntimeError("Could not open your default browser for download authorization.")
        deadline = time.monotonic() + timeout
        while not captured.is_set() and time.monotonic() < deadline:
            if callable(progress_callback):
                progress_callback(stage=("Waiting for browser authorization — complete any verification in Firefox"
                                         if attached.is_set() else "Waiting for Firefox companion — load the add-on and refresh its page"))
            captured.wait(.25)
        if not captured.is_set():
            if not attached.is_set():
                raise RuntimeError("Firefox companion did not connect. Load its manifest.json in about:debugging, then retry.")
            raise RuntimeError("Browser authorization timed out. Complete verification in your normal Firefox and retry the selected file.")
        return state["url"], {"Referer": "https://civitai.red/", "Accept": "*/*"}
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)
        _VERIFY_LOCK.release()
