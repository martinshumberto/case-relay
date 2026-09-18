import base64
import binascii
import os
import threading
import time
from datetime import datetime
from collections import defaultdict, deque
from typing import Literal

import psycopg

from fastapi import FastAPI, Depends, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager

from auth import current_ctx, require_admin
from db import get_conn, pool
from shared import obs

log = obs.setup("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    pool.open()
    pool.wait(timeout=30)
    log.info("api.started")
    yield
    pool.close()


app = FastAPI(title="Relay", lifespan=lifespan)

CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "http://localhost:5173").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
    # Lets the frontend read the request_id and surface it to the user.
    expose_headers=["X-Request-Id"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Correlate the request with its processing in the worker.

    Accepts an inbound X-Request-Id so the chain holds when the call comes from
    another service. The value goes into the ContextVar (and therefore every log
    line), is echoed back in the header, and is stored on jobs.request_id for the
    worker to read.
    """
    rid = request.headers.get("x-request-id") or obs.new_request_id()
    obs.set_request_id(rid)
    response = await call_next(request)
    response.headers["X-Request-Id"] = rid
    return response


@app.get("/health")
def health():
    """Liveness and readiness: checks the database, not just that the process
    answers."""
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
    except Exception:
        log.exception("health.db_unavailable")
        raise HTTPException(503, "db unavailable")
    return {"status": "ok"}


# In-memory sliding window, per tenant and route. State is per process, so with
# several replicas the effective limit is N x RATE_LIMIT. Covers accidental
# abuse such as a retry loop; a determined attacker would need shared state.
RATE_LIMIT = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "300"))

# The check runs before the tenant is known to exist, so a caller sending
# arbitrary company ids would otherwise create a permanent entry per id.
RATE_LIMIT_MAX_KEYS = 10_000

_hits: defaultdict[str, deque[float]] = defaultdict(deque)
_hits_lock = threading.Lock()


def _drop_stale_windows(now: float) -> None:
    for key in [k for k, window in _hits.items() if not window or now - window[-1] > 60]:
        del _hits[key]


def rate_limited(key: str) -> bool:
    now = time.monotonic()
    with _hits_lock:
        if len(_hits) > RATE_LIMIT_MAX_KEYS:
            _drop_stale_windows(now)
        window = _hits[key]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= RATE_LIMIT:
            return True
        window.append(now)
        return False


DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_IDEMPOTENCY_KEY = 255


def _encode_cursor(created_at, job_id: int) -> str:
    """Opaque cursor.

    base64url for two reasons: the timestamp contains '+', which means space in a
    query string and arrived corrupted; and opacity stops clients from depending
    on the format, which is an implementation detail of the ordering.
    """
    bruto = f"{created_at.isoformat()}|{job_id}".encode()
    return base64.urlsafe_b64encode(bruto).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple:
    if not cursor:
        return None, None
    try:
        padding = "=" * (-len(cursor) % 4)
        created_at, _, job_id = base64.urlsafe_b64decode(cursor + padding).decode().rpartition("|")
        # Validated here rather than letting the Postgres cast fail: a malformed
        # cursor is a client error (400), not a server error (500).
        datetime.fromisoformat(created_at)
        return created_at, int(job_id)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise HTTPException(400, "cursor inválido")


# Prometheus text format, emitted by hand: queue depth and quota are database
# facts, not process counters, and a client library would aggregate per replica
# and report wrong numbers with more than one instance.
#
# Cardinality is the design constraint: a job_id label would create one series
# per job. company_id is acceptable, with an explicit ceiling.
METRICS_TOKEN = os.environ.get("METRICS_TOKEN", "")
METRICS_MAX_TENANTS = int(os.environ.get("METRICS_MAX_TENANTS", "100"))
METRICS_FAILURE_WINDOW_MINUTES = int(os.environ.get("METRICS_FAILURE_WINDOW_MINUTES", "60"))


@app.get("/metrics")
def metrics(authorization: str | None = Header(default=None)):
    """Prometheus metrics.

    Token-gated and disabled by default: per-company queue depth and quota are
    business data, and exposing them unauthenticated would be a cross-tenant leak
    through the back door.
    """
    if not METRICS_TOKEN:
        raise HTTPException(404, "métricas desabilitadas")
    if authorization != f"Bearer {METRICS_TOKEN}":
        raise HTTPException(401, "token de métricas inválido")

    # Restricted to queued and running, which is what a gauge can express and
    # what jobs_active_by_company_idx covers index-only. Counting every status
    # meant aggregating the whole table on each scrape, so the cost of observing
    # the system grew with its history and never came down.
    lines = [
        "# HELP relay_jobs_active Jobs awaiting or under processing, by company.",
        "# TYPE relay_jobs_active gauge",
    ]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT company_id, status, count(*) FROM jobs"
            " WHERE status IN ('queued','running')"
            "   AND company_id IN (SELECT id FROM companies ORDER BY id LIMIT %s)"
            " GROUP BY company_id, status ORDER BY company_id, status",
            (METRICS_MAX_TENANTS,),
        )
        for company_id, status, total in cur.fetchall():
            lines.append(f'relay_jobs_active{{company="{company_id}",status="{status}"}} {total}')

        lines += [
            "# HELP relay_queue_oldest_seconds Age of the oldest queued job.",
            "# TYPE relay_queue_oldest_seconds gauge",
        ]
        # One index lookup per tenant instead of GROUP BY over every queued row:
        # Postgres has no loose index scan for a grouped min(), so the aggregate
        # form falls back to a sequential scan of the whole table.
        cur.execute(
            "SELECT c.id, extract(epoch FROM now() - ("
            "         SELECT min(j.created_at) FROM jobs j"
            "          WHERE j.company_id=c.id AND j.status='queued'))"
            "  FROM (SELECT id FROM companies ORDER BY id LIMIT %s) c",
            (METRICS_MAX_TENANTS,),
        )
        for company_id, age in cur.fetchall():
            if age is not None:
                lines.append(f'relay_queue_oldest_seconds{{company="{company_id}"}} {age:.1f}')

        lines += [
            "# HELP relay_quota_remaining Remaining quota per company.",
            "# TYPE relay_quota_remaining gauge",
        ]
        cur.execute("SELECT id, job_quota FROM companies ORDER BY id LIMIT %s", (METRICS_MAX_TENANTS,))
        for company_id, quota in cur.fetchall():
            lines.append(f'relay_quota_remaining{{company="{company_id}"}} {quota}')

        # Grouped by error class: the message carries ids and values, so each
        # failure would create a new series.
        #
        # Windowed, and not only to bound the cost: counting every failure ever
        # produces a number that keeps climbing after the incident is over, so
        # it cannot be alerted on. The window is what makes it a rate.
        lines += [
            f"# HELP relay_job_failures_recent Failures in the last"
            f" {METRICS_FAILURE_WINDOW_MINUTES} minutes, by error class.",
            "# TYPE relay_job_failures_recent gauge",
        ]
        cur.execute(
            "SELECT coalesce(split_part(failure_reason, ':', 1), 'desconhecido'), count(*)"
            "  FROM jobs WHERE status='failed'"
            "   AND failed_at > now() - make_interval(mins => %s)"
            " GROUP BY 1 ORDER BY 2 DESC LIMIT 20",
            (METRICS_FAILURE_WINDOW_MINUTES,),
        )
        for error_class, total in cur.fetchall():
            safe_label = error_class.replace('"', "").replace("\\", "")[:60]
            lines.append(f'relay_job_failures_recent{{error_class="{safe_label}"}} {total}')

    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/jobs")
def list_jobs(limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None, ctx=Depends(current_ctx)):
    """List the tenant's jobs, paginated.

    UNIQUE(job_id) guarantees at most one result per job, so the LEFT JOIN needs
    no aggregation.

    Keyset rather than OFFSET: ordering is created_at DESC and new jobs arrive at
    the top constantly, so OFFSET would show repeated or skipped items between
    pages.
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    cursor_ts, cursor_id = _decode_cursor(cursor)

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT j.id, j.kind, j.status, j.created_at, j.attempts, j.max_attempts,"
            "       j.failure_reason, (r.job_id IS NOT NULL)::int AS result_count"
            "  FROM jobs j"
            "  LEFT JOIN job_results r ON r.job_id = j.id"
            " WHERE j.company_id = %s"
            "   AND (%s::timestamptz IS NULL"
            "        OR (j.created_at, j.id) < (%s::timestamptz, %s::bigint))"
            " ORDER BY j.created_at DESC, j.id DESC"
            " LIMIT %s",
            (ctx["company_id"], cursor_ts, cursor_ts, cursor_id, limit),
        )
        rows = cur.fetchall()

    items = [
        {
            "id": r[0],
            "kind": r[1],
            "status": r[2],
            "created_at": r[3].isoformat(),
            "attempts": r[4],
            "max_attempts": r[5],
            "failure_reason": r[6],
            "result_count": r[7],
        }
        for r in rows
    ]
    next_cursor = _encode_cursor(rows[-1][3], rows[-1][0]) if len(rows) == limit else None
    return {"items": items, "next_cursor": next_cursor}

def record_event(cur, job_id: int, company_id: int, event: str,
                 *, before=None, after=None, detail=None) -> None:
    """Append to the audit trail in the same transaction as the state change: a
    trail that can diverge from reality is worse than no trail."""
    cur.execute(
        "INSERT INTO job_events (job_id, company_id, event, from_status, to_status,"
        "                        actor, request_id, detail)"
        " VALUES (%s,%s,%s,%s,%s,'api',%s,%s)",
        (job_id, company_id, event, before, after, obs.get_request_id(),
         detail[:500] if detail else None),
    )


def job_for_tenant(cur, job_id: int, company_id: int) -> tuple:
    """Resolve a job within the tenant boundary.

    Single resolution point: scattering `AND company_id=%s` across routes only
    needs to be forgotten once to reopen the leak.

    404 rather than 403 for another tenant's job: 403 would confirm the resource
    exists, and with sequential ids that allows enumerating the database.
    """
    cur.execute(
        "SELECT id, company_id, kind, status FROM jobs WHERE id=%s AND company_id=%s",
        (job_id, company_id),
    )
    row = cur.fetchone()
    if row is None:
        raise HTTPException(404, "job não encontrado")
    return row


@app.get("/jobs/{job_id}")
def get_job(job_id: int, ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        row = job_for_tenant(cur, job_id, ctx["company_id"])
        return {"id": row[0], "company_id": row[1], "kind": row[2], "status": row[3]}


@app.get("/jobs/{job_id}/result")
def get_result(job_id: int, ctx=Depends(current_ctx)):
    with get_conn() as conn, conn.cursor() as cur:
        # job_results has no company_id, so isolation requires the JOIN.
        _, _, _, status = job_for_tenant(cur, job_id, ctx["company_id"])

        cur.execute(
            "SELECT r.payload, r.purged_at FROM job_results r"
            "  JOIN jobs j ON j.id = r.job_id"
            " WHERE r.job_id=%s AND j.company_id=%s",
            (job_id, ctx["company_id"]),
        )
        row = cur.fetchone()
        if row is not None and row[1] is not None:
            # 410 signals the resource existed, unlike "never produced one".
            raise HTTPException(
                410, f"resultado expurgado por retenção em {row[1].date().isoformat()}"
            )
        if row is None:
            raise HTTPException(409, f"job ainda sem resultado (status={status})")
        return {"payload": row[0]}

class NewJob(BaseModel):
    # Two layers: 422 here, and a CHECK constraint for every other write path.
    kind: Literal["report", "import"]


@app.post("/jobs")
def create_job(
    body: NewJob,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ctx=Depends(current_ctx),
):
    if idempotency_key and len(idempotency_key) > MAX_IDEMPOTENCY_KEY:
        raise HTTPException(422, f"Idempotency-Key acima de {MAX_IDEMPOTENCY_KEY} caracteres")

    if rate_limited(f"{ctx['company_id']}:POST /jobs"):
        # Same status as the concurrency limit but a different meaning: the
        # message and Retry-After tell the client which one to address.
        raise HTTPException(
            429,
            "taxa de requisições excedida, reduza a cadência",
            headers={"Retry-After": "60"},
        )

    with get_conn() as conn, conn.cursor() as cur:
        # Optional key: without the header, behaviour is unchanged.
        if idempotency_key:
            cur.execute(
                "SELECT id, status FROM jobs WHERE company_id=%s AND idempotency_key=%s",
                (ctx["company_id"], idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                # Return the original rather than creating another or failing:
                # that is what a client retrying after a timeout expects.
                log.info("job.idempotent_replay",
                         extra={"job_id": existing[0], "company_id": ctx["company_id"]})
                return {"id": existing[0], "status": existing[1], "replayed": True}
        # SELECT count + INSERT without a lock is check-then-act: under READ
        # COMMITTED the count blocks nothing and concurrent requests all pass.
        # The row lock serialises admission per tenant, which is the rule's own
        # granularity since max_concurrent_jobs and job_quota live on this row.
        #
        # companies before jobs, matching the worker: opposite orders on the same
        # two tables deadlock under load.
        cur.execute(
            "SELECT max_concurrent_jobs, job_quota FROM companies WHERE id=%s FOR UPDATE",
            (ctx["company_id"],),
        )
        company = cur.fetchone()
        if company is None:
            raise HTTPException(401, "empresa inválida")
        limit, quota = company

        cur.execute(
            "SELECT count(*) FROM jobs WHERE company_id=%s AND status IN ('queued','running')",
            (ctx["company_id"],),
        )
        running = cur.fetchone()[0]
        if running >= limit:
            log.info("job.rejected", extra={"company_id": ctx["company_id"], "reason": "limite_concorrencia", "limite": limit})
            raise HTTPException(429, "limite de jobs concorrentes atingido")

        # Checked at admission: otherwise quota exhaustion would only surface at
        # completion, as a failed job instead of a rejected submission.
        if quota <= 0:
            log.info("job.rejected", extra={"company_id": ctx["company_id"], "reason": "quota_esgotada"})
            raise HTTPException(429, "cota de jobs esgotada")

        try:
            cur.execute(
                "INSERT INTO jobs (company_id, kind, status, request_id, idempotency_key)"
                " VALUES (%s,%s,'queued',%s,%s) RETURNING id",
                (ctx["company_id"], body.kind, obs.get_request_id(), idempotency_key),
            )
            job_id = cur.fetchone()[0]
            conn.commit()
        except psycopg.errors.UniqueViolation:
            # The lookup above runs before the company lock, so two concurrent
            # requests carrying the same key can both find nothing. The unique
            # index is the arbiter: whoever lost replays the winner's job rather
            # than failing, which is the whole point of the header.
            conn.rollback()
            cur.execute(
                "SELECT id, status FROM jobs WHERE company_id=%s AND idempotency_key=%s",
                (ctx["company_id"], idempotency_key),
            )
            winner = cur.fetchone()
            if winner is None:
                raise
            log.info("job.idempotent_replay",
                     extra={"job_id": winner[0], "company_id": ctx["company_id"], "raced": True})
            return {"id": winner[0], "status": winner[1], "replayed": True}
        log.info(
            "job.created",
            extra={"job_id": job_id, "company_id": ctx["company_id"], "kind": body.kind},
        )
        return {"id": job_id, "status": "queued"}

@app.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, ctx=Depends(current_ctx)):
    """Cancel a queued or running job.

    Conditional UPDATE with no prior SELECT: checking and then deciding would be
    check-then-act. Here both are the same statement, and the tenant filter sits
    inside the write condition.

    Racing the worker: both UPDATE the same row, so Postgres serialises them.
    Whoever commits first wins; the losing worker finds out via the
    `status='running'` guard and rolls everything back.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='cancelled', cancelled_at=now(),"
            "       locked_by=NULL, locked_until=NULL"
            " WHERE id=%s AND company_id=%s AND status IN ('queued','running')"
            " RETURNING id, status",
            (job_id, ctx["company_id"]),
        )
        row = cur.fetchone()
        if row is None:
            # 404 if absent or another tenant's; if it exists, the reason is the
            # state, which is 409.
            current = job_for_tenant(cur, job_id, ctx["company_id"])
            conn.rollback()
            raise HTTPException(409, f"job não pode ser cancelado (status={current[3]})")

        record_event(cur, row[0], ctx["company_id"], "job.cancelled",
                     after="cancelled", detail="solicitado pelo usuário")
        conn.commit()
        log.info("job.cancelled", extra={"job_id": row[0], "company_id": ctx["company_id"]})
        return {"id": row[0], "status": row[1]}


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, ctx=Depends(current_ctx)):
    """Retry a failed job, idempotently.

    The state machine is the lock: a second click finds 'queued', the condition
    fails and the answer is 409, with no idempotency key or auxiliary table. The
    other two layers are structural: UNIQUE(job_id) guarantees a single result
    and quota_charged a single charge, so N executions produce one of each.

    attempts is incremented by the worker on claim, not here, so the counter
    reflects real processing attempts.
    """
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='queued', failure_reason=NULL, failed_at=NULL,"
            "       locked_by=NULL, locked_until=NULL"
            " WHERE id=%s AND company_id=%s"
            "   AND status='failed' AND attempts < max_attempts"
            " RETURNING id, status, attempts, max_attempts",
            (job_id, ctx["company_id"]),
        )
        row = cur.fetchone()
        if row is None:
            job_for_tenant(cur, job_id, ctx["company_id"])  # 404 se não é seu
            cur.execute(
                "SELECT status, attempts, max_attempts FROM jobs WHERE id=%s AND company_id=%s",
                (job_id, ctx["company_id"]),
            )
            status, attempts, max_attempts = cur.fetchone()
            conn.rollback()
            if status == "failed" and attempts >= max_attempts:
                raise HTTPException(
                    409, f"máximo de tentativas atingido ({attempts}/{max_attempts})"
                )
            raise HTTPException(409, f"job não pode ser reprocessado (status={status})")

        record_event(cur, row[0], ctx["company_id"], "job.retried",
                     before="failed", after="queued", detail=f"tentativa {row[2]}")
        conn.commit()
        log.info("job.retried", extra={"job_id": row[0], "company_id": ctx["company_id"], "attempts": row[2]})
        return {"id": row[0], "status": row[1], "attempts": row[2], "max_attempts": row[3]}


@app.get("/jobs/{job_id}/events")
def job_events(job_id: int, ctx=Depends(current_ctx)):
    """Job timeline.

    jobs.status holds only the current state; the trail answers what happened:
    how many attempts, who cancelled, why it failed. Logs rotate, the job stays.
    """
    with get_conn() as conn, conn.cursor() as cur:
        job_for_tenant(cur, job_id, ctx["company_id"])  # 404 se não é seu
        # company_id is redundant after job_for_tenant, but the column exists and
        # a divergent event written by another path would otherwise be served.
        cur.execute(
            "SELECT event, from_status, to_status, actor, request_id, detail, created_at"
            "  FROM job_events WHERE job_id=%s AND company_id=%s ORDER BY id",
            (job_id, ctx["company_id"]),
        )
        return {
            "job_id": job_id,
            "events": [
                {"event": r[0], "from": r[1], "to": r[2], "actor": r[3],
                 "request_id": r[4], "detail": r[5], "at": r[6].isoformat()}
                for r in cur.fetchall()
            ],
        }


@app.get("/admin/jobs")
def admin_jobs(limit: int = DEFAULT_PAGE_SIZE, cursor: int | None = None, ctx=Depends(require_admin)):
    """Administrative view, scoped to the caller's own tenant.

    Deliberate divergence from the README, which describes "all companies": the
    model only has `role` within a company, so no platform-wide role exists. See
    DECISIONS.md.
    """
    limit = max(1, min(limit, MAX_PAGE_SIZE))
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, company_id, kind, status FROM jobs"
            " WHERE company_id=%s AND (%s::bigint IS NULL OR id < %s::bigint)"
            " ORDER BY id DESC LIMIT %s",
            (ctx["company_id"], cursor, cursor, limit),
        )
        rows = cur.fetchall()
    items = [{"id": r[0], "company_id": r[1], "kind": r[2], "status": r[3]} for r in rows]
    return {"items": items, "next_cursor": str(rows[-1][0]) if len(rows) == limit else None}
