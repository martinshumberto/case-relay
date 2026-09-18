"""Database access."""

import os

from psycopg_pool import ConnectionPool

# lock_timeout matters because POST /jobs locks the company row: without it one
# stuck request would block every other request for the same tenant.
_SESSION_OPTIONS = "-c statement_timeout=30s -c lock_timeout=10s"

pool = ConnectionPool(
    os.environ["DATABASE_URL"],
    min_size=1,
    max_size=10,
    timeout=10,
    kwargs={"options": _SESSION_OPTIONS},
    open=False,
)


def get_conn():
    return pool.connection()
