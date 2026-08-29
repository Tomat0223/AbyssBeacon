from scan_logging import verbose_print as print
NAME = "huggingface"
DISPLAY = "Hugging Face"
ENABLED = True
import requests, time, database
from scanners.common import metadata, media, processors

from datetime import datetime, timedelta
from urllib.parse import quote

import scan_control, scan_status
from secrets_manager import get_source_token
from scanners.http_retry import get_with_backoff


HF_API = "https://huggingface.co/api/models"


session = requests.Session()

session.headers.update({
    "User-Agent": "AbyssBeacon/1.0"
})


def _apply_auth():
    token = get_source_token("huggingface")
    if token:
        session.headers["Authorization"] = f"Bearer {token}"
    else:
        session.headers.pop("Authorization", None)



def scan(
    term,
    scan_seen_models=None,
    scan_settings=None,
    creator=None
):
    _apply_auth()

    detail_fetches = 0
    readme_fetches = 0
    preview_models = 0
    gated_models = 0
    media_files = 0

    scan_settings = scan_settings or {}

    SEARCH_DAYS = int(scan_settings.get("search_days", 7))
    MAX_RESULTS = max(1, int(scan_settings.get("max_results", 100)))
    SORT_MODE = scan_settings.get("sort", "newest_updated")

    sort_map = {
        "newest_updated": "lastModified",
        "newest_created": "createdAt",
        "downloads": "downloads",
        "likes": "likes",
        "trending": "trendingScore"
    }
    api_sort = sort_map.get(SORT_MODE, "lastModified")

    start_time = time.perf_counter()

    cutoff = datetime.utcnow() - timedelta(days=SEARCH_DAYS)


    results = []

    if scan_seen_models is None:
        scan_seen_models = set()

    duplicates = 0
    old_models = 0


    query = term

    if creator:
        print(f"\nCREATOR SCAN: {creator}")
    else:
        print(f"\nSEARCH TERM: {query}")

    # AbyssBeacon exposes a total result ceiling rather than page controls.
    # Hugging Face pagination is followed via the API's Link header.
    per_request = 1000
    target_results = 10000 if creator else MAX_RESULTS
    params = {
        "limit": min(per_request, target_results),
        "sort": api_sort,
        "direction": -1
    }

    if creator:
        params["author"] = creator
    else:
        params["search"] = query

    items = []
    next_url = HF_API
    next_params = params
    page_number = 0

    while next_url and len(items) < target_results:
        if scan_control.should_stop():
            print("Hugging Face scan stopped")
            return results

        page_number += 1
        try:
            r = get_with_backoff(
                session, next_url, provider="Hugging Face",
                label=f"search page {page_number}",
                params=next_params, timeout=15
            )
        except Exception as e:
            print("HF connection error:", e)
            break

        if r.status_code == 429:
            print("Hugging Face search stopped after repeated rate limiting")
            break

        if r.status_code != 200:
            print("HF ERROR:", r.status_code)
            break

        page_items = r.json()
        if not isinstance(page_items, list):
            break

        remaining = target_results - len(items)
        items.extend(page_items[:remaining])
        print(f"Hugging Face page {page_number}: {len(page_items)} results")

        if len(items) >= target_results or not page_items:
            break

        link_header = r.headers.get("Link", "")
        next_link = ""
        if link_header:
            try:
                from requests.utils import parse_header_links
                links = parse_header_links(link_header.rstrip("> ").replace(">,<", ">, <"))
                for link in links:
                    if link.get("rel") == "next" and link.get("url"):
                        next_link = link["url"]
                        break
            except Exception:
                next_link = ""

        if not next_link:
            break

        next_url = next_link
        next_params = None

    print(f"Hugging Face results inspected: {len(items)}")

    for item in items:

        if scan_control.should_stop():
            print("Hugging Face scan stopped")
            return results


        model_id = item.get("id")

        seen_key = ("huggingface", model_id.lower())

        if seen_key in scan_seen_models:
            continue

        scan_seen_models.add(seen_key)


        author = model_id.split("/")[0] if "/" in model_id else ""
        blocked = {str(x).casefold() for x in (scan_settings.get("_blocked_creators") or [])}
        if author and author.casefold() in blocked:
            continue

        model_url = f"https://huggingface.co/{model_id}"

        model_key = model_id.lower()

        existing_model = database.get_model(
            model_key,
            "huggingface"
        )

        details = None


        # FAST DUPLICATE CHECK USING TIMESTAMP

        if existing_model:

            api_sha = item.get(
                "sha",
                ""
            )

            db_sha = existing_model["sha"] or ""


            api_modified = item.get(
                "lastModified",
                ""
            )

            db_modified = existing_model["updated"] or ""


            # SHA is the strongest indicator
            if api_sha and db_sha:

                if api_sha == db_sha:

                    duplicates += 1
                    continue


            # fallback if SHA is unavailable
            elif api_modified and api_modified == db_modified:

                duplicates += 1
                continue


            # model requires refresh


            scan_status.update_status(
                status="running",
                source="huggingface",
                current=model_id,
            )


        created = item.get(
            "createdAt",
            ""
        )

        # Search Days follows the selected time-based sort. For popularity
        # sorts, keep the age window based on creation date.
        cutoff_value = (
            item.get("lastModified", "")
            if SORT_MODE == "newest_updated"
            else created
        )

        if cutoff_value and not creator:

            try:

                model_date = datetime.fromisoformat(
                    cutoff_value.replace(
                        "Z",
                        "+00:00"
                    )
                ).replace(
                    tzinfo=None
                )

                if model_date < cutoff:

                    old_models += 1
                    continue

            except Exception:

                pass


        # FAST DUPLICATE CHECK USING SEARCH RESULT

        repo_sha = item.get(
            "sha",
            ""
        )


        # ONLY FETCH DETAILS FOR NEW OR CHANGED MODELS

        try:

            time.sleep(0.05)

            detail_fetches += 1

            detail_response = get_with_backoff(
                session,
                f"https://huggingface.co/api/models/{model_id}",
                provider="Hugging Face",
                label=f"model detail {model_id}",
                # Hugging Face only includes RepoSibling size/LFS metadata
                # when file metadata is requested. This maps to
                # HfApi.model_info(..., files_metadata=True).
                params={"blobs": "true"},
                timeout=15
            )

            if detail_response.status_code != 200:
                print(
                    "DETAIL ERROR:",
                    model_id,
                    detail_response.status_code
                )
                continue

            details = detail_response.json()

            card_data = details.get("cardData", {}) or {}

            gated = False

            if details.get("gated"):
                gated = True

            card_data["gated"] = gated

            if gated:
                gated_models += 1

            # README is also our description fallback. Fetch it for new models,
            # or for an existing model whose stored/API description is still blank.
            api_description = metadata.extract_description(details)
            stored_description = (existing_model["description"] or "") if existing_model else ""
            if (not existing_model) or (not api_description and not stored_description):

                try:

                    readme_fetches += 1

                    readme_response = get_with_backoff(
                        session,
                        f"https://huggingface.co/{model_id}/raw/main/README.md",
                        provider="Hugging Face",
                        label=f"README {model_id}",
                        timeout=10
                    )

                    if readme_response.status_code == 200:
                        details["readme"] = readme_response.text
                    else:
                        details["readme"] = ""

                except Exception:
                    details["readme"] = ""

            else:
                details["readme"] = ""


        except Exception as e:

            print(
                "DETAIL ERROR:",
                model_id,
                type(e).__name__,
                e
            )

            continue

        raw_tags = item.get(
            "tags",
            []
        )


        if isinstance(raw_tags, list):

            tag_values = []
            for tag in raw_tags:
                if not isinstance(tag, str):
                    continue
                text = tag.strip()
                if not text:
                    continue
                prefix = text.split(":", 1)[0].strip().casefold() if ":" in text else ""
                if prefix in {"region", "license", "library_name", "pipeline_tag"}:
                    continue
                tag_values.append(text)
            tags = ",".join(dict.fromkeys(tag_values))

        else:

            tags = str(raw_tags or "")
            if tags.casefold().startswith("region:"):
                tags = ""

        sensitive = metadata.detect_sensitive(
            model_id,
            tags,
            card_data,
            details.get("tags", [])
        )

        # Keep repository-file metadata in the same normalized format used
        # by ModelScope so the download panel works for both sources.
        files = []
        for sibling in details.get("siblings", []) or []:
            if not isinstance(sibling, dict):
                continue

            filename = sibling.get("rfilename", "")
            if not filename:
                continue

            lower_name = filename.lower()
            lfs = sibling.get("lfs", {}) or {}
            size = sibling.get("size", 0) or lfs.get("size", 0) or 0
            primary = lower_name.endswith((
                ".safetensors", ".ckpt", ".pt", ".pth",
                ".bin", ".gguf"
            ))

            encoded_path = quote(filename, safe="/")
            resolve_url = (
                f"https://huggingface.co/{model_id}/resolve/main/"
                f"{encoded_path}"
            )
            download_url = f"{resolve_url}?download=true"

            files.append({
                "name": filename.split("/")[-1],
                "path": filename,
                "size": size,
                "size_bytes": size,
                "sha256": lfs.get("sha256", ""),
                "is_lfs": bool(lfs),
                "revision": details.get("sha", "") or "main",
                "download_url": download_url,
                "media_url": resolve_url,
                "primary": primary
            })

        media_data = media.extract_media(
            files,
            f"https://huggingface.co/{model_id}/resolve/main"
        )

        preview = media_data["image"]

        preview_count = media_data["preview_count"]

        has_video = media_data["has_video"]

        has_media = media_data["has_media"]

        model_media = media_data["media"]


        if preview:
            preview_models += 1


        model_media_count = len(model_media)
        media_files += model_media_count

        raw_model = {

            "details": details,

            "model_id": model_id,

            "model_key": model_key,

            "tags": tags,

            "files": files,

            "image": preview,

            "preview_count": preview_count,

            "has_media": has_media,

            "has_video": has_video,

            "media": model_media,

            "gated": gated,

            "card_data": card_data,

            "pipeline": details.get("pipeline_tag") or details.get("pipelineTag") or "",
            "library": details.get("library_name") or details.get("libraryName") or "",

            "sensitive": sensitive,

            "source": "huggingface",

            "url": model_url,

            "sha": repo_sha,

            "_existing": bool(existing_model),

            "_existing_id":
                existing_model["id"] if existing_model else None

        }


        processed_model = processors.build_model(
            raw_model
        )

        if str(getattr(processed_model, "architecture", "") or "").casefold() == "other":
            processed_model.architecture = processors.classify_architecture_with_watch_fallback(
                scan_settings.get("_watch_architecture"),
                getattr(processed_model, "base_model", ""),
                getattr(processed_model, "name", ""),
                getattr(processed_model, "display_name", ""),
                getattr(processed_model, "tags", ""),
                getattr(processed_model, "description", ""),
                raw_model.get("card_data"),
            )


        results.append(
            processed_model
        )


    elapsed = time.perf_counter() - start_time


    print("\n========================================")
    print("Hugging Face Scan Complete")
    print("========================================")
    print(f"Processed models : {len(results)}")
    print(f"Old models : {old_models}")
    print(f"Duplicates : {duplicates}")
    print(f"Time       : {elapsed:.2f} seconds")
    print(f"Detail fetches: {detail_fetches}")
    print(f"README fetches: {readme_fetches}")
    print(f"Models with previews: {preview_models}")
    print(f"Gated models: {gated_models}")
    print(f"Media files found: {media_files}")


    return results
