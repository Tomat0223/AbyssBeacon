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
            options.binary_location = exe
            options.add_argument("-headless")
            options.add_argument("-profile")
            options.add_argument(str(profile))
            driver = webdriver.Firefox(options=options)
        elif self.browser == "chrome":
            options = ChromeOptions()
            options.binary_location = exe
            options.add_argument("--headless=new")
            options.add_argument(f"--user-data-dir={profile}")
            options.add_argument("--profile-directory=Default")
            options.add_argument("--window-size=1100,900")
            driver = webdriver.Chrome(options=options)
        else:
            options = EdgeOptions()
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

    def _try_newest(self):
        """Best-effort switch of SeaArt's search UI to Newest.

        We deliberately drive SeaArt's own UI instead of modifying signed API bodies.
        The site has changed this control more than once, so keep selectors text-based.
        """
        if self.driver is None:
            return False
        try:
            from selenium.webdriver.common.by import By
        except Exception:
            return False

        # If Newest is already visible, click it directly.
        expressions = [
            "//*[normalize-space()='Newest']",
            "//*[contains(translate(normalize-space(.),'NEWEST','newest'),'newest')]",
        ]
        for xpath in expressions:
            try:
                for el in self.driver.find_elements(By.XPATH, xpath):
                    if el.is_displayed():
                        self.driver.execute_script("arguments[0].click();", el)
                        time.sleep(1.4)
                        return True
            except Exception:
                pass

        # Otherwise open a likely sort control, then try Newest again.
        for label in ("Hot", "Popular", "Recommended", "Sort"):
            try:
                candidates = self.driver.find_elements(
                    By.XPATH,
                    f"//*[contains(translate(normalize-space(.),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'{label.lower()}')]",
                )
                target = next((x for x in candidates if x.is_displayed()), None)
                if target is None:
                    continue
                self.driver.execute_script("arguments[0].click();", target)
                time.sleep(.5)
                for el in self.driver.find_elements(By.XPATH, "//*[normalize-space()='Newest']"):
                    if el.is_displayed():
                        self.driver.execute_script("arguments[0].click();", el)
                        time.sleep(1.4)
                        return True
            except Exception:
                continue
        return False

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

        Detail/account/download endpoints observed in SeaArt do not require the volatile
        X-Sign discovery header set, so a same-origin browser fetch is sufficient and
        automatically uses the current browser profile's live cookies/session state.
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
            fetch(url, {
                method: 'POST',
                credentials: 'include',
                headers: {
                    'Accept': 'application/json, text/plain, */*',
                    'Content-Type': 'application/json',
                    'X-Platform': 'web',
                    'X-Project-Id': 'seaart',
                    'X-App-Id': 'web_global_seaart'
                },
                body: JSON.stringify(payload)
            }).then(async r => {
                const text = await r.text();
                done({ok: r.ok, status: r.status, text});
            }).catch(e => done({ok:false, status:0, text:String(e)}));
        """
        result = self.driver.execute_async_script(script, str(url), payload)
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


def live_session():
    return SeaArtLiveSession()
