"""Optional Renderoverse LoRA discovery adapter for maintainer review."""

import json
import re
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common.model import Model
from .common.processors import classify_architecture


NAME = "renderoverse"
DISPLAY = "Renderoverse"
# Don't register the adapter or send requests from normal scans before review.
ENABLED = False
SITE = "https://renderoverse.com"
CATALOG = SITE + "/api/models?type=lora"
MAX_BYTES = 256 * 1024
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*", re.ASCII)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_catalog():
    request = Request(CATALOG, headers={"Accept": "application/json", "User-Agent": "AbyssBeacon"})
    with build_opener(NoRedirects).open(request, timeout=10) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise ValueError("Expected Renderoverse catalog JSON")
        payload = response.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError("Renderoverse catalog response exceeded the size limit")
    return json.loads(payload)


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit or not value.isprintable():
        return ""
    return value.strip()


def normalize_catalog(payload, term="", *, base_model_only=False):
    """Map the returned API page into link-only models; do not infer access."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a Renderoverse catalog object")
    rows, total = payload.get("models"), payload.get("total")
    if not isinstance(rows, list) or type(total) is not int or total < len(rows):
        raise ValueError("Invalid Renderoverse catalog rows or count")

    query = " ".join(str(term or "").casefold().split())
    results = {}
    for row in rows:
        # Unlabeled and mature entries stay out of this proposed first pass.
        if not isinstance(row, dict) or row.get("type") != "lora" or row.get("nsfw") is not False:
            continue
        slug, name = _text(row.get("slug"), 160), _text(row.get("name"), 300)
        if not name or not SLUG.fullmatch(slug) or slug in results:
            continue
        base_model = _text(row.get("baseModel"), 120)
        base_key = " ".join(base_model.casefold().split())
        searchable = " ".join((name, slug, base_model)).casefold()
        if query and (query != base_key if base_model_only else query not in searchable):
            continue

        model = Model()
        model.name = model.display_name = name
        model.author = _text(row.get("authorName"), 120) or _text(row.get("authorUsername"), 120)
        model.source = NAME
        model.url = f"{SITE}/model/{slug}"
        model.model_key = slug
        model.model_type = "LoRA"
        model.base_model = base_model
        model.architecture = classify_architecture(base_model)
        model.gated = True  # The scanner offers a source page, not an installer.
        price = row.get("directPriceCents")
        price = price if row.get("hasDirectDownload") is True and type(price) is int and price >= 0 else None
        model.card_data = {"nsfw": False, NAME: {"discovery_only": True, "direct_price_cents": price}}
        results[slug] = model
    return list(results.values())


def scan(term, scan_seen_models=None, scan_settings=None):
    """Read one public catalog page; search locally without sending user terms."""
    settings = scan_settings or {}
    mode = settings.get("_search_mode", "text")
    if mode not in {"text", "base_model"}:
        return []
    return normalize_catalog(fetch_catalog(), term, base_model_only=mode == "base_model")
