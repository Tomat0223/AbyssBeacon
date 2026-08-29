import json
import os
import re
import threading

_SECRETS_PATH = os.path.abspath("secrets.json")
_LOCK = threading.RLock()

_SOURCE_ENV = {
    "huggingface": "HF_TOKEN",
    "modelscope": "MODELSCOPE_API_TOKEN",
    "civitai": "CIVITAI_TOKEN",
    "tensorhub": "TENSORART_SESSION_TOKEN",
}


def _load_unlocked():
    try:
        with open(_SECRETS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print("WARNING: could not read local AbyssBeacon secrets:", exc)
        return {}


def load_secrets():
    with _LOCK:
        return _load_unlocked()


def _write_unlocked(data):
    temp = _SECRETS_PATH + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, _SECRETS_PATH)
    try:
        os.chmod(_SECRETS_PATH, 0o600)
    except Exception:
        pass


def _clean_token(value, prefixes=()):
    value = str(value or "").strip()
    for prefix in prefixes:
        if value.lower().startswith(prefix.lower() + "="):
            return value.split("=", 1)[1].strip()
    return value


def set_source_token(source, token=""):
    source = str(source or "").strip().lower()
    if source not in _SOURCE_ENV:
        raise ValueError(f"Unsupported token source: {source}")
    with _LOCK:
        data = _load_unlocked()
        section = data.setdefault(source, {})
        section["token"] = _clean_token(token)
        _write_unlocked(data)


def clear_source_token(source):
    source = str(source or "").strip().lower()
    with _LOCK:
        data = _load_unlocked()
        data.pop(source, None)
        _write_unlocked(data)


def get_source_token(source):
    source = str(source or "").strip().lower()
    env_name = _SOURCE_ENV.get(source)
    env_value = os.environ.get(env_name, "") if env_name else ""
    if source == "civitai" and not env_value:
        env_value = os.environ.get("CIVITAI_API_KEY", "")
    if source == "tensorhub" and not env_value:
        # Backward-compatible with the short-lived v3.8 TAMS variable name.
        env_value = os.environ.get("TENSORART_API_KEY", "")
    if env_value:
        return env_value.strip()
    data = load_secrets().get(source, {})
    return str(data.get("token") or "").strip()


def source_token_configured(source):
    return bool(get_source_token(source))



def set_civitai_search_key(search_key=""):
    """Store CivitAI's website-search bearer independently from the API key."""
    value = str(search_key or "").strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()

    with _LOCK:
        data = _load_unlocked()
        section = data.setdefault("civitai_search", {})
        section["token"] = value
        _write_unlocked(data)


def get_civitai_search_key():
    """Environment variable overrides the separately persisted search key."""
    value = os.environ.get("CIVITAI_SEARCH_KEY", "").strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if value:
        return value

    data = load_secrets().get("civitai_search", {})
    return str(data.get("token") or "").strip()


def clear_civitai_search_key():
    with _LOCK:
        data = _load_unlocked()
        data.pop("civitai_search", None)
        _write_unlocked(data)


def clear_civitai_credentials():
    """Clear both independent CivitAI credentials only when explicitly requested."""
    with _LOCK:
        data = _load_unlocked()
        data.pop("civitai", None)
        data.pop("civitai_search", None)
        _write_unlocked(data)


def civitai_search_configured():
    return bool(get_civitai_search_key())


def set_civitaired_credentials(session_token="", device_token=""):
    """Store local CivitAI Red session values. Passwords are never stored."""
    with _LOCK:
        data = _load_unlocked()
        red = data.setdefault("civitaired", {})

        cleaned_session = _clean_token(session_token, ("__Secure-civ-token",))
        cleaned_device = _clean_token(device_token, ("__Secure-civ-device",))

        # Save independently: a blank field means keep the current saved value.
        if cleaned_session:
            red["session_token"] = cleaned_session
        if cleaned_device:
            red["device_token"] = cleaned_device

        _write_unlocked(data)


def clear_civitaired_credentials():
    with _LOCK:
        data = _load_unlocked()
        data.pop("civitaired", None)
        _write_unlocked(data)


def get_civitaired_credentials():
    """Environment variables override the local secrets file."""
    data = load_secrets().get("civitaired", {})
    return {
        "session_token": os.environ.get("CIVITAI_RED_TOKEN", "") or data.get("session_token", ""),
        "device_token": os.environ.get("CIVITAI_RED_DEVICE", "") or data.get("device_token", ""),
    }


def civitaired_configured():
    return bool(get_civitaired_credentials().get("session_token"))





def _decode_windows_curl_arg(value):
    """Decode one Firefox/Chromium Windows Copy-as-cURL argument.

    Important: do this *after* isolating the whole argument. Some SeaArt cookie
    values (for example browser/login state) can themselves contain quoted JSON.
    Converting caret-escaped quotes before splitting headers can truncate the
    Cookie header and silently drop the actual authenticated session.
    """
    value = str(value or "")

    # Firefox's Windows cURL can double-escape embedded quotes in JSON/cookies.
    value = value.replace('^\\^"', '"')
    value = value.replace('^\\^"', '"')
    value = value.replace('^"', '"')

    # Standard cmd.exe caret escapes.
    for escaped, literal in (
        ("^^", "^"),
        ("^&", "&"),
        ("^|", "|"),
        ("^<", "<"),
        ("^>", ">"),
        ("^(", "("),
        ("^)", ")"),
    ):
        value = value.replace(escaped, literal)

    return value


def _parse_seaart_curl_request(curl_text):
    """Parse a SeaArt browser Copy-as-cURL request without executing it.

    The parser intentionally reads Windows cURL one argument/line at a time.
    SeaArt cookies can contain embedded quoted state; the older parser first
    converted caret-escaped quotes and then used a quote-delimited regex, which
    could cut the Cookie header at the first embedded quote. That made an exact
    cURL work in PowerShell while AbyssBeacon replayed an incomplete logged-out
    session.
    """
    raw_text = str(curl_text or "").strip()
    if not raw_text:
        raise ValueError("Paste a SeaArt Copy as cURL request first.")

    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def extract_quoted_argument(line, flag=None):
        line = str(line or "").strip()
        if flag:
            match = re.match(
                rf'^{re.escape(flag)}\s+\^"(.*)\^"\s*\^?\s*$',
                line,
                re.I,
            )
            if match:
                return _decode_windows_curl_arg(match.group(1))

            match = re.match(
                rf'^{re.escape(flag)}\s+"(.*)"\s*$',
                line,
                re.I,
            )
            if match:
                return match.group(1)
            return None

        # curl.exe ^"URL^" ^   or   curl "URL"
        match = re.search(r'curl(?:\.exe)?\s+\^"(.*)\^"\s*\^?\s*$', line, re.I)
        if match:
            return _decode_windows_curl_arg(match.group(1))

        match = re.search(r'curl(?:\.exe)?\s+"(.*)"\s*$', line, re.I)
        if match:
            return match.group(1)
        return None

    url = ""
    headers = {}
    data_raw = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        if not url and re.search(r'\bcurl(?:\.exe)?\b', stripped, re.I):
            candidate = extract_quoted_argument(stripped)
            if candidate:
                url = candidate.strip()
                continue

        if re.match(r"^-H\b", stripped, re.I):
            raw_header = extract_quoted_argument(stripped, "-H")
            if raw_header is None or ":" not in raw_header:
                continue
            name, value = raw_header.split(":", 1)
            name = name.strip()
            value = value.strip()
            if name:
                headers[name] = value
            continue

        if re.match(r"^--data-raw\b", stripped, re.I):
            candidate = extract_quoted_argument(stripped, "--data-raw")
            if candidate is not None:
                data_raw = candidate

    # Fallback for single-line/non-Windows cURL forms.
    if not url:
        normalized = re.sub(r"\^\s*(?:\r?\n)", " ", raw_text)
        url_match = re.search(r'curl(?:\.exe)?\s+"([^"\r\n]+)"', normalized, re.I)
        if url_match:
            url = url_match.group(1).strip()

    if not url:
        raise ValueError("Could not find the request URL in the pasted cURL.")

    if "seaart.ai/api/" not in url:
        raise ValueError("That cURL does not appear to be a SeaArt API request.")

    if not headers:
        # Conservative fallback for cURL produced without cmd.exe caret quoting.
        normalized = re.sub(r"\^\s*(?:\r?\n)", " ", raw_text)
        for match in re.finditer(r'(?:^|\s)-H\s+"([^"\r\n]+)"', normalized, re.I):
            raw = match.group(1)
            if ":" not in raw:
                continue
            name, value = raw.split(":", 1)
            headers[name.strip()] = value.strip()

    lower = {k.lower(): (k, v) for k, v in headers.items()}
    required = ("x-device-id", "x-gray-tag", "x-browser-id", "x-page-id", "cookie")
    missing = [name for name in required if name not in lower or not lower[name][1]]
    if missing:
        raise ValueError(
            "SeaArt cURL is missing required browser state: " + ", ".join(missing)
        )

    # Never persist values curl must calculate from the replayed request body.
    drop = {"content-length", "host"}
    kept = {k: v for k, v in headers.items() if k.lower() not in drop}

    model_ver_no = ""
    body_for_match = data_raw or raw_text
    match = re.search(
        r'["\']?model_ver_no["\']?\s*:\s*["\']([A-Za-z0-9_-]{8,})["\']',
        body_for_match,
        re.I,
    )
    if not match:
        # Handles heavily caret-escaped Windows JSON without depending on exact
        # quote escaping.
        match = re.search(
            r'model_ver_no[^A-Za-z0-9_-]+([A-Za-z0-9_-]{8,})',
            body_for_match,
            re.I,
        )
    if match:
        model_ver_no = match.group(1).strip()

    return {
        "url": url,
        "headers": kept,
        "model_ver_no": model_ver_no,
    }


def _parse_windows_curl_headers(curl_text):
    """Backward-compatible SeaArt cURL header parser."""
    return _parse_seaart_curl_request(curl_text)["headers"]


def _seaart_cookie_has_login(headers):
    cookie = next(
        (str(v) for k, v in (headers or {}).items() if str(k).lower() == "cookie"),
        "",
    )
    return bool(re.search(r"(?:^|;\s*)T=([^;]+)", cookie))


def set_seaart_scan_session(curl_text=""):
    parsed = _parse_seaart_curl_request(curl_text)
    headers = parsed["headers"]
    with _LOCK:
        data = _load_unlocked()
        sea = data.setdefault("seaart", {})
        sea["scan_headers"] = headers
        sea.pop("headers", None)
        _write_unlocked(data)
    return headers


def _seaart_minimal_account_headers(headers):
    """Keep only the reusable signed-in state from a SeaArt /account/my request.

    SeaArt binds the T cookie to browser/device identity. T alone is not enough.
    We intentionally discard per-request IDs, Cloudflare cookies and telemetry
    so the stored connection has a chance to live as long as the account token.
    """
    source = {str(k): str(v) for k, v in (headers or {}).items()}
    lower = {k.casefold(): (k, v) for k, v in source.items()}

    keep_headers = (
        "user-agent",
        "accept-language",
        "x-platform",
        "x-project-id",
        "x-timezone",
        "x-device-id",
        "x-gray-tag",
        "x-browser-id",
        "x-page-id",
        "x-eyes",
        "x-app-id",
    )
    result = {}
    for wanted in keep_headers:
        found = lower.get(wanted)
        if found and found[1]:
            result[found[0]] = found[1]

    cookie_entry = lower.get("cookie")
    cookie_text = cookie_entry[1] if cookie_entry else ""
    allowed_cookies = {
        "t",
        "deviceid",
        "graytag",
        "pageid",
        "browserid",
        "app_id",
        "x-eyes",
        "lang",
    }
    kept = []
    for part in cookie_text.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        if name.strip().casefold() in allowed_cookies and value.strip():
            kept.append(f"{name.strip()}={value.strip()}")

    if kept:
        result["Cookie"] = "; ".join(kept)

    # Fresh request context is generated by the scanner on every call.
    result.setdefault("Accept", "application/json, text/plain, */*")
    result.setdefault("Content-Type", "application/json")
    result.setdefault("Origin", "https://www.seaart.ai")
    result.setdefault("X-Platform", "web")
    result.setdefault("X-Project-Id", "seaart")
    result.setdefault("X-App-Id", "web_global_seaart")
    return result


def set_seaart_account_token(token=""):
    """Store SeaArt's signed-in T browser token directly.

    This is deliberately smaller and more durable than replaying an entire
    /account/my browser request. The token is a credential and stays only in
    local secrets.json.
    """
    token = _clean_token(token, ("T",))
    if not token:
        raise ValueError("Paste the SeaArt T token first.")

    with _LOCK:
        data = _load_unlocked()
        sea = data.setdefault("seaart", {})
        sea["account_token"] = token
        # Prefer the minimal token path after import. Keep scan state separate.
        sea.pop("account_headers", None)
        sea.pop("download_headers", None)
        sea.pop("download_test_model_ver_no", None)
        _write_unlocked(data)
    return token


def get_seaart_account_token():
    data = load_secrets().get("seaart", {})
    if not isinstance(data, dict):
        return ""
    return str(data.get("account_token") or "").strip()


def set_seaart_account_session(curl_text=""):
    """Import /account/my but store only reusable signed-in browser identity."""
    parsed = _parse_seaart_curl_request(curl_text)
    headers = parsed["headers"]
    url = str(parsed.get("url") or "").strip()

    if "/api/v1/account/my" not in url:
        raise ValueError(
            "For the SeaArt Account Connection, copy the signed-in POST request ending in "
            "/api/v1/account/my from your SeaArt profile page."
        )

    minimal = _seaart_minimal_account_headers(headers)
    cookie = str(minimal.get("Cookie") or "")
    cookie_names = {
        part.split("=", 1)[0].strip().casefold()
        for part in cookie.split(";")
        if "=" in part
    }
    required = {"t", "deviceid", "browserid"}
    missing = sorted(required - cookie_names)
    if missing:
        raise ValueError(
            "The copied SeaArt account request is missing required signed-in browser state: "
            + ", ".join(missing)
            + ". Refresh SeaArt Personal while signed in and copy a fresh account/my request."
        )

    with _LOCK:
        data = _load_unlocked()
        sea = data.setdefault("seaart", {})
        sea["account_headers"] = minimal
        # T-only auth proved insufficient; remove it when a complete minimal
        # account connection is imported.
        sea.pop("account_token", None)
        sea.pop("download_headers", None)
        sea.pop("download_test_model_ver_no", None)
        _write_unlocked(data)

    return minimal


def set_seaart_download_session(curl_text=""):
    """Backward-compatible alias for the new harmless account session."""
    return set_seaart_account_session(curl_text)


def set_seaart_curl_session(curl_text=""):
    """Backward-compatible alias for the public scanning session."""
    return set_seaart_scan_session(curl_text)


def clear_seaart_scan_session():
    with _LOCK:
        data = _load_unlocked()
        sea = data.get("seaart")
        if isinstance(sea, dict):
            sea.pop("scan_headers", None)
            sea.pop("headers", None)
            if not sea:
                data.pop("seaart", None)
        _write_unlocked(data)


def clear_seaart_download_session():
    with _LOCK:
        data = _load_unlocked()
        sea = data.get("seaart")
        if isinstance(sea, dict):
            sea.pop("account_token", None)
            sea.pop("account_headers", None)
            sea.pop("download_headers", None)
            sea.pop("download_test_model_ver_no", None)
            if not sea:
                data.pop("seaart", None)
        _write_unlocked(data)


def clear_seaart_curl_session():
    with _LOCK:
        data = _load_unlocked()
        data.pop("seaart", None)
        _write_unlocked(data)


def get_seaart_scan_session():
    data = load_secrets().get("seaart", {})
    if not isinstance(data, dict):
        return {}
    headers = data.get("scan_headers")
    if not isinstance(headers, dict):
        headers = data.get("headers")
    return dict(headers) if isinstance(headers, dict) else {}


def get_seaart_download_session():
    data = load_secrets().get("seaart", {})
    if not isinstance(data, dict):
        return {}

    # SeaArt binds the account token to browser/device identity. Prefer the
    # minimal state imported from /account/my. A legacy T-only token is retained
    # only for compatibility and is not considered a complete connection.
    headers = data.get("account_headers")
    if isinstance(headers, dict) and headers:
        return dict(headers)

    token = str(data.get("account_token") or "").strip()
    if token:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://www.seaart.ai",
            "Cookie": f"T={token}",
            "X-Platform": "web",
            "X-Project-Id": "seaart",
            "X-App-Id": "web_global_seaart",
        }

    # Upgrade compatibility with the earlier getDownloadLink-based setup.
    headers = data.get("download_headers")
    if isinstance(headers, dict):
        return dict(headers)

    legacy = data.get("headers")
    if isinstance(legacy, dict) and _seaart_headers_configured(legacy):
        return dict(legacy)
    return {}



def get_seaart_download_test_model_ver_no():
    # Retained for compatibility; account-session validation no longer consumes
    # a SeaArt download chance.
    return ""



def get_seaart_curl_session():
    return get_seaart_scan_session()


def _seaart_headers_configured(headers):
    lower = {str(k).lower(): str(v or "") for k, v in (headers or {}).items()}
    return all(
        lower.get(k)
        for k in ("x-device-id", "x-gray-tag", "x-browser-id", "x-page-id", "cookie")
    )


def seaart_scan_configured():
    return _seaart_headers_configured(get_seaart_scan_session())


def seaart_download_configured():
    # T alone is not sufficient: SeaArt binds it to device/browser identity.
    return _seaart_headers_configured(get_seaart_download_session())


def seaart_curl_configured():
    return seaart_scan_configured()


def seaart_authenticated_session():
    return seaart_download_configured()


def seaart_connection_status():
    return {"scan": seaart_scan_configured(), "download": seaart_download_configured()}


def configured_sources():
    return {
        "huggingface": source_token_configured("huggingface"),
        "modelscope": source_token_configured("modelscope"),
        "civitai": source_token_configured("civitai"),
        "tensorhub": source_token_configured("tensorhub"),
        "civitaired": civitaired_configured(),
        "seaart": seaart_scan_configured(),
    }
