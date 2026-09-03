"""SeaArt-only browser-session helper.

SeaArt signs discovery requests in its own frontend.  The connection step deliberately
launches the user's installed browser *normally* (no Selenium/WebDriver/Playwright), so
Google/SeaArt login sees an ordinary browser.  AbyssBeacon uses an isolated local browser
profile. The user finishes the connection explicitly after signing in so browser timing or stale session state cannot interrupt the login flow.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from settings_manager import load_settings, save_settings

_ROOT = Path(__file__).resolve().parent
_PROFILE_ROOT = _ROOT / "browser_profiles" / "seaart"
_MARKER = _PROFILE_ROOT / "modelradar_session.json"
_LOCK = threading.RLock()
_THREAD = None
_PROCESS = None
_PROCESS_BROWSER = ""
_PROCESS_PROFILE = ""
_STOP = threading.Event()
_FINISH = threading.Event()
_STATE = {"state":"idle","message":"SeaArt browser session is not connected.","connected":False,"display_name":""}
_LIVE_LOCK = threading.RLock()
_ALLOWED_BROWSERS = {"firefox", "chrome", "edge"}


def _utc_now(): return datetime.now(timezone.utc).isoformat()


def preferred_browser():
    try:
        value = str(load_settings().get("preferences", {}).get("seaart_browser", "firefox")).strip().lower()
    except Exception:
        value = "firefox"
    return value if value in _ALLOWED_BROWSERS else "firefox"


def set_preferred_browser(value):
    value = str(value or "").strip().lower()
    if value not in _ALLOWED_BROWSERS:
        raise ValueError("Choose Firefox, Chrome, or Microsoft Edge.")
    settings = load_settings()
    settings.setdefault("preferences", {})["seaart_browser"] = value
    save_settings(settings)
    return value


def _read_marker():
    try:
        data = json.loads(_MARKER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_marker(display_name="", browser=""):
    _PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    _MARKER.write_text(json.dumps({
        "connected_at": _utc_now(),
        "display_name": str(display_name or "").strip(),
        "browser": browser or preferred_browser(),
    }, indent=2), encoding="utf-8")


def _set_state(state, message, *, connected=None, display_name=None):
    with _LOCK:
        _STATE["state"] = str(state)
        _STATE["message"] = str(message)
        if connected is not None: _STATE["connected"] = bool(connected)
        if display_name is not None: _STATE["display_name"] = str(display_name or "").strip()


def browser_session_status():
    marker = _read_marker()
    with _LOCK:
        result = dict(_STATE)
        alive = bool(_THREAD and _THREAD.is_alive())
    if not marker and not alive:
        recovered = _detect_existing_profile()
        if recovered:
            _write_marker(browser=recovered)
            marker = _read_marker()
            result.update({"state":"saved", "connected":True, "message":"SeaArt browser session recovered from the existing local profile."})
    if marker and not alive and result.get("state") in {"idle", "error"}:
        name = str(marker.get("display_name") or "").strip()
        result.update({
            "state":"saved", "connected":True, "display_name":name,
            "message":f"SeaArt browser session saved{(' for '+name) if name else ''}.",
        })
    result.update({
        "running":alive,
        "profile_saved":bool(marker),
        "preferred_browser":preferred_browser(),
        "python_executable":sys.executable,
    })
    return result


def browser_session_saved(): return bool(_read_marker())


def _candidate_executables(browser):
    pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    pfx86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    if browser == "firefox":
        return [shutil.which("firefox"), Path(pf)/"Mozilla Firefox"/"firefox.exe", Path(pfx86)/"Mozilla Firefox"/"firefox.exe"]
    if browser == "chrome":
        return [shutil.which("chrome"), Path(pf)/"Google"/"Chrome"/"Application"/"chrome.exe", Path(pfx86)/"Google"/"Chrome"/"Application"/"chrome.exe", Path(local)/"Google"/"Chrome"/"Application"/"chrome.exe"]
    return [shutil.which("msedge"), Path(pf)/"Microsoft"/"Edge"/"Application"/"msedge.exe", Path(pfx86)/"Microsoft"/"Edge"/"Application"/"msedge.exe", Path(local)/"Microsoft"/"Edge"/"Application"/"msedge.exe"]


def _browser_executable(browser):
    for candidate in _candidate_executables(browser):
        if not candidate: continue
        path = Path(candidate)
        if path.is_file(): return str(path)
    label = {"firefox":"Firefox", "chrome":"Google Chrome", "edge":"Microsoft Edge"}[browser]
    raise RuntimeError(f"{label} was not found on this computer. Choose another browser in SeaArt settings.")


def _launch_browser(browser):
    global _PROCESS, _PROCESS_BROWSER, _PROCESS_PROFILE
    exe = _browser_executable(browser)
    profile = _PROFILE_ROOT / browser
    profile.mkdir(parents=True, exist_ok=True)
    url = "https://www.seaart.ai/?openLoginDialog=1"
    if browser == "firefox":
        # -no-remote + -profile gives AbyssBeacon a completely isolated Firefox session.
        args = [exe, "-no-remote", "-profile", str(profile), "--width", "900", "--height", "700", "-new-window", url]
    else:
        # Chrome/Edge use a dedicated user-data directory and otherwise launch normally.
        args = [exe, f"--user-data-dir={profile}", "--profile-directory=Default", "--window-size=900,700", "--window-position=140,90", "--new-window", url]
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    _PROCESS = subprocess.Popen(args, creationflags=flags)
    _PROCESS_BROWSER = browser
    _PROCESS_PROFILE = str(profile.resolve())
    return _PROCESS


def _decode_jwt_payload(token):
    try:
        parts = str(token or "").strip().split(".")
        if len(parts) != 3 or not parts[0].startswith("eyJ"):
            return {}
        raw = parts[1] + ("=" * (-len(parts[1]) % 4))
        data = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _looks_like_seaart_auth_token(value, expiry=0):
    """Conservatively identify SeaArt's signed-in T cookie.

    SeaArt's authenticated T value is a JWT.  Merely finding a cookie row is not
    enough: Firefox can retain an empty/stale T row after logout and the login page
    can create cookie state before the user has authenticated.
    """
    token = str(value or "").strip()
    if len(token) < 40 or token.count(".") != 2 or not token.startswith("eyJ"):
        return False
    try:
        expiry = int(expiry or 0)
    except (TypeError, ValueError):
        expiry = 0
    now = int(time.time())
    if expiry and expiry <= now:
        return False
    payload = _decode_jwt_payload(token)
    if not payload:
        return False
    try:
        jwt_exp = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        jwt_exp = 0
    if jwt_exp and jwt_exp <= now:
        return False
    return True


def _cookie_fingerprint(value):
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        raw = value
    else:
        raw = str(value or "").encode("utf-8", "ignore")
    return hashlib.sha256(raw).hexdigest() if raw else ""


def _cookie_state_firefox(profile):
    db = profile / "cookies.sqlite"
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=.25)
        try:
            rows = conn.execute(
                "SELECT value, expiry, lastAccessed, creationTime FROM moz_cookies "
                "WHERE lower(name)='t' AND lower(host) LIKE '%seaart.ai' "
                "ORDER BY lastAccessed DESC"
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return None
    for value, expiry, last_accessed, creation_time in rows:
        if not _looks_like_seaart_auth_token(value, expiry):
            continue
        return {
            "fingerprint": _cookie_fingerprint(value),
            "verified": True,
            "changed_at": int(last_accessed or creation_time or 0),
        }
    return None


def _cookie_state_chromium(profile):
    candidates = [profile/"Default"/"Network"/"Cookies", profile/"Default"/"Cookies"]
    for db in candidates:
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=.25)
            try:
                rows = conn.execute(
                    "SELECT value, encrypted_value, last_update_utc, creation_utc FROM cookies "
                    "WHERE lower(name)='t' AND lower(host_key) LIKE '%seaart.ai' "
                    "ORDER BY last_update_utc DESC"
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            continue
        for value, encrypted_value, last_update, creation_time in rows:
            # Some Chromium builds expose the plaintext value; when they do, use
            # the same strong JWT check as Firefox.  Windows normally encrypts it,
            # so in that case the watcher can only detect a *change* in the encrypted
            # cookie after the login page has settled.
            if _looks_like_seaart_auth_token(value):
                return {
                    "fingerprint": _cookie_fingerprint(value),
                    "verified": True,
                    "changed_at": int(last_update or creation_time or 0),
                }
            blob = bytes(encrypted_value or b"")
            if blob:
                return {
                    "fingerprint": _cookie_fingerprint(blob),
                    "verified": False,
                    "changed_at": int(last_update or creation_time or 0),
                }
    return None


def _seaart_cookie_state(browser):
    profile = _PROFILE_ROOT / browser
    return _cookie_state_firefox(profile) if browser == "firefox" else _cookie_state_chromium(profile)


def _seaart_cookie_present(browser, *, allow_unverified=False):
    state = _seaart_cookie_state(browser)
    return bool(state and (state.get("verified") or allow_unverified))


def _plaintext_profile_cookies(browser):
    """Read SeaArt cookies that the local browser stores in plaintext.

    Firefox stores cookie values directly in cookies.sqlite, which lets AbyssBeacon
    perform a harmless server-side account check while the normal login window is
    still open. Chromium normally encrypts cookie values on Windows; when that is
    the case this returns only any plaintext values that are actually available.
    """
    profile = _PROFILE_ROOT / browser
    rows = []
    if browser == "firefox":
        db = profile / "cookies.sqlite"
        if not db.exists():
            return {}
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=.35)
            try:
                rows = conn.execute(
                    "SELECT name, value, expiry, lastAccessed FROM moz_cookies "
                    "WHERE lower(host) LIKE '%seaart.ai' ORDER BY lastAccessed DESC"
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            return {}
        now = int(time.time())
        out = {}
        for name, value, expiry, _last_accessed in rows:
            lname = str(name or "").strip().casefold()
            text = str(value or "").strip()
            try:
                expired = bool(expiry and int(expiry) <= now)
            except (TypeError, ValueError):
                expired = False
            if lname and text and not expired and lname not in out:
                out[lname] = (str(name), text)
        return out

    candidates = [profile/"Default"/"Network"/"Cookies", profile/"Default"/"Cookies"]
    for db in candidates:
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=.35)
            try:
                rows = conn.execute(
                    "SELECT name, value, last_update_utc FROM cookies "
                    "WHERE lower(host_key) LIKE '%seaart.ai' ORDER BY last_update_utc DESC"
                ).fetchall()
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            continue
        out = {}
        for name, value, _last_update in rows:
            lname = str(name or "").strip().casefold()
            text = str(value or "").strip()
            if lname and text and lname not in out:
                out[lname] = (str(name), text)
        if out:
            return out
    return {}


def _verify_logged_in_account_from_profile(browser, timeout=7):
    """Ask SeaArt whether the *currently open* isolated profile is signed in.

    A JWT-shaped T cookie is only a candidate signal: SeaArt can retain or rotate
    cookie state while logged out.  A connection is accepted only after
    /api/v1/account/my returns success plus a real account id.  No credential value
    is logged or persisted by this probe.
    """
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return False, ""
    cookies = _plaintext_profile_cookies(browser)
    required = ("t", "deviceid", "browserid")
    if any(not cookies.get(name, ("", ""))[1] for name in required):
        return False, ""

    allowed = ("t", "deviceid", "graytag", "pageid", "browserid", "app_id", "x-eyes", "lang")
    cookie_parts = []
    for lname in allowed:
        original, value = cookies.get(lname, ("", ""))
        if original and value:
            cookie_parts.append(f"{original}={value}")
    if not cookie_parts:
        return False, ""

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "Content-Type": "application/json",
        "Origin": "https://www.seaart.ai",
        "Referer": "https://www.seaart.ai/personal",
        "X-Platform": "web",
        "X-Project-Id": "seaart",
        "X-App-Id": "web_global_seaart",
        "X-Request-Id": str(uuid.uuid4()),
        "Cookie": "; ".join(cookie_parts),
    }
    mappings = {
        "deviceid": "X-Device-Id",
        "graytag": "X-Gray-Tag",
        "browserid": "X-Browser-Id",
        "pageid": "X-Page-Id",
        "x-eyes": "X-Eyes",
    }
    for cookie_name, header_name in mappings.items():
        value = cookies.get(cookie_name, ("", ""))[1]
        if value:
            headers[header_name] = value

    command = [
        curl, "--silent", "--show-error", "--compressed",
        "--request", "POST", "--max-time", str(max(4, int(timeout))),
        "--write-out", "\n__AB_HTTP__:%{http_code}",
        "https://www.seaart.ai/api/v1/account/my",
    ]
    for name, value in headers.items():
        command.extend(["--header", f"{name}: {value}"])
    command.extend(["--data-raw", '{"show_exp_level":true}'])
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(8, int(timeout) + 3),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False, ""
    if proc.returncode != 0:
        return False, ""
    stdout = proc.stdout or ""
    marker = "\n__AB_HTTP__:"
    if marker in stdout:
        raw, code_text = stdout.rsplit(marker, 1)
    else:
        raw, code_text = stdout, "0"
    try:
        http_code = int(code_text.strip().splitlines()[0])
    except Exception:
        http_code = 0
    if http_code < 200 or http_code >= 300:
        return False, ""
    try:
        payload = json.loads(raw)
    except Exception:
        return False, ""
    status = payload.get("status") if isinstance(payload, dict) else {}
    data = payload.get("data") if isinstance(payload, dict) else {}
    ok = (
        isinstance(status, dict)
        and status.get("code") in (10000, "10000")
        and isinstance(data, dict)
        and bool(data.get("id"))
    )
    if not ok:
        return False, ""
    return True, str(data.get("name") or "").strip()


def _detect_existing_profile():
    """Recover only a profile SeaArt itself still recognizes as signed in.

    A leftover T cookie is not sufficient because logout can leave stale cookie rows
    behind.  Firefox can be positively verified from its plaintext profile cookies.
    Chromium profiles with encrypted cookies are intentionally not auto-recovered; the
    user can reconnect and explicitly Finish Connection instead.
    """
    order = [preferred_browser()] + [b for b in ("firefox", "chrome", "edge") if b != preferred_browser()]
    for browser in order:
        try:
            verified, _display_name = _verify_logged_in_account_from_profile(browser)
            if verified:
                return browser
        except Exception:
            pass
    return ""


def _finalize_after_browser_close(browser, label, *, manual=False, baseline_fingerprint=""):
    """Verify a closed SeaArt profile before saving it as connected.

    Firefox is verified against SeaArt's account endpoint. Chromium may not expose
    plaintext cookies on Windows, so an explicit Finish Connection remains the
    conservative fallback there.
    """
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        verified, display_name = _verify_logged_in_account_from_profile(browser)
        if verified:
            _write_marker(display_name=display_name, browser=browser)
            suffix = " after Finish Connection" if manual else ""
            _set_state(
                "connected",
                f"SeaArt connected in {label}{suffix}.",
                connected=True,
                display_name=display_name,
            )
            return True
        # Chrome/Edge cookie values are normally encrypted on Windows.  Only an
        # explicit user Finish may use their existing conservative fallback.
        if manual and browser != "firefox":
            state = _seaart_cookie_state(browser)
            if state:
                _write_marker(browser=browser)
                _set_state("connected", f"SeaArt connected in {label} after Finish Connection.", connected=True)
                return True
        time.sleep(.45)
    return False


def _profile_process_ids(browser, profile_path):
    """Find browser processes launched with AbyssBeacon's isolated SeaArt profile.

    Firefox/Chromium can hand the visible window off to another process and let the
    original Popen PID exit.  Matching the unique profile path gives us a narrow
    fallback that does not touch the user's normal browser windows.
    """
    if os.name != "nt" or not profile_path:
        return []
    names = {"firefox":"firefox.exe", "chrome":"chrome.exe", "edge":"msedge.exe"}
    wanted = names.get(browser, "")
    if not wanted:
        return []
    script = (
        "$p=$env:MODELRADAR_SEAART_PROFILE; "
        "$n=$env:MODELRADAR_SEAART_BROWSER_EXE; "
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq $n -and $_.CommandLine -and $_.CommandLine.Contains($p) } | "
        "ForEach-Object { $_.ProcessId }"
    )
    env = os.environ.copy()
    env["MODELRADAR_SEAART_PROFILE"] = str(profile_path)
    env["MODELRADAR_SEAART_BROWSER_EXE"] = wanted
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=6, env=env,
        )
        return [int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit()]
    except Exception:
        return []


def _close_launched_browser():
    global _PROCESS, _PROCESS_BROWSER, _PROCESS_PROFILE
    process = _PROCESS
    browser = _PROCESS_BROWSER
    profile_path = _PROCESS_PROFILE
    _PROCESS = None
    _PROCESS_BROWSER = ""
    _PROCESS_PROFILE = ""

    # Start with the PID returned by Popen, then fall back to the browser process
    # that owns AbyssBeacon's unique SeaArt profile.  This handles Firefox handing
    # the visible window to a different process while keeping normal Firefox safe.
    pids = []
    if process and process.poll() is None:
        pids.append(process.pid)
    for pid in _profile_process_ids(browser, profile_path):
        if pid not in pids:
            pids.append(pid)
    if not pids:
        return

    if os.name == "nt":
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            except Exception:
                pass
        time.sleep(.6)
        # Force only any isolated-profile process that survived the normal close.
        for pid in _profile_process_ids(browser, profile_path):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
            except Exception:
                pass
        return

    if process and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


def _normal_browser_worker():
    browser = preferred_browser()
    label = {"firefox":"Firefox", "chrome":"Google Chrome", "edge":"Microsoft Edge"}[browser]
    try:
        _PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
        _set_state("opening", f"Opening SeaArt in {label}…", connected=False)
        process = _launch_browser(browser)
        waiting_message = (
            f"Sign in to SeaArt in the {label} window. When SeaArt is visibly signed in, return to "
            "AbyssBeacon and click Finish Connection. The login window will stay open until you finish it or close it yourself."
        )
        _set_state("waiting", waiting_message, connected=False)
        deadline = time.monotonic() + 15*60

        # Firefox can let the PID returned by Popen exit while handing the visible
        # window to another firefox.exe process using the same isolated profile.
        # Do not mistake that launcher hand-off for the user closing the SeaArt
        # window.  Once a hand-off is observed, re-check the profile-owned process
        # about once per second instead of killing it in the worker's finally block.
        profile_path = str((_PROFILE_ROOT / browser).resolve())
        handed_off_alive = False
        next_handoff_probe = 0.0

        while time.monotonic() < deadline and not _STOP.is_set():
            if _FINISH.is_set():
                _set_state("verifying", "Finishing SeaArt connection and verifying the signed-in account…", connected=False)
                _close_launched_browser()
                if _finalize_after_browser_close(browser, label, manual=True):
                    return
                _set_state("error", "SeaArt is not signed in yet. Reopen Connect SeaArt, finish signing in, then try Finish Connection again.", connected=False)
                return

            if process.poll() is not None:
                now = time.monotonic()
                if now >= next_handoff_probe:
                    handed_off_alive = bool(_profile_process_ids(browser, profile_path))
                    next_handoff_probe = now + 1.0
                if not handed_off_alive:
                    if _finalize_after_browser_close(browser, label):
                        return
                    _set_state("idle", "SeaArt connection window was closed before a signed-in SeaArt account could be verified.", connected=False)
                    return

            # Deliberately do not auto-detect/auto-close here.  Browser cookie/process
            # timing differs enough between Firefox/Chrome/Edge that automatic closing
            # can interrupt a real login flow.  Finish Connection is the authoritative
            # completion action; manually closing the isolated window is still detected
            # above and verified conservatively.
            time.sleep(.35)
        _set_state("idle" if _STOP.is_set() else "error", "SeaArt connection window closed." if _STOP.is_set() else "SeaArt sign-in timed out. Click Connect SeaArt to try again.", connected=browser_session_saved())
    except Exception as exc:
        _set_state("error", f"SeaArt connection failed: {exc}", connected=browser_session_saved())
    finally:
        _close_launched_browser()


def _browser_worker():
    global _THREAD
    try:
        _normal_browser_worker()
    finally:
        with _LOCK: _THREAD = None
        _STOP.clear()
        _FINISH.clear()


def start_browser_connection():
    global _THREAD
    with _LOCK:
        if _THREAD and _THREAD.is_alive(): return browser_session_status()
        # Reconnect is a fresh authentication attempt.  Do not let a marker from a
        # previous session make a failed/logged-out reconnect look connected again.
        try:
            _MARKER.unlink(missing_ok=True)
        except OSError:
            pass
        _STOP.clear()
        _FINISH.clear()
        _THREAD = threading.Thread(target=_browser_worker, name="SeaArtBrowserConnect", daemon=True)
        _THREAD.start()
    return browser_session_status()


def finish_browser_connection():
    with _LOCK:
        running = bool(_THREAD and _THREAD.is_alive())
    if not running:
        recovered = _detect_existing_profile()
        if recovered:
            _write_marker(browser=recovered)
            label = {"firefox":"Firefox", "chrome":"Google Chrome", "edge":"Microsoft Edge"}[recovered]
            _set_state("connected", f"SeaArt connected in {label}.", connected=True)
        else:
            _set_state("error", "No SeaArt login window is currently open and no signed-in SeaArt browser profile was found.", connected=browser_session_saved())
        return browser_session_status()
    _FINISH.set()
    _set_state("verifying", "Finishing SeaArt connection…", connected=False)
    return browser_session_status()


def stop_browser_connection():
    _STOP.set()
    return browser_session_status()


def disconnect_browser_session():
    stop_browser_connection()
    _close_launched_browser()
    for _ in range(30):
        with _LOCK: alive = bool(_THREAD and _THREAD.is_alive())
        if not alive: break
        time.sleep(.1)
    try:
        if _PROFILE_ROOT.exists(): shutil.rmtree(_PROFILE_ROOT)
    except OSError as exc:
        _set_state("error", f"Could not remove the SeaArt browser profile: {exc}", connected=browser_session_saved())
        return browser_session_status()
    _set_state("idle", "SeaArt browser session disconnected.", connected=False, display_name="")
    return browser_session_status()

# ---------------------------------------------------------------------------
# Phase 2: live SeaArt browser client
# ---------------------------------------------------------------------------

class SeaArtLiveSession:
    """Headless SeaArt session using the already-authenticated isolated profile.

    Login itself is intentionally *not* automated.  This class is only used after
    the user has completed the normal-browser Connect SeaArt flow.
    """

    def __init__(self):
        self.browser = str((_read_marker().get("browser") or preferred_browser())).strip().lower()
        if self.browser not in _ALLOWED_BROWSERS:
            self.browser = preferred_browser()
        self.driver = None

    def __enter__(self):
        if not browser_session_saved():
            raise RuntimeError("SeaArt Browser Session is not connected. Open Source Accounts and click Connect SeaArt.")
        _LIVE_LOCK.acquire()
        try:
            self.driver = self._open_driver()
            return self
        except Exception:
            _LIVE_LOCK.release()
            raise

    def __exit__(self, exc_type, exc, tb):
        try:
            self.close()
        finally:
            _LIVE_LOCK.release()

    def _open_driver(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.firefox.options import Options as FirefoxOptions
            from selenium.webdriver.chrome.options import Options as ChromeOptions
            from selenium.webdriver.edge.options import Options as EdgeOptions
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                f"SeaArt live scanning needs Selenium in AbyssBeacon's Python ({sys.executable}). "
                "Restart with Start AbyssBeacon.bat so requirements are installed in the project venv."
            ) from exc

        exe = _browser_executable(self.browser)
        profile = (_PROFILE_ROOT / self.browser).resolve()
        profile.mkdir(parents=True, exist_ok=True)

        active = _profile_process_ids(self.browser, str(profile))
        if active:
            # If Connect/Reconnect is genuinely still running, do not interrupt it.
            with _LOCK:
                connecting = bool(_THREAD and _THREAD.is_alive())
            if connecting:
                raise RuntimeError("The SeaArt login browser is still open. Finish or close that SeaArt window, then retry the scan.")

            # Firefox can leave an isolated-profile process alive after the login window
            # closes (especially around a browser update).  It belongs only to
            # AbyssBeacon's SeaArt profile, so clean it up before opening the headless
            # scan instance instead of making the user reconnect.
            if os.name == "nt":
                for pid in active:
                    try:
                        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
                    except Exception:
                        pass
                time.sleep(.8)
            active = _profile_process_ids(self.browser, str(profile))
            if active:
                raise RuntimeError("SeaArt's isolated Firefox profile is still busy. Close the SeaArt window and retry the scan.")
        # A forced/automatic close can leave harmless stale profile lock files behind.
        for lock_name in ("parent.lock", ".parentlock", "lock", "SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                lock_path = profile / lock_name
                if lock_path.exists() or lock_path.is_symlink():
                    lock_path.unlink()
            except OSError:
                pass

        if self.browser == "firefox":
            options = FirefoxOptions()
            options.enable_bidi = True
            options.binary_location = exe
            options.add_argument("-headless")
            options.add_argument("-profile")
            options.add_argument(str(profile))
            driver = webdriver.Firefox(options=options)
        elif self.browser == "chrome":
            options = ChromeOptions()
            options.enable_bidi = True
            options.binary_location = exe
            options.add_argument("--headless=new")
            options.add_argument(f"--user-data-dir={profile}")
            options.add_argument("--profile-directory=Default")
            options.add_argument("--window-size=1100,900")
            driver = webdriver.Chrome(options=options)
        else:
            options = EdgeOptions()
            options.enable_bidi = True
            options.binary_location = exe
            options.add_argument("--headless=new")
            options.add_argument(f"--user-data-dir={profile}")
            options.add_argument("--profile-directory=Default")
            options.add_argument("--window-size=1100,900")
            driver = webdriver.Edge(options=options)

        try:
            driver.set_page_load_timeout(60)
            driver.set_script_timeout(45)
        except Exception:
            pass
        return driver

    def close(self):
        driver, self.driver = self.driver, None
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    @staticmethod
    def _model_ids_from_html(source):
        import re as _re
        ids = []
        seen = set()
        for match in _re.finditer(r"/(?:models|model)/detail/([A-Za-z0-9_-]{8,})", str(source or "")):
            value = match.group(1)
            if value not in seen:
                seen.add(value)
                ids.append(value)
        return ids

    def _model_ids_from_dom(self):
        """Collect only model-card URLs instead of serializing the entire page source.

        Firefox/Geckodriver can fail GET_PAGE_SOURCE on SeaArt pages containing a
        malformed JS/CSS escape ("unexpected end of hex escape"). Returning a small
        list of href strings avoids that serializer path completely.
        """
        if self.driver is None:
            return []
        script = r"""
            const out = [];
            const seen = new Set();
            for (const el of document.querySelectorAll('a[href]')) {
                const href = el.getAttribute('href') || el.href || '';
                if (!/\/(?:models|model)\/detail\/[A-Za-z0-9_-]{8,}/.test(href)) continue;
                if (!seen.has(href)) { seen.add(href); out.push(href); }
            }
            return out;
        """
        try:
            hrefs = self.driver.execute_script(script) or []
        except Exception:
            return []
        ids = []
        seen = set()
        import re as _re
        for href in hrefs:
            match = _re.search(r"/(?:models|model)/detail/([A-Za-z0-9_-]{8,})", str(href or ""))
            if not match:
                continue
            value = match.group(1)
            if value not in seen:
                seen.add(value)
                ids.append(value)
        return ids

    def _install_catalog_response_capture(self):
        """Capture SeaArt's real catalog cards from same-page XHR/fetch responses.

        SeaArt's current ``/square/v3/model/list`` schema exposes the public model
        identifier directly at ``data.items[*].id``. Preserve those top-level card
        objects instead of recursively guessing at nested ids; ``child[*].id`` is a
        different internal object and must never be used as a model detail id.
        """
        if self.driver is None:
            return False
        script = r"""
            try {
                if (!window.__abyssSeaartCatalogCaptureInstalled) {
                    window.__abyssSeaartCatalogCaptureInstalled = true;
                    window.__abyssSeaartCatalogCards = [];

                    const wanted = (url) => {
                        url = String(url || '');
                        return url.includes('/api/v1/square/v3/model/list') ||
                               url.includes('/api/v1/square/v3/model/recommend');
                    };
                    const addCards = (payload) => {
                        try {
                            const current = window.__abyssSeaartCatalogCards ||
                                (window.__abyssSeaartCatalogCards = []);
                            const byId = new Map(current.map(card => [String(card && card.id || ''), card]));

                            const data = payload && typeof payload === 'object' ? payload.data : null;
                            let cards = data && Array.isArray(data.items) ? data.items : [];

                            // Recommended has used slightly different wrappers in older
                            // SeaArt builds. Keep a narrow direct-array fallback, but never
                            // recurse into child/version/media objects looking for ids.
                            if (!cards.length && data && typeof data === 'object') {
                                const direct = ['list', 'models', 'model_list', 'recommend_list'];
                                for (const key of direct) {
                                    if (Array.isArray(data[key])) {
                                        cards = data[key];
                                        break;
                                    }
                                }
                            }

                            for (const raw of cards) {
                                if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
                                const id = String(raw.id ?? raw.model_id ?? raw.model_no ?? '').trim();
                                const title = String(raw.title ?? raw.name ?? '').trim();
                                if (!id || !title) continue;
                                const card = Object.assign({}, raw, {id, model_id: id});
                                byId.set(id, card);
                            }
                            window.__abyssSeaartCatalogCards = Array.from(byId.values()).slice(-1200);
                        } catch (_) {}
                    };

                    const originalFetch = window.fetch;
                    if (typeof originalFetch === 'function') {
                        window.fetch = function(...args) {
                            const requestUrl = args && args.length ?
                                (typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url)) : '';
                            return originalFetch.apply(this, args).then(response => {
                                try {
                                    if (wanted(requestUrl || response.url)) {
                                        response.clone().json().then(addCards).catch(() => {});
                                    }
                                } catch (_) {}
                                return response;
                            });
                        };
                    }

                    const proto = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
                    if (proto && !proto.__abyssSeaartCapturePatched) {
                        proto.__abyssSeaartCapturePatched = true;
                        const originalOpen = proto.open;
                        const originalSend = proto.send;
                        proto.open = function(method, url, ...rest) {
                            this.__abyssSeaartUrl = String(url || '');
                            return originalOpen.call(this, method, url, ...rest);
                        };
                        proto.send = function(...args) {
                            try {
                                if (wanted(this.__abyssSeaartUrl)) {
                                    this.addEventListener('load', function() {
                                        try { addCards(JSON.parse(this.responseText || 'null')); } catch (_) {}
                                    }, {once:true});
                                }
                            } catch (_) {}
                            return originalSend.apply(this, args);
                        };
                    }
                }
                window.__abyssSeaartCatalogCards = [];
                return true;
            } catch (_) {
                return false;
            }
        """
        try:
            return bool(self.driver.execute_script(script))
        except Exception:
            return False

    def _captured_catalog_cards(self):
        if self.driver is None:
            return []
        try:
            values = self.driver.execute_script(
                "return Array.isArray(window.__abyssSeaartCatalogCards) ? "
                "window.__abyssSeaartCatalogCards.slice() : [];"
            ) or []
        except Exception:
            return []
        out = []
        seen = set()
        for card in values:
            if not isinstance(card, dict):
                continue
            model_id = str(card.get("id") or card.get("model_id") or card.get("model_no") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            card = dict(card)
            card["id"] = model_id
            card["model_id"] = model_id
            out.append(card)
        return out

    def _captured_catalog_ids(self):
        # Compatibility helper for older call sites/debugging.
        return [str(card.get("id") or "") for card in self._captured_catalog_cards() if card.get("id")]

    def _catalog_grid_model_ids(self):
        """Collect model IDs only from SeaArt's filtered waterfall result grid."""
        if self.driver is None:
            return []
        script = r"""
            const grids = [...document.querySelectorAll('.hy-waterfall-container')]
                .filter(el => el && el.offsetWidth > 0 && el.offsetHeight > 0);
            if (!grids.length) return [];
            // The active catalog grid is the visible waterfall with the most
            // model-card links. Featured/recommendation rails live outside it.
            grids.sort((a, b) =>
                b.querySelectorAll('a.sku-card-box[href*="/detail/"]').length -
                a.querySelectorAll('a.sku-card-box[href*="/detail/"]').length
            );
            const out = [], seen = new Set();
            for (const link of grids[0].querySelectorAll('a.sku-card-box[href*="/detail/"]')) {
                const href = link.getAttribute('href') || link.href || '';
                const match = href.match(/\/(?:models|model)\/detail\/([A-Za-z0-9_-]{8,})/);
                if (match && !seen.has(match[1])) {
                    seen.add(match[1]);
                    out.push(match[1]);
                }
            }
            return out;
        """
        try:
            return [str(value) for value in (self.driver.execute_script(script) or []) if str(value).strip()]
        except Exception:
            return []

    def _scroll_catalog_surface(self):
        """Advance SeaArt's virtualized model grid far enough to request the next page.

        SeaArt currently lazy-loads the Models catalog in ~24-card pages. Jumping
        straight to the absolute bottom can skip the frontend's intersection
        sentinel, so move incrementally past the last visible model card and emit
        scroll events on the actual scroll container.
        """
        if self.driver is None:
            return {}
        script = r"""
            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const grids = [...document.querySelectorAll('.hy-waterfall-container')]
                .filter(el => visible(el));
            grids.sort((a, b) =>
                b.querySelectorAll('a.sku-card-box[href*="/detail/"]').length -
                a.querySelectorAll('a.sku-card-box[href*="/detail/"]').length
            );
            const grid = grids.length ? grids[0] : null;
            const links = [...(grid ? grid.querySelectorAll('a.sku-card-box[href]') : [])].filter(el =>
                visible(el) && /\/(?:models|model)\/detail\/[A-Za-z0-9_-]{8,}/.test(el.getAttribute('href') || el.href || '')
            );
            const last = links.length ? links[links.length - 1] : null;

            // First let the browser reveal the last rendered card. For nested
            // virtual scrollers, scrollIntoView moves the correct ancestor even
            // when window itself is not the scrolling surface.
            try {
                if (last) last.scrollIntoView({block: 'end', inline: 'nearest', behavior: 'instant'});
            } catch (_) {}

            const candidates = [];
            for (const el of document.querySelectorAll('body *')) {
                if (!visible(el)) continue;
                const style = getComputedStyle(el);
                const overflow = String(style.overflowY || '');
                const room = (el.scrollHeight || 0) - (el.clientHeight || 0);
                if (/(auto|scroll)/.test(overflow) && room > 160) {
                    candidates.push({el, room, area: (el.clientWidth || 0) * (el.clientHeight || 0)});
                }
            }

            let scroller = null;
            if (last) {
                let node = last.parentElement;
                while (node && node !== document.body && node !== document.documentElement) {
                    const style = getComputedStyle(node);
                    if (/(auto|scroll)/.test(String(style.overflowY || '')) &&
                        node.scrollHeight > node.clientHeight + 160) {
                        scroller = node;
                        break;
                    }
                    node = node.parentElement;
                }
            }
            if (!scroller && candidates.length) {
                candidates.sort((a, b) => (b.room - a.room) || (b.area - a.area));
                scroller = candidates[0].el;
            }
            if (!scroller) scroller = document.scrollingElement || document.documentElement;

            const root = scroller === document.scrollingElement ||
                         scroller === document.documentElement ||
                         scroller === document.body;
            const beforeTop = root ? (window.scrollY || scroller.scrollTop || 0) : (scroller.scrollTop || 0);
            const beforeHeight = scroller.scrollHeight || document.documentElement.scrollHeight || 0;
            const client = root ? (window.innerHeight || scroller.clientHeight || 800) : (scroller.clientHeight || 800);
            const maxTop = Math.max(0, beforeHeight - client);

            // Advance about one viewport at a time. This reliably crosses the
            // infinite-scroll sentinel instead of teleporting past it.
            // If the virtual list is already parked at the bottom but its
            // observer did not fire, nudge upward. The next pass crosses the
            // sentinel again instead of issuing identical no-op scrolls.
            const atBottom = beforeTop >= Math.max(0, maxTop - 4);
            const target = atBottom
                ? Math.max(0, maxTop - Math.max(client * 0.45, 420))
                : Math.min(maxTop, beforeTop + Math.max(client * 0.92, 650));
            if (root) {
                window.scrollTo(0, target);
                try { window.dispatchEvent(new Event('scroll')); } catch (_) {}
            } else {
                scroller.scrollTop = target;
                try { scroller.dispatchEvent(new Event('scroll', {bubbles: true})); } catch (_) {}
            }

            const afterTop = root ? (window.scrollY || scroller.scrollTop || 0) : (scroller.scrollTop || 0);
            return {
                root,
                beforeTop,
                afterTop,
                beforeHeight,
                client,
                maxTop,
                renderedLinks: links.length,
                scrollCandidates: candidates.length
            };
        """
        try:
            return self.driver.execute_script(script) or {}
        except Exception:
            return {}

    def _try_sort(self, sort):
        """Switch SeaArt's Models-page sort control through its exposed menu.

        Current SeaArt builds expose the *closed* sort trigger as an
        ``aria-haspopup="menu"`` control whose visible text is the current sort
        (for example ``Recommended``).  Prefer that stable semantic control over
        trying to guess which icon button opens the menu.  The wider toolbar
        probing remains as a fallback for older layouts.
        """
        if self.driver is None:
            return False

        requested = str(sort or "newest").strip().lower()
        wanted = {
            "newest": "New",
            "new": "New",
            "hot": "Hot",
            "recommended": "Recommended",
        }.get(requested, "New")

        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.action_chains import ActionChains
        except Exception:
            By = None
            ActionChains = None

        def _text(el):
            try:
                return " ".join(str(el.text or "").split())
            except Exception:
                return ""

        def _native_click(el):
            """Use a real WebDriver click first; JS click is only a fallback."""
            try:
                el.click()
                return True
            except Exception:
                pass
            if ActionChains is not None:
                try:
                    ActionChains(self.driver).move_to_element(el).click().perform()
                    return True
                except Exception:
                    pass
            try:
                self.driver.execute_script(r"""
                    const el = arguments[0];
                    el.scrollIntoView({block:'center', inline:'center'});
                    const opts = {bubbles:true, cancelable:true, view:window};
                    el.dispatchEvent(new MouseEvent('mousedown', opts));
                    el.dispatchEvent(new MouseEvent('mouseup', opts));
                    el.dispatchEvent(new MouseEvent('click', opts));
                """, el)
                return True
            except Exception:
                return False

        def _menu_target(label):
            """Find the requested entry inside an actual open menu/listbox."""
            if By is None:
                return None
            xpaths = [
                f"//*[@role='menu']//*[@role='menuitem' and normalize-space(.)={json.dumps(label)}]",
                f"//*[@role='menu']//*[normalize-space(.)={json.dumps(label)}]",
                f"//*[@role='listbox']//*[@role='option' and normalize-space(.)={json.dumps(label)}]",
                f"//*[@role='listbox']//*[normalize-space(.)={json.dumps(label)}]",
                f"//*[@role='menuitem' and normalize-space(.)={json.dumps(label)}]",
                f"//*[@role='option' and normalize-space(.)={json.dumps(label)}]",
            ]
            for xpath in xpaths:
                try:
                    for el in self.driver.find_elements(By.XPATH, xpath):
                        if el.is_displayed():
                            return el
                except Exception:
                    continue
            return None

        # Preferred path: SeaArt currently exposes the closed trigger as, e.g.,
        # ``Recommended`` + aria-haspopup="menu".  The diagnostic from the live
        # site reports this as ``Recommended|menu``.
        if By is not None:
            triggers = []
            try:
                triggers.extend(self.driver.find_elements(By.CSS_SELECTOR, '[aria-haspopup="menu"]'))
            except Exception:
                pass
            try:
                triggers.extend(self.driver.find_elements(By.CSS_SELECTOR, '[role="button"][aria-haspopup]'))
            except Exception:
                pass

            seen_ids = set()
            for trigger in triggers:
                try:
                    if not trigger.is_displayed():
                        continue
                    key = getattr(trigger, "id", None) or id(trigger)
                    if key in seen_ids:
                        continue
                    seen_ids.add(key)
                    current = _text(trigger)
                    if current not in {"Recommended", "Hot", "New", "Newest"}:
                        continue
                    if current in {wanted, "Newest" if wanted == "New" else wanted}:
                        return True
                    if not _native_click(trigger):
                        continue
                    deadline = time.time() + 2.5
                    while time.time() < deadline:
                        target = _menu_target(wanted)
                        if target is not None:
                            clickable = target
                            try:
                                # Prefer the menu item/option ancestor when the
                                # text itself is a nested span.
                                clickable = target.find_element(By.XPATH, "ancestor-or-self::*[@role='menuitem' or @role='option'][1]")
                            except Exception:
                                pass
                            if _native_click(clickable):
                                time.sleep(1.4)
                                return True
                        time.sleep(.1)
                except Exception:
                    continue
            # Do not stop here; older layouts may have no semantic trigger.

        # Fallback: when the popup is already open, require the distinctive trio
        # before selecting anything. This prevents a model-card Hot badge from
        # being treated as the Hot sort option.
        menu_script = r"""
            const wanted = arguments[0];
            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const exact = label => [...document.querySelectorAll('body *')]
                .filter(el => visible(el) && (el.textContent || '').trim() === label);
            const rec = exact('Recommended'), hot = exact('Hot'), fresh = exact('New');
            if (!rec.length || !hot.length || !fresh.length) return false;
            for (const r of rec) {
                const rr = r.getBoundingClientRect();
                const near = el => {
                    const x = el.getBoundingClientRect();
                    return Math.abs(x.left - rr.left) < 260 && Math.abs(x.top - rr.top) < 260;
                };
                const h = hot.find(near), n = fresh.find(near);
                if (!h || !n) continue;
                const pool = wanted === 'Recommended' ? rec : (wanted === 'Hot' ? hot : fresh);
                const target = pool.find(near);
                if (!target) continue;
                const clickable = target.closest('[role="menuitem"],[role="option"],button,[role="button"]') || target;
                const opts = {bubbles:true, cancelable:true, view:window};
                clickable.dispatchEvent(new MouseEvent('mousedown', opts));
                clickable.dispatchEvent(new MouseEvent('mouseup', opts));
                clickable.dispatchEvent(new MouseEvent('click', opts));
                return true;
            }
            return false;
        """
        try:
            if bool(self.driver.execute_script(menu_script, wanted)):
                time.sleep(1.4)
                return True
        except Exception:
            pass

        # Older-layout fallback: probe safe controls on the same toolbar row as
        # Base Model and accept a candidate only if opening it reveals the real
        # Recommended/Hot/New menu.
        candidate_script = r"""
            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const all = [...document.querySelectorAll('body *')];
            const baseText = all.find(el => visible(el) && (el.textContent || '').trim() === 'Base Model');
            if (!baseText) return [];
            const baseControl = baseText.closest('button,[role="button"],[aria-haspopup],[data-state]') || baseText;
            const br = baseControl.getBoundingClientRect();
            const by = br.top + br.height / 2;
            const raw = all.filter(el => {
                if (!visible(el) || el === baseControl || el.contains(baseControl) || baseControl.contains(el)) return false;
                if (el.closest('a[href]')) return false;
                const r = el.getBoundingClientRect(), cy = r.top + r.height / 2;
                if (Math.abs(cy - by) > 48) return false;
                const role = el.getAttribute('role') || '';
                const interactive = el.tagName === 'BUTTON' || role === 'button' ||
                    el.hasAttribute('aria-haspopup') || el.hasAttribute('data-state') ||
                    (el.querySelector('svg') && getComputedStyle(el).cursor === 'pointer');
                if (!interactive) return false;
                const text = (el.innerText || '').trim();
                if (/^(Create|Upload Model|Base Model)$/i.test(text)) return false;
                return true;
            });
            const unique = raw.filter(el => !raw.some(other => other !== el && other.contains(el)));
            return unique.map(el => {
                const r = el.getBoundingClientRect();
                const text = (el.innerText || '').trim();
                const semanticSort = /^(Recommended|Hot|New|Newest)$/i.test(text) && el.hasAttribute('aria-haspopup');
                const iconOnly = !text && !!el.querySelector('svg');
                const popup = el.hasAttribute('aria-haspopup') || el.hasAttribute('data-state');
                return {el, semanticSort, iconOnly, popup, x:r.left, area:r.width*r.height};
            }).sort((a,b) =>
                (Number(b.semanticSort)-Number(a.semanticSort)) ||
                (Number(b.popup)-Number(a.popup)) ||
                (Number(b.iconOnly)-Number(a.iconOnly)) ||
                (b.x-a.x) ||
                (a.area-b.area)
            ).slice(0, 18).map(x => x.el);
        """
        try:
            candidates = self.driver.execute_script(candidate_script) or []
        except Exception:
            candidates = []

        for target in candidates:
            try:
                current = _text(target)
                if current in {wanted, "Newest" if wanted == "New" else wanted}:
                    return True
                if not _native_click(target):
                    continue
                deadline = time.time() + 1.8
                while time.time() < deadline:
                    menu_target = _menu_target(wanted)
                    if menu_target is not None and _native_click(menu_target):
                        time.sleep(1.4)
                        return True
                    try:
                        if bool(self.driver.execute_script(menu_script, wanted)):
                            time.sleep(1.4)
                            return True
                    except Exception:
                        pass
                    time.sleep(.1)
                try:
                    from selenium.webdriver.common.keys import Keys
                    self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
                    time.sleep(.12)
                except Exception:
                    pass
            except Exception:
                continue
        return False

    def _sort_control_debug(self):
        """Return a compact, non-sensitive description of the Models toolbar."""
        if self.driver is None:
            return ""
        script = r"""
            const visible = el => {
                if (!el) return false;
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
            };
            const all = [...document.querySelectorAll('body *')];
            const base = all.find(el => visible(el) && (el.textContent || '').trim() === 'Base Model');
            if (!base) return ['Base Model text not found'];
            const br = base.getBoundingClientRect(), by = br.top + br.height/2;
            const controls = all.filter(el => {
                if (!visible(el) || el.closest('a[href]')) return false;
                const r=el.getBoundingClientRect();
                if (Math.abs((r.top+r.height/2)-by) > 55) return false;
                const role=el.getAttribute('role') || '';
                return el.tagName === 'BUTTON' || role === 'button' || el.hasAttribute('aria-haspopup') ||
                    el.hasAttribute('data-state') || (el.querySelector('svg') && getComputedStyle(el).cursor === 'pointer');
            }).filter(el => !controlsContainsParent(el));

            function controlsContainsParent(el) {
                // Keep outer clickable wrappers rather than nested svg/span nodes.
                return all.some(other => other !== el && visible(other) && other.contains(el) &&
                    (other.tagName === 'BUTTON' || (other.getAttribute('role') || '') === 'button'));
            }

            return controls.map(el => {
                const r=el.getBoundingClientRect();
                const text=(el.innerText || '').trim().replace(/\s+/g,' ').slice(0,45);
                const aria=el.getAttribute('aria-label') || '';
                const title=el.getAttribute('title') || '';
                const popup=el.getAttribute('aria-haspopup') || '';
                const state=el.getAttribute('data-state') || '';
                const label=[text,aria,title,popup,state].filter(Boolean).join('|') || '(icon button)';
                return {x:Math.round(r.left), w:Math.round(r.width), label};
            }).sort((a,b)=>b.x-a.x).slice(0,12)
              .map(x => `${x.label}@x${x.x}/w${x.w}`);
        """
        try:
            values = self.driver.execute_script(script) or []
            return ", ".join(str(x)[:120] for x in values)
        except Exception:
            return ""

    def _try_newest(self):
        # Backward-compatible helper used by explicit keyword search.
        return self._try_sort("newest")

    def _try_base_model(self, base_model, sort="newest"):
        """Apply SeaArt's Models-page sort and Base Model in one open popover.

        SeaArt signs the underlying catalog request inside its frontend, so the
        persistent browser path must let the site perform this interaction rather
        than replaying or inventing X-Sign values. SeaArt does not reliably retain
        a selection when this popover is closed and reopened, so both controls must
        be selected before the menu is dismissed.
        """
        if self.driver is None:
            return False
        try:
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.webdriver.common.by import By
        except Exception:
            return False

        value = str(base_model or "").strip()
        aliases = {
            "krea image": ["Krea 2", "Krea Image"],
            "minimax h3 open": ["MiniMax H3", "Minimax H3 Open", "Minimax H3"],
        }.get(value.casefold(), [value])
        sort_label = {
            "newest": "New",
            "new": "New",
            "hot": "Hot",
            "recommended": "Recommended",
        }.get(str(sort or "newest").strip().casefold(), "New")

        def native_click(element):
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center',inline:'center'});", element
                )
                ActionChains(self.driver).move_to_element(element).pause(.12).click().perform()
                return True
            except Exception:
                try:
                    element.click()
                    return True
                except Exception:
                    return False

        before_ids = tuple(self._catalog_grid_model_ids())

        # "Base Model" in the horizontal navigation is a category tab, not the
        # filter menu.  Open the right-side Filter control and constrain all
        # option lookup to its visible popover.
        try:
            controls = self.driver.find_elements(
                By.CSS_SELECTOR,
                ".right-filter-box .filter-box, .select-filter-box .filter-box",
            )
            control = next((el for el in controls if el.is_displayed()), None)
            if control is None or not native_click(control):
                return False
        except Exception:
            return False

        def visible_popover():
            try:
                return next(
                    (el for el in self.driver.find_elements(By.CSS_SELECTOR, ".hy-filter-popover")
                     if el.is_displayed()),
                    None,
                )
            except Exception:
                return None

        popover = None
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and popover is None:
            popover = visible_popover()
            if popover is None:
                time.sleep(.1)
        if popover is None:
            return False

        def activate_exact(labels):
            for label in labels:
                if not label:
                    continue
                current_popover = visible_popover()
                if current_popover is None:
                    return False
                try:
                    options = current_popover.find_elements(
                        By.XPATH, f".//*[normalize-space()={json.dumps(label)}]"
                    )
                    option = next((el for el in options if el.is_displayed()), None)
                    if option is None:
                        continue
                    option_class = str(option.get_attribute("class") or "")
                    if "act-item" not in option_class.split() and not native_click(option):
                        continue
                    time.sleep(.25)
                    return True
                except Exception:
                    continue
            return False

        # Keep the popover open while setting both values. Selecting New in one
        # menu visit and Krea 2 in a later visit can silently restore Recommended.
        if not activate_exact([sort_label]):
            return False

        selected = False
        selected = activate_exact(aliases)
        if not selected:
            return False

        # The waterfall is replaced column-by-column.  Its first changed set can
        # still contain cards from the previous architecture, so do not accept a
        # transition frame.  Require the complete ID set to remain unchanged for
        # four consecutive observations before catalog collection begins.
        deadline = time.monotonic() + 7.0
        last_ids = ()
        stable_observations = 0
        while time.monotonic() < deadline:
            current_ids = tuple(self._catalog_grid_model_ids())
            if current_ids and current_ids != before_ids:
                if current_ids == last_ids:
                    stable_observations += 1
                else:
                    last_ids = current_ids
                    stable_observations = 1
                if stable_observations >= 4:
                    return True
            else:
                stable_observations = 0
            time.sleep(.3)
        return bool(last_ids or self._catalog_grid_model_ids())

    @staticmethod
    def _bidi_headers_to_dict(headers):
        """Normalize WebDriver BiDi request headers to ordinary strings.

        Firefox/Selenium versions have exposed this as either the W3C list of
        ``{name, value}`` objects or a mapping-like object.  Accept both so a
        harmless binding-shape change cannot disable SeaArt scanning.
        """
        result = {}
        if isinstance(headers, dict):
            iterable = ({"name": key, "value": value} for key, value in headers.items())
        else:
            iterable = headers or []
        for item in iterable:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = item.get("value")
            if isinstance(value, dict):
                value = value.get("value")
            if not name or value in (None, ""):
                continue
            result[name] = str(value)
        return result

    def _capture_catalog_request_headers(self):
        """Capture SeaArt's own signed catalog headers through WebDriver BiDi.

        Listen broadly and filter in Python rather than asking Firefox to apply
        URL-pattern matching in the intercept.  This is intentionally a little
        noisier for a few seconds, but avoids browser-specific URLPattern quirks.
        Every intercepted request is immediately released.
        """
        if self.driver is None:
            return {}

        captured = {}
        network = None
        callback_id = None
        capture_error = ""
        matched_catalog_request = False
        try:
            network = self.driver.network

            def on_request(request):
                nonlocal matched_catalog_request, capture_error
                try:
                    url = str(getattr(request, "url", "") or "")
                    if (
                        "/api/v1/square/v3/model/list" in url
                        or "/api/v1/square/v3/model/recommend" in url
                    ):
                        matched_catalog_request = True
                        headers = self._bidi_headers_to_dict(getattr(request, "headers", None))
                        if headers and not captured:
                            captured.update(headers)
                except Exception as exc:
                    capture_error = str(exc or type(exc).__name__)
                finally:
                    # add_request_handler() intercepts beforeRequestSent, so every
                    # request must be released whether it is interesting or not.
                    try:
                        request.continue_request()
                    except Exception:
                        pass

            # Do not pass url_patterns here. Firefox BiDi builds have differed in
            # URLPattern handling; broad interception plus a cheap URL test above
            # is much more reliable and lasts only for this short page load.
            callback_id = network.add_request_handler("before_request", on_request)
            self.driver.get("https://www.seaart.ai/model")

            deadline = time.time() + 4.5
            while not captured and time.time() < deadline:
                time.sleep(.10)

            # SeaArt can hydrate the first catalog from cached app data.  Scrolling
            # the catalog forces the next page and therefore a fresh signed list
            # request that BiDi can observe.
            if not captured:
                try:
                    self.driver.execute_script(
                        "window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));"
                    )
                except Exception:
                    pass
                deadline = time.time() + 4.0
                while not captured and time.time() < deadline:
                    time.sleep(.10)
        except Exception as exc:
            capture_error = str(exc or type(exc).__name__)
        finally:
            if network is not None and callback_id is not None:
                try:
                    network.remove_request_handler("before_request", callback_id)
                except Exception:
                    pass

        # Keep a diagnostic for catalog_models(); never expose signed values.
        if captured and not any(str(k).casefold() == "x-sign" for k in captured):
            capture_error = "catalog request was observed but X-Sign was not exposed by WebDriver BiDi"
            captured = {}
        elif not captured and not capture_error:
            capture_error = (
                "catalog request was observed but no headers were exposed"
                if matched_catalog_request
                else "no SeaArt catalog request was observed by WebDriver BiDi"
            )
        self._last_catalog_capture_error = capture_error
        return captured

    def _signed_browser_post(self, url, payload, signed_headers):
        """Replay a catalog request inside SeaArt using its captured signed headers."""
        if self.driver is None:
            raise RuntimeError("SeaArt live browser is not open")

        allowed = {}
        for name, value in (signed_headers or {}).items():
            lname = str(name or "").casefold()
            if lname.startswith("x-") or lname in {"accept", "accept-language", "content-type"}:
                allowed[str(name)] = str(value)
        # SeaArt treats request IDs as per-request values.  Keep the signed
        # identity headers but refresh this non-signing correlation id.
        for key in list(allowed):
            if key.casefold() == "x-request-id":
                allowed[key] = str(uuid.uuid4())
        if not any(k.casefold() == "content-type" for k in allowed):
            allowed["Content-Type"] = "application/json"
        if not any(k.casefold() == "accept" for k in allowed):
            allowed["Accept"] = "application/json, text/plain, */*"

        script = r"""
            const done = arguments[arguments.length - 1];
            const url = arguments[0], payload = arguments[1], headers = arguments[2];
            window.fetch.call(window, url, {
                method: 'POST',
                credentials: 'include',
                headers,
                body: JSON.stringify(payload)
            }).then(async r => {
                const text = await r.text();
                done({ok:r.ok, status:r.status, text});
            }).catch(e => done({ok:false, status:0, text:String(e)}));
        """
        result = self.driver.execute_async_script(script, str(url), payload, allowed)
        if not isinstance(result, dict):
            raise RuntimeError("SeaArt signed browser request returned no response")
        status = int(result.get("status") or 0)
        text = str(result.get("text") or "")
        if status >= 400 or not result.get("ok"):
            raise RuntimeError(f"SeaArt signed catalog HTTP {status}: {text[:240]}")
        try:
            data = json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"SeaArt signed catalog returned invalid JSON: {text[:180]}") from exc
        api_status = data.get("status") if isinstance(data, dict) else None
        if isinstance(api_status, dict) and api_status.get("code") not in (None, 0, 10000, "10000"):
            raise RuntimeError(api_status.get("msg") or f"SeaArt API code {api_status.get('code')}")
        return data

    @staticmethod
    def _catalog_response_cards(payload):
        """Extract model cards defensively from SeaArt list/recommend wrappers."""
        data = payload.get("data") if isinstance(payload, dict) else payload
        out, seen = [], set()

        def walk(obj):
            if isinstance(obj, dict):
                yield obj
                for value in obj.values():
                    yield from walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    yield from walk(value)

        for node in walk(data):
            model_id = node.get("id") or node.get("model_id") or node.get("model_no")
            name = node.get("name") or node.get("title")
            if not model_id or not name:
                continue
            if not any(k in node for k in (
                "model_ver_no", "model_type", "type", "base_model",
                "base_model_title", "author", "cover", "cover_v2",
            )):
                continue
            key = str(model_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(node)
        return out

    @staticmethod
    def _catalog_response_offset(payload):
        data = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(data, dict):
            for key in ("offset", "next_offset", "nextOffset"):
                value = data.get(key)
                if value not in (None, ""):
                    return str(value)
        if isinstance(payload, dict):
            for key in ("offset", "next_offset", "nextOffset"):
                value = payload.get(key)
                if value not in (None, ""):
                    return str(value)
        return ""

    @staticmethod
    def _catalog_card_matches_base(card, base_model):
        wanted = str(base_model or "").strip().casefold()
        aliases = {
            "krea image": ("krea image", "krea 2", "krea2"),
            "minimax h3 open": ("minimax h3 open", "minimax h3", "minimax-h3"),
        }.get(wanted, (wanted,))
        values = []

        def walk(obj):
            if isinstance(obj, dict):
                yield obj
                for value in obj.values():
                    yield from walk(value)
            elif isinstance(obj, list):
                for value in obj:
                    yield from walk(value)

        for node in walk(card):
            for key in ("base_model", "base_model_title", "base_model_name", "baseModel"):
                value = node.get(key)
                if value not in (None, ""):
                    values.append(str(value).casefold())
        text = " ".join(values)
        return bool(text and any(alias and alias in text for alias in aliases))

    def _catalog_models_via_signed_api(self, base_model, max_results, sort, signed_headers):
        requested = str(sort or "newest").strip().lower()
        recommended = requested == "recommended"
        endpoint = (
            "https://www.seaart.ai/api/v1/square/v3/model/recommend"
            if recommended
            else "https://www.seaart.ai/api/v1/square/v3/model/list"
        )
        page_size = min(24, max_results)
        collected, known = [], set()
        page, offset = 1, ""

        while len(collected) < max_results and page <= 60 and not _STOP.is_set():
            requested_count = min(page_size, max_results - len(collected))
            if recommended:
                payload = {
                    "offset": offset,
                    "page": page,
                    "page_size": requested_count,
                    "canary_for_other": "sku",
                }
            else:
                payload = {
                    "scene": "scene_ai_search_list_order_by_hot",
                    "offset": offset,
                    "page": page,
                    "page_size": requested_count,
                    "base_models": [base_model],
                    "model_types": [],
                    "model_category": "all",
                    "order_by": "hot" if requested == "hot" else "scope_b",
                    "canary_for_other": "sku",
                    "ss": 54,
                }

            response = self._signed_browser_post(endpoint, payload, signed_headers)
            items = self._catalog_response_cards(response)
            if not items:
                break

            raw_added = 0
            for item in items:
                key = str(item.get("id") or item.get("model_id") or item.get("model_no") or "").strip()
                if not key or key in known:
                    continue
                known.add(key)
                raw_added += 1
                if recommended and not self._catalog_card_matches_base(item, base_model):
                    continue
                collected.append(item)
                if len(collected) >= max_results:
                    break

            next_offset = self._catalog_response_offset(response)
            if not raw_added:
                break
            offset = next_offset
            if len(items) < requested_count and not next_offset:
                break
            page += 1

        return collected[:max_results]

    def model_catalog_page_ready(self):
        """Verify that the saved browser session can open SeaArt's Models page.

        Connection health must not depend on a particular sort/filter selector. A
        SeaArt UI copy/layout change should fail an individual scan with a useful
        scanner error, not mark the entire saved session as disconnected.
        """
        if self.driver is None:
            return False
        try:
            self.driver.get("https://www.seaart.ai/model")
            time.sleep(2.0)
            current = str(getattr(self.driver, "current_url", "") or "")
            if not current.startswith("https://www.seaart.ai"):
                return False
            if self._model_ids_from_dom():
                return True
            body_text = str(self.driver.execute_script("return document.body ? document.body.innerText : '';") or "")
            return "Base Model" in body_text or "Upload Model" in body_text
        except Exception:
            return False

    def catalog_models(self, base_model, max_results=100, sort="newest"):
        """Browse SeaArt's real Models catalog with a structured Base Model filter.

        SeaArt virtualizes the Models page, so preserve complete cards from its own
        successful ``model/list`` responses while scrolling.  The current API schema
        uses ``data.items[*].id`` as the public model id; keeping the whole card also
        preserves architecture/type/date metadata for retention and diagnostics.
        """
        if self.driver is None:
            raise RuntimeError("SeaArt live browser is not open")
        try:
            limit = max(1, int(max_results))
        except Exception:
            limit = 100

        self.driver.get("https://www.seaart.ai/model")
        time.sleep(2.0)
        if not self._try_base_model(base_model, sort=sort):
            raise RuntimeError(
                f"SeaArt combined sort/base-model filter was not found for {sort!r} / {base_model!r}"
            )
        time.sleep(1.0)

        found = []
        seen = set()

        def collect_current():
            cards = [
                {"id": model_id, "model_id": model_id}
                for model_id in self._catalog_grid_model_ids()
            ]

            added = 0
            for card in cards:
                if not isinstance(card, dict):
                    continue
                model_id = str(card.get("id") or card.get("model_id") or card.get("model_no") or "").strip()
                if not model_id or model_id in seen:
                    continue
                seen.add(model_id)
                normalized = dict(card)
                normalized["id"] = model_id
                normalized["model_id"] = model_id
                found.append(normalized)
                added += 1
                if len(found) >= limit:
                    break
            return added

        collect_current()
        stagnant = 0
        for _ in range(60):
            if _STOP.is_set() or len(found) >= limit:
                break
            before = len(found)
            self._scroll_catalog_surface()
            time.sleep(.55)
            collect_current()
            if len(found) == before:
                stagnant += 1
            else:
                stagnant = 0
            if stagnant >= 10:
                break

        label = {"newest": "New", "new": "New", "hot": "Hot", "recommended": "Recommended"}.get(
            str(sort or "newest").strip().lower(), "New"
        )
        print(
            f"SeaArt live catalog: {base_model} / {label} -> {len(found[:limit])} candidate(s) "
            f"(limit {limit}, scoped filtered grid)"
        )
        return found[:limit]

    def search_models(self, query, max_results=100):
        """Use SeaArt's real search page and collect model IDs from rendered cards.

        No signed request is replayed or forged: SeaArt's own frontend performs all
        discovery requests and pagination while AbyssBeacon scrolls the page.
        """
        if self.driver is None:
            raise RuntimeError("SeaArt live browser is not open")
        from urllib.parse import quote as _quote

        try:
            limit = max(1, int(max_results))
        except Exception:
            limit = 100
        url = "https://www.seaart.ai/search/model/" + _quote(str(query or "").strip())
        self.driver.get(url)
        time.sleep(2.0)
        self._try_newest()

        found = []
        seen = set()
        stagnant = 0
        last_height = 0
        for _ in range(60):
            if _STOP.is_set():
                break
            before_count = len(found)
            for model_id in self._model_ids_from_dom():
                if model_id not in seen:
                    seen.add(model_id)
                    found.append({"id": model_id, "model_id": model_id})
                    if len(found) >= limit:
                        return found[:limit]

            try:
                height = int(self.driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);"))
                self.driver.execute_script("window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));")
            except Exception:
                break
            time.sleep(1.15)
            if height <= last_height and len(found) == before_count:
                stagnant += 1
            else:
                stagnant = 0
            last_height = height
            if stagnant >= 4:
                break
        return found[:limit]

    def post_json(self, url, payload, referer=None):
        """POST from the authenticated SeaArt page context.

        SeaArt's detail/account endpoints do not use the volatile X-Sign catalog
        signature, but current browser requests do carry stable browser-context
        headers derived from the isolated profile. Recreate those headers from the
        live page's own cookies so detail enrichment matches SeaArt's frontend more
        closely without persisting or printing any token values.
        """
        if self.driver is None:
            raise RuntimeError("SeaArt live browser is not open")
        if not str(getattr(self.driver, "current_url", "") or "").startswith("https://www.seaart.ai"):
            self.driver.get(referer or "https://www.seaart.ai/model")
            time.sleep(1.0)
        script = r"""
            const done = arguments[arguments.length - 1];
            const url = arguments[0];
            const payload = arguments[1];
            const requestedReferer = arguments[2] || '';
            const readCookie = (name) => {
                try {
                    const prefix = name + '=';
                    for (const part of String(document.cookie || '').split(';')) {
                        const value = part.trim();
                        if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
                    }
                } catch (_) {}
                return '';
            };
            const requestId = (() => {
                try { if (crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (_) {}
                return String(Date.now()) + '-' + Math.random().toString(16).slice(2);
            })();
            const headers = {
                'Accept': 'application/json, text/plain, */*',
                'Content-Type': 'application/json',
                'X-Request-Id': requestId,
                'X-Platform': 'web',
                'X-Project-Id': 'seaart',
                'X-App-Id': 'web_global_seaart',
                'X-Timezone': (() => {
                    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }
                    catch (_) { return 'UTC'; }
                })()
            };
            const optional = {
                'X-Device-Id': readCookie('deviceId'),
                'X-Gray-Tag': readCookie('grayTag'),
                'X-Browser-Id': readCookie('browserId'),
                'X-Page-Id': readCookie('pageId')
            };
            for (const [name, value] of Object.entries(optional)) {
                if (value) headers[name] = value;
            }
            let restoreUrl = '';
            try {
                if (requestedReferer) {
                    const target = new URL(requestedReferer, location.href);
                    if (target.origin === location.origin && target.href !== location.href) {
                        restoreUrl = location.href;
                        history.replaceState(history.state, '', target.pathname + target.search + target.hash);
                    }
                }
            } catch (_) {}
            window.fetch.call(window, url, {
                method: 'POST',
                credentials: 'include',
                headers,
                body: JSON.stringify(payload)
            }).then(async r => {
                const text = await r.text();
                try {
                    if (restoreUrl) {
                        const back = new URL(restoreUrl);
                        history.replaceState(history.state, '', back.pathname + back.search + back.hash);
                    }
                } catch (_) {}
                done({ok: r.ok, status: r.status, text});
            }).catch(e => {
                try {
                    if (restoreUrl) {
                        const back = new URL(restoreUrl);
                        history.replaceState(history.state, '', back.pathname + back.search + back.hash);
                    }
                } catch (_) {}
                done({ok:false, status:0, text:String(e)});
            });
        """
        result = self.driver.execute_async_script(script, str(url), payload, str(referer or ""))
        if not isinstance(result, dict):
            raise RuntimeError("SeaArt browser request returned no response")
        status = int(result.get("status") or 0)
        text = str(result.get("text") or "")
        if status >= 400 or not result.get("ok"):
            raise RuntimeError(f"SeaArt HTTP {status}: {text[:300]}")
        try:
            data = json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"SeaArt returned invalid JSON: {text[:200]}") from exc
        api_status = data.get("status") if isinstance(data, dict) else None
        if isinstance(api_status, dict) and api_status.get("code") not in (None, 0, 10000, "10000"):
            raise RuntimeError(api_status.get("msg") or f"SeaArt API code {api_status.get('code')}")
        return data

    def post_json_many(self, url, payloads, referers=None, max_concurrency=6):
        """POST several independent API payloads through one live page call.

        SeaArt detail enrichment does not need DOM navigation. Running a small
        worker pool inside the already-open page preserves its cookies/browser
        identity while avoiding one Selenium round trip per model.
        """
        if self.driver is None:
            raise RuntimeError("SeaArt live browser is not open")
        payloads = list(payloads or [])
        if not payloads:
            return []
        referers = list(referers or [])
        try:
            concurrency = max(1, min(8, int(max_concurrency or 6)))
        except Exception:
            concurrency = 6
        if not str(getattr(self.driver, "current_url", "") or "").startswith("https://www.seaart.ai"):
            self.driver.get("https://www.seaart.ai/model")
            time.sleep(1.0)

        script = r"""
            const done = arguments[arguments.length - 1];
            const url = arguments[0], payloads = arguments[1];
            const referers = arguments[2] || [], concurrency = arguments[3] || 6;
            const readCookie = (name) => {
                try {
                    const prefix = name + '=';
                    for (const part of String(document.cookie || '').split(';')) {
                        const value = part.trim();
                        if (value.startsWith(prefix)) return decodeURIComponent(value.slice(prefix.length));
                    }
                } catch (_) {}
                return '';
            };
            const requestId = () => {
                try { if (crypto && crypto.randomUUID) return crypto.randomUUID(); } catch (_) {}
                return String(Date.now()) + '-' + Math.random().toString(16).slice(2);
            };
            const makeHeaders = () => {
                const headers = {
                    'Accept': 'application/json, text/plain, */*',
                    'Content-Type': 'application/json',
                    'X-Request-Id': requestId(),
                    'X-Platform': 'web',
                    'X-Project-Id': 'seaart',
                    'X-App-Id': 'web_global_seaart',
                    'X-Timezone': (() => {
                        try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }
                        catch (_) { return 'UTC'; }
                    })()
                };
                const optional = {
                    'X-Device-Id': readCookie('deviceId'),
                    'X-Gray-Tag': readCookie('grayTag'),
                    'X-Browser-Id': readCookie('browserId'),
                    'X-Page-Id': readCookie('pageId')
                };
                for (const [name, value] of Object.entries(optional)) if (value) headers[name] = value;
                return headers;
            };
            const results = new Array(payloads.length);
            let next = 0;
            const worker = async () => {
                while (true) {
                    const index = next++;
                    if (index >= payloads.length) return;
                    try {
                        const options = {
                            method: 'POST', credentials: 'include',
                            headers: makeHeaders(), body: JSON.stringify(payloads[index])
                        };
                        const referrer = String(referers[index] || '');
                        if (referrer) options.referrer = referrer;
                        const response = await window.fetch.call(window, url, options);
                        results[index] = {
                            ok: response.ok, status: response.status,
                            text: await response.text()
                        };
                    } catch (error) {
                        results[index] = {ok:false, status:0, text:String(error)};
                    }
                }
            };
            Promise.all(Array.from({length: Math.min(concurrency, payloads.length)}, worker))
                .then(() => done(results))
                .catch(error => done([{ok:false, status:0, text:String(error)}]));
        """
        raw_results = self.driver.execute_async_script(
            script, str(url), payloads, referers, concurrency
        )
        if not isinstance(raw_results, list) or len(raw_results) != len(payloads):
            raise RuntimeError("SeaArt detail batch returned an incomplete response")

        results = []
        failure_counts = {}
        first_failure = ""
        for item in raw_results:
            if not isinstance(item, dict):
                failure_counts["invalid"] = failure_counts.get("invalid", 0) + 1
                results.append(None)
                continue
            status = int(item.get("status") or 0)
            text = str(item.get("text") or "")
            if status >= 400 or not item.get("ok"):
                label = f"HTTP {status}" if status else "network"
                failure_counts[label] = failure_counts.get(label, 0) + 1
                if not first_failure:
                    first_failure = " ".join(text.split())[:160]
                results.append(None)
                continue
            try:
                data = json.loads(text)
            except Exception:
                failure_counts["invalid JSON"] = failure_counts.get("invalid JSON", 0) + 1
                results.append(None)
                continue
            api_status = data.get("status") if isinstance(data, dict) else None
            if isinstance(api_status, dict) and api_status.get("code") not in (None, 0, 10000, "10000"):
                label = f"API {api_status.get('code')}"
                failure_counts[label] = failure_counts.get(label, 0) + 1
                if not first_failure:
                    first_failure = " ".join(str(api_status.get("msg") or "").split())[:160]
                results.append(None)
                continue
            results.append(data)
        self._last_post_json_many_diagnostic = {
            "failures": failure_counts,
            "first_failure": first_failure,
        }
        return results



def live_session():
    return SeaArtLiveSession()
