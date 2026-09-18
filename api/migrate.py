"""Apply versioned SQL migrations.

/docker-entrypoint-initdb.d only runs against an empty data dir, so changes to
db/schema.sql are silently ignored on an existing database. This runner is the
schema evolution path: it runs at startup, in any database state.

Each file is applied once, in lexicographic order, inside its own transaction,
and recorded in schema_migrations.

Files named *.concurrent.sql are the exception: CREATE INDEX CONCURRENTLY
cannot run inside a transaction, and a plain CREATE INDEX holds a lock that
blocks writes for the whole build, which on a table of millions of rows means
a write outage. Those files run in autocommit, one statement at a time, so
they must contain only statements without an embedded semicolon.
"""

import os
import pathlib
import sys

import psycopg

MIGRATIONS_DIR = pathlib.Path(os.environ.get("MIGRATIONS_DIR", "/migrations"))

# Prevents replicas starting in parallel from applying the same migration.
# Session-scoped advisory lock, released when the connection closes.
LOCK_KEY = 8472315

CONCURRENT_SUFFIX = ".concurrent.sql"


def _apply(conn, cur, path: pathlib.Path) -> None:
    sql = path.read_text()
    if not path.name.endswith(CONCURRENT_SUFFIX):
        cur.execute(sql)
        return

    conn.commit()
    conn.autocommit = True
    try:
        # A concurrent build scans the table twice and can outlast the global
        # statement_timeout, and it takes no lock that would justify killing it.
        cur.execute("SET statement_timeout = 0")
        for statement in (s.strip() for s in sql.split(";")):
            if statement:
                cur.execute(statement)
    finally:
        cur.execute("SET statement_timeout = '120s'")
        conn.autocommit = False


def run() -> None:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        # ALTER TABLE needs ACCESS EXCLUSIVE. A connection left idle in
        # transaction blocks the migration indefinitely with no diagnostic;
        # failing loudly after 10s beats hanging the deploy.
        cur.execute("SET lock_timeout = '10s'")
        cur.execute("SET statement_timeout = '120s'")

        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version TEXT PRIMARY KEY,"
            "  applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        conn.commit()

        cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        conn.commit()

        cur.execute("SELECT version FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}

        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        if not files:
            print(f"migrate: nenhuma migration em {MIGRATIONS_DIR}", file=sys.stderr)

        for path in files:
            version = path.name
            if version in applied:
                print(f"migrate: {version} ja aplicada")
                continue
            print(f"migrate: aplicando {version}")
            _apply(conn, cur, path)
            cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            conn.commit()
            print(f"migrate: {version} ok")

        print("migrate: concluido")


if __name__ == "__main__":
    run()
