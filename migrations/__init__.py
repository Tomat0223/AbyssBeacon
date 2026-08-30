"""One-time AbyssBeacon database/data migrations.

Migration files remain in the source tree intentionally so users can upgrade
across multiple AbyssBeacon releases without wiping their existing database.
Each migration is recorded in the app_migrations table after it succeeds and is
never run again for that database.
"""
