"""Слой доступа к БД.

Два бэкенда:
- Postgres — общая база проектов Альфы, ОТДЕЛЬНАЯ схема config.DB_SCHEMA
  (по умолчанию 'stok_analiz'). Соединения берутся из пула (быстро + потокобезопасно
  под gunicorn: web-потоки + фоновый планировщик). search_path выставляется на схему
  при подключении, поэтому таблицы создаются и читаются внутри неё.
- SQLite — локальный файл, fallback для отладки без доступа к Postgres.

Публичный API одинаков: init_db(), query(), query_one(), execute(), execute_many().
В SQL используем плейсхолдеры '?'; для Postgres они транслируются в '%s'."""
import os
import sqlite3
from contextlib import contextmanager

import config

_TABLES = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          {pk},
    fetched_at  TEXT NOT NULL,
    row_count   INTEGER,
    raw_hash    TEXT
);
CREATE TABLE IF NOT EXISTS listings (
    vin         TEXT PRIMARY KEY,
    date_added  TEXT,
    make_raw    TEXT,
    brand       TEXT,
    year        INTEGER,
    year_flag   TEXT,
    region_raw  TEXT,
    region      TEXT,
    address     TEXT,
    price       INTEGER,
    url         TEXT,
    ban_court   INTEGER,
    ban_fssp    INTEGER,
    first_seen  TEXT,
    last_seen   TEXT,
    status      TEXT DEFAULT 'active',
    removed_at  TEXT
);
CREATE TABLE IF NOT EXISTS price_history (
    id          {pk},
    vin         TEXT NOT NULL,
    price       INTEGER,
    observed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS changes (
    id          {pk},
    vin         TEXT NOT NULL,
    ctype       TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    title       TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS valuations (
    vin           TEXT PRIMARY KEY,
    input_hash    TEXT,
    market_low    INTEGER,
    market_mid    INTEGER,
    market_high   INTEGER,
    days_to_sell  INTEGER,
    reasoning     TEXT,
    comparables   TEXT,
    model         TEXT,
    created_at    TEXT
);
"""

# ---------------- Postgres (пул соединений) ----------------

_pool = None


def _pg_pool():
    global _pool
    if _pool is None:
        from psycopg2.pool import ThreadedConnectionPool
        # search_path задаём при подключении — не нужно SET на каждый запрос
        opts = f"-c search_path={config.DB_SCHEMA},public"
        common = dict(connect_timeout=10, options=opts)
        if config.DATABASE_URL:
            _pool = ThreadedConnectionPool(1, 8, config.DATABASE_URL, **common)
        else:
            _pool = ThreadedConnectionPool(
                1, 8, host=config.DB_HOST, dbname=config.DB_NAME, user=config.DB_USER,
                password=config.DB_PASSWORD, port=config.DB_PORT, **common)
    return _pool


@contextmanager
def _pg_conn():
    pool = _pg_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _pg_init():
    import psycopg2
    # схему создаём отдельным соединением без привязки к ней
    dsn = config.DATABASE_URL
    conn = psycopg2.connect(dsn, connect_timeout=10) if dsn else psycopg2.connect(
        host=config.DB_HOST, dbname=config.DB_NAME, user=config.DB_USER,
        password=config.DB_PASSWORD, port=config.DB_PORT, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {config.DB_SCHEMA}")
            cur.execute(f"SET search_path TO {config.DB_SCHEMA}, public")
            cur.execute(_TABLES.format(pk="SERIAL PRIMARY KEY"))
        conn.commit()
    finally:
        conn.close()


def _pg_query(sql, params):
    import psycopg2.extras
    with _pg_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql.replace("?", "%s"), params)
            return [dict(r) for r in cur.fetchall()]


def _pg_execute(sql, params):
    with _pg_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql.replace("?", "%s"), params)


def _pg_execute_many(ops):
    # все операции — одним соединением в одной транзакции (атомарно + быстро)
    with _pg_conn() as conn:
        with conn.cursor() as cur:
            for sql, params in ops:
                cur.execute(sql.replace("?", "%s"), params)


# ---------------- SQLite ----------------

@contextmanager
def _sqlite_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _sqlite_init():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    with _sqlite_conn() as c:
        c.executescript(_TABLES.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT"))


# ---------------- Публичный API ----------------

def init_db():
    _pg_init() if config.USE_POSTGRES else _sqlite_init()


def query(sql, params=()):
    if config.USE_POSTGRES:
        return _pg_query(sql, params)
    with _sqlite_conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def query_one(sql, params=()):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=()):
    if config.USE_POSTGRES:
        return _pg_execute(sql, params)
    with _sqlite_conn() as c:
        return c.execute(sql, params).lastrowid


def execute_many(ops):
    """Список (sql, params) одной транзакцией. Для ingest: атомарно и без
    рукопожатия на каждый INSERT."""
    if not ops:
        return
    if config.USE_POSTGRES:
        _pg_execute_many(ops)
    else:
        with _sqlite_conn() as c:
            for sql, params in ops:
                c.execute(sql, params)


def backend_info():
    if config.USE_POSTGRES:
        host = config.DB_HOST or (config.DATABASE_URL or "").split("@")[-1].split("/")[0]
        return f"Postgres ({config.DB_NAME or 'db'} @ {host}, схема {config.DB_SCHEMA})"
    return f"SQLite ({config.DB_PATH})"
