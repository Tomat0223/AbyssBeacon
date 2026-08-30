"""Recover legacy merged-card media for the source-aware gallery model.

AbyssBeacon v1.0.1 and earlier could collapse multiple providers into one model
while retaining only one provider's gallery. Deleted gallery rows cannot be
reconstructed offline, but model_sources often still contains a provider-local
preview URL. This migration seeds that preview only for providers that currently
have no media, giving the next normal scan or Reload Model a safe source-local
place to rebuild the complete gallery.
"""

import json


MIGRATION_ID = "source_aware_merged_media_v1"


def run(cursor):
    attributed = 0
    seeded = 0
    missing = 0

    # Very old rows may predate reliable source attribution on model_media.
    # Their only defensible owner is the canonical models.source value.
    rows = cursor.execute(
        """
        SELECT mm.id, lower(COALESCE(m.source,'')) AS source
        FROM model_media mm
        JOIN models m ON m.id=mm.model_id
        WHERE trim(COALESCE(mm.source,''))=''
          AND trim(COALESCE(m.source,''))<>''
        """
    ).fetchall()
    for row in rows:
        cursor.execute(
            "UPDATE model_media SET source=? WHERE id=?",
            (row["source"], int(row["id"])),
        )
        attributed += 1

    source_rows = cursor.execute(
        """
        SELECT ms.model_id, lower(COALESCE(ms.source,'')) AS source,
               ms.source_data, ms.url,
               lower(COALESCE(m.source,'')) AS canonical_source,
               m.image AS canonical_image
        FROM model_sources ms
        JOIN models m ON m.id=ms.model_id
        WHERE trim(COALESCE(ms.source,''))<>''
        ORDER BY ms.model_id, ms.id
        """
    ).fetchall()

    for row in source_rows:
        model_id = int(row["model_id"] or 0)
        source = str(row["source"] or "").strip().lower()
        if not model_id or not source:
            continue

        existing = cursor.execute(
            """SELECT 1 FROM model_media
               WHERE model_id=? AND lower(COALESCE(source,''))=? LIMIT 1""",
            (model_id, source),
        ).fetchone()
        if existing:
            continue

        try:
            snapshot = json.loads(row["source_data"] or "{}")
        except Exception:
            snapshot = {}
        if not isinstance(snapshot, dict):
            snapshot = {}

        preview = str(snapshot.get("image") or "").strip()
        # Never borrow another provider's canonical preview for a sibling source.
        # A canonical source may use its own historical card image as a fallback.
        if not preview and source == str(row["canonical_source"] or "").strip().lower():
            preview = str(row["canonical_image"] or "").strip()

        if not preview:
            missing += 1
            continue

        lower_preview = preview.lower().split("?", 1)[0]
        media_type = "video" if lower_preview.endswith((".mp4", ".webm", ".mov")) else "image"
        filename = "preview-1.mp4" if media_type == "video" else "preview-1.jpg"
        metadata_blob = json.dumps(
            {"source_aware_backfill": True, "recovery": "source_snapshot_preview"},
            ensure_ascii=False,
        )
        cursor.execute(
            """
            INSERT INTO model_media
            (model_id,source,type,url,thumbnail,filename,path,metadata,position)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                model_id,
                source,
                media_type,
                preview,
                preview if media_type == "video" else "",
                filename,
                "",
                metadata_blob,
                0,
            ),
        )
        seeded += 1

    details = f"attributed={attributed}; seeded={seeded}; missing={missing}"
    repaired = attributed + seeded
    if repaired:
        message = (
            "Source-aware media upgrade: repaired "
            f"{attributed} unattributed media row(s); "
            f"seeded {seeded} missing source preview(s)."
        )
    elif missing:
        message = (
            "Source-aware media upgrade: no recoverable legacy previews were stored; "
            "normal scans / Reload Model will populate independent source galleries."
        )
    else:
        message = ""

    return {
        "attributed": attributed,
        "seeded": seeded,
        "missing": missing,
        "details": details,
        "message": message,
    }
