import sqlite3
import os

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
                area_pkid         INTEGER,
                shelter_pkid      INTEGER,
                synced_at         TEXT
            )
        """)
    conn.close()
