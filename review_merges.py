"""Offline whole-card merge review. Default: read-only. Stop AbyssBeacon before --apply."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='models.db')
    parser.add_argument('--output', help='Write the report directly as UTF-8 JSON, avoiding shell redirection encoding.')
    parser.add_argument('--apply', action='store_true', help='Back up and apply unambiguous complete matches; close AbyssBeacon first.')
    args = parser.parse_args(argv)
    path = Path(args.database).resolve(strict=True)
    output = Path(args.output).resolve() if args.output else None
    if output == path:
        parser.error('--output must not overwrite the database')
    # Importing these helpers does not initialize/migrate the database.
    from database import preview_cross_source_merges, reconcile_cross_source_sha256_duplicates
    connection = sqlite3.connect(path.as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        if args.apply:
            # Take the write lock before backup/rechecking so the preview and
            # applied candidates describe one consistent database state.
            connection.execute('BEGIN IMMEDIATE')
        preview = preview_cross_source_merges(connection.cursor())
        result = {'mode': 'apply' if args.apply else 'preview', **preview}
        if args.apply and preview['candidates']:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = path.with_name(f'{path.stem}.before-merges-{stamp}{path.suffix}')
            # A separate reader can use SQLite backup while this connection
            # holds the RESERVED write lock. Avoid backing up a write transaction.
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as reader:
                with sqlite3.connect(backup) as target:
                    reader.backup(target)
            result['backup'] = str(backup)
            result['applied'] = reconcile_cross_source_sha256_duplicates(connection.cursor())
        if args.apply:
            connection.commit()
        if output:
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        else:
            # Redirected Windows stdout may use CP1252. JSON escapes preserve
            # every Unicode name while keeping this stream ASCII-compatible.
            print(json.dumps(result, ensure_ascii=True, indent=2))
        return result
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    main()
