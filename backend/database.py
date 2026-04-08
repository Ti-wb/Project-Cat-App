import os
import sqlite3

DATABASE_PATH = os.getenv("DATABASE_PATH", "./data/cats.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = get_connection()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cats (
                animal_id         INTEGER PRIMARY KEY,
                animal_subid      TEXT,
                animal_place      TEXT,
                animal_variety    TEXT,
                animal_sex        TEXT,
                animal_bodytype   TEXT,
                animal_colour     TEXT,
                animal_age        TEXT,
                animal_sterilization TEXT,
                animal_bacterin   TEXT,
                animal_foundplace TEXT,
                animal_status     TEXT,
                animal_remark     TEXT,
                animal_opendate   TEXT,
                animal_closeddate TEXT,
                animal_update     TEXT,
                animal_createtime TEXT,
                shelter_name      TEXT,
                shelter_address   TEXT,
                shelter_tel       TEXT,
                album_file        TEXT,
                local_image       TEXT,
                source_album_url  TEXT,
                source_animal_update TEXT,
                source_album_update TEXT,
                area_pkid         INTEGER,
                shelter_pkid      INTEGER,
                synced_at         TEXT,
                first_seen_at     TEXT,
                last_seen_at      TEXT
            )
        """)
        _ensure_column(conn, "cats", "source_album_url", "TEXT")
        _ensure_column(conn, "cats", "source_animal_update", "TEXT")
        _ensure_column(conn, "cats", "source_album_update", "TEXT")
        _ensure_column(conn, "cats", "first_seen_at", "TEXT")
        _ensure_column(conn, "cats", "last_seen_at", "TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS image_fetch_state (
                animal_id         INTEGER PRIMARY KEY,
                source_url_hash   TEXT NOT NULL,
                last_attempt_at   TEXT,
                last_success_at   TEXT,
                failure_count     INTEGER NOT NULL DEFAULT 0,
                last_error_code   TEXT,
                next_eligible_at  TEXT,
                status            TEXT NOT NULL
                    CHECK (status IN ('pending', 'success', 'cooldown', 'terminal_failure'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_runs (
                id                INTEGER PRIMARY KEY,
                started_at        TEXT NOT NULL,
                finished_at       TEXT,
                phase             TEXT NOT NULL,
                status            TEXT NOT NULL
                    CHECK (status IN ('running', 'success', 'failed', 'partial')),
                pages_fetched     INTEGER NOT NULL DEFAULT 0,
                records_seen      INTEGER NOT NULL DEFAULT 0,
                records_upserted  INTEGER NOT NULL DEFAULT 0,
                records_removed   INTEGER NOT NULL DEFAULT 0,
                images_attempted  INTEGER NOT NULL DEFAULT 0,
                images_succeeded  INTEGER NOT NULL DEFAULT 0,
                images_failed     INTEGER NOT NULL DEFAULT 0,
                error_summary     TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cat_removals (
                id                      INTEGER PRIMARY KEY,
                animal_id               INTEGER NOT NULL,
                removed_at              TEXT NOT NULL,
                detected_in_run_id      INTEGER NOT NULL,
                last_seen_at            TEXT NOT NULL,
                last_known_animal_update TEXT,
                last_known_album_update TEXT,
                last_known_album_url    TEXT,
                snapshot_json           TEXT NOT NULL,
                removal_reason          TEXT NOT NULL,
                FOREIGN KEY(detected_in_run_id) REFERENCES sync_runs(id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_cat_removals_animal_id_removed_at
            ON cat_removals (animal_id, removed_at DESC)
        """)
    conn.close()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
