"""Job queue worker."""

import os
import pathlib
import signal
import socket
import threading
import time

import psycopg

from shared import obs

log = obs.setup("worker")

POLL_MIN_SECONDS = 0.2
POLL_MAX_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 30

# A fixed lease forces one number to satisfy two opposing needs: long enough to
# outlast the slowest job, short enough that a crashed worker's job returns to
# the queue quickly. LeaseKeeper renews it while the job runs, which decouples
# them, so this is only the window a dead worker leaves a job stranded.
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", "30"))

# A third of the lease, so renewal has to fail twice before the reaper acts.
LEASE_RENEW_SECONDS = max(1, LEASE_SECONDS // 3)

# Retention is configured in days, so sweeping every minute scans the whole
# jobs table to find nothing 60 times an hour, in every worker. Hourly is still
# orders of magnitude finer than the unit being enforced.
PURGE_EVERY_SECONDS = 3600
PURGE_BATCH = 500

# Jobs predating the lease mechanism sit in 'running' with locked_until NULL.
# Without this fallback on updated_at they would be invisible to the reaper.
LEGACY_GRACE = "5 minutes"

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# The worker exposes no HTTP port; liveness is this file's mtime.
HEARTBEAT_PATH = "/tmp/worker-heartbeat"

_shutdown = False


def _request_shutdown(signum, _frame):
    global _shutdown
    _shutdown = True
    log.info("worker.shutdown_requested", extra={"signal": signum})


def _handle_report(job: dict) -> str:
    time.sleep(1)  # simulated work
    return f"resultado sensível da empresa {job['company_id']}"


def _handle_import(job: dict) -> str:
    time.sleep(1)  # simulated work
    return f"importação concluída para a empresa {job['company_id']}"


HANDLERS = {"report": _handle_report, "import": _handle_import}
SUPPORTED_KINDS = list(HANDLERS)


_lease_conn = None
_lease_conn_lock = threading.Lock()


def _lease_connection():
    """Connection reserved for lease renewal.

    Renewal cannot share the worker's connection: that one is inside the job's
    transaction, and a second thread issuing statements on it would interleave
    with the job's own writes. One connection is opened for the process and
    reused, not one per job.
    """
    global _lease_conn
    with _lease_conn_lock:
        if _lease_conn is None or _lease_conn.closed:
            _lease_conn = psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)
        return _lease_conn


class LeaseKeeper:
    """Extend the lease for as long as the job is actually running.

    Without this, LEASE_SECONDS has to be set above the slowest job the system
    will ever run, and every crash then strands a job for that whole window.

    Losing the lease is not an error to retry: it means the job was cancelled,
    reaped or taken by another worker. The `status='running'` guard in
    complete() already blocks the write, so the flag exists to stop the process
    spending the rest of the attempt on work nobody will accept.
    """

    def __init__(self, job: dict):
        self._job = job
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.lost = False

    def __enter__(self) -> "LeaseKeeper":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> bool:
        self._stop.set()
        self._thread.join(timeout=LEASE_RENEW_SECONDS)
        return False

    def _run(self) -> None:
        while not self._stop.wait(LEASE_RENEW_SECONDS):
            try:
                with _lease_connection().cursor() as cur:
                    cur.execute(
                        "UPDATE jobs SET locked_until = now() + make_interval(secs => %s)"
                        " WHERE id=%s AND locked_by=%s AND status='running'",
                        (LEASE_SECONDS, self._job["id"], WORKER_ID),
                    )
                    renewed = cur.rowcount
            except Exception:
                # Keep trying: a transient failure still has two attempts
                # before the lease expires.
                log.exception("lease.renew_failed", extra={"job_id": self._job["id"]})
                continue

            if renewed == 0:
                self.lost = True
                log.warning("lease.lost", extra={"job_id": self._job["id"]})
                return

            log.info("lease.renewed", extra={"job_id": self._job["id"]})


def record_event(cur, job: dict, event: str, *, before=None, after=None, detail=None) -> None:
    """Append to the audit trail using the caller's cursor, so the event and the
    state change commit together or not at all."""
    cur.execute(
        "INSERT INTO job_events (job_id, company_id, event, from_status, to_status,"
        "                        actor, request_id, detail)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (job["id"], job["company_id"], event, before, after,
         f"worker:{WORKER_ID}", job.get("request_id"), detail[:500] if detail else None),
    )


def reap_expired(conn) -> None:
    """Return jobs with an expired lease to the queue.

    Without this a dead worker strands its job in 'running' forever: the claim
    only looks at 'queued', so nothing recovers it and the tenant permanently
    loses a concurrency slot.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='queued', locked_by=NULL, locked_until=NULL "
            " WHERE status='running'"
            "   AND coalesce(locked_until, updated_at + %s::interval) < now()"
            "   AND attempts < max_attempts"
            " RETURNING id, company_id",
            (LEGACY_GRACE,),
        )
        requeued = [(r[0], r[1]) for r in cur.fetchall()]

        cur.execute(
            "UPDATE jobs SET status='failed', failed_at=now(),"
            "       failure_reason='reserva expirada e tentativas esgotadas',"
            "       locked_by=NULL, locked_until=NULL"
            " WHERE status='running'"
            "   AND coalesce(locked_until, updated_at + %s::interval) < now()"
            "   AND attempts >= max_attempts"
            " RETURNING id, company_id",
            (LEGACY_GRACE,),
        )
        exhausted = [(r[0], r[1]) for r in cur.fetchall()]

        for job_id, company_id in requeued + exhausted:
            spent = (job_id, company_id) in exhausted
            cur.execute(
                "INSERT INTO job_events (job_id, company_id, event, from_status, to_status, actor, detail)"
                " VALUES (%s,%s,%s,'running',%s,'reaper',%s)",
                (job_id, company_id,
                 "job.reaped_exhausted" if spent else "job.reaped",
                 "failed" if spent else "queued",
                 "reserva expirada"),
            )
        conn.commit()

    if requeued:
        log.warning("job.reaped", extra={"job_ids": [j for j, _ in requeued]})
    if exhausted:
        log.warning("job.reaped_exhausted", extra={"job_ids": [j for j, _ in exhausted]})


def purge_expired_results(conn) -> int:
    """Drop payloads past their tenant's retention window.

    Clears the payload rather than deleting the row: "never produced a result"
    and "produced one that was purged" are different answers for support.
    Batched to avoid holding a lock on a large table inside a long transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE job_results r"
            "   SET payload = '', purged_at = now()"
            "  FROM jobs j, companies c"
            " WHERE r.job_id = j.id"
            "   AND j.company_id = c.id"
            "   AND r.purged_at IS NULL"
            "   AND j.created_at < now() - make_interval(days => c.result_retention_days)"
            "   AND r.id IN ("
            "       SELECT r2.id FROM job_results r2"
            "        JOIN jobs j2 ON j2.id = r2.job_id"
            "        JOIN companies c2 ON c2.id = j2.company_id"
            "       WHERE r2.purged_at IS NULL"
            "         AND j2.created_at < now() - make_interval(days => c2.result_retention_days)"
            "       LIMIT %s"
            "   )"
            " RETURNING r.id",
            (PURGE_BATCH,),
        )
        purged = len(cur.fetchall())
        conn.commit()
    return purged


def claim_job(conn) -> dict | None:
    """Claim the next job atomically and fairly across tenants.

    FOR UPDATE SKIP LOCKED collapses selection and reservation into one
    statement, so there is no window between reading and marking, and concurrent
    workers skip a locked row instead of contending for it.

    Ordering is not global FIFO: it picks the tenant with the fewest running
    jobs, then that tenant's oldest job. Global FIFO would let one tenant with a
    large backlog monopolise every worker.

    The recursive term is a loose index scan over jobs_queued_by_company_idx,
    walking one index entry per tenant. A plain DISTINCT reads every queued row
    to produce the same list, which makes the cost of each claim grow with the
    size of the backlog: precisely backwards, since the backlog is largest when
    the workers are furthest behind.

    Within a tenant the order is submission time, not id: created_at comes from
    now(), which is transaction start, so a request that began earlier and
    committed later carries a lower timestamp and a higher id.

    The five-candidate window keeps the worker from idling while work exists:
    with LIMIT 1, a single row locked by another worker makes SKIP LOCKED return
    nothing.

    Filtering on SUPPORTED_KINDS keeps a kind this build cannot handle from
    being claimed. That matters during a rolling deploy, and it also stops a row
    that violates jobs_kind_check from wedging the loop: the claim's UPDATE
    would raise on every cycle and no other job would make progress.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='running',"
            "       attempts=attempts+1,"
            "       locked_by=%s,"
            "       locked_until=now() + make_interval(secs => %s)"
            " WHERE id = ("
            "     WITH RECURSIVE tenants AS ("
            "         (SELECT company_id FROM jobs"
            "           WHERE status='queued' AND kind = ANY(%s)"
            "           ORDER BY company_id LIMIT 1)"
            "         UNION ALL"
            "         SELECT (SELECT n.company_id FROM jobs n"
            "                  WHERE n.status='queued' AND n.kind = ANY(%s)"
            "                    AND n.company_id > t.company_id"
            "                  ORDER BY n.company_id LIMIT 1)"
            "           FROM tenants t WHERE t.company_id IS NOT NULL"
            "     )"
            "     SELECT id FROM ("
            "         SELECT j.id FROM jobs j"
            "          WHERE j.status='queued'"
            "            AND j.kind = ANY(%s)"
            "            AND j.company_id = ("
            "                SELECT t.company_id FROM tenants t"
            "                 WHERE t.company_id IS NOT NULL"
            "                 ORDER BY ("
            "                     SELECT count(*) FROM jobs r"
            "                      WHERE r.company_id = t.company_id AND r.status='running'"
            "                 ), t.company_id"
            "                 LIMIT 1"
            "            )"
            "          ORDER BY j.created_at, j.id"
            "          LIMIT 5"
            "          FOR UPDATE SKIP LOCKED"
            "     ) candidates LIMIT 1"
            " )"
            " RETURNING id, company_id, kind, request_id, attempts, max_attempts",
            (WORKER_ID, LEASE_SECONDS, SUPPORTED_KINDS, SUPPORTED_KINDS, SUPPORTED_KINDS),
        )
        row = cur.fetchone()
        if row is None:
            # Close the transaction the UPDATE opened. Without this the
            # connection sits idle in transaction while the worker sleeps,
            # blocking DDL and VACUUM.
            conn.rollback()
            return None

        job_id, company_id, kind, request_id, attempts, max_attempts = row
        record_event(
            cur,
            {"id": job_id, "company_id": company_id, "request_id": request_id},
            "job.claimed", before="queued", after="running",
        )
        conn.commit()
        return {
            "id": job_id,
            "company_id": company_id,
            "kind": kind,
            "request_id": request_id,
            "attempts": attempts,
            "max_attempts": max_attempts,
        }


def do_work(job: dict) -> str:
    handler = HANDLERS.get(job["kind"])
    if handler is None:
        raise ValueError(f"kind não suportado: {job['kind']!r}")
    return handler(job)


def complete(conn, job: dict, payload: str) -> bool:
    """Finish the job. Returns False if another actor already changed its state."""
    with conn.cursor() as cur:
        # companies before jobs, matching POST /jobs. Opposite orders on the same
        # two tables deadlock under load.
        cur.execute("SELECT id FROM companies WHERE id=%s FOR UPDATE", (job["company_id"],))

        cur.execute(
            "UPDATE jobs SET status='done', quota_charged=true,"
            "       locked_by=NULL, locked_until=NULL"
            " WHERE id=%s AND status='running' AND NOT quota_charged"
            " RETURNING id",
            (job["id"],),
        )
        if cur.fetchone() is None:
            conn.rollback()
            return False

        cur.execute(
            "INSERT INTO job_results (job_id, payload) VALUES (%s,%s)"
            " ON CONFLICT (job_id) DO NOTHING",
            (job["id"], payload),
        )
        cur.execute(
            "UPDATE companies SET job_quota = job_quota - 1 WHERE id=%s",
            (job["company_id"],),
        )
        record_event(cur, job, "job.done", before="running", after="done",
                     detail="quota cobrada")
        conn.commit()
        return True


def fail(conn, job: dict, reason: str) -> None:
    conn.rollback()  # discard partial writes from the attempt
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status='failed', failure_reason=%s, failed_at=now(),"
            "       locked_by=NULL, locked_until=NULL"
            " WHERE id=%s AND status='running'",
            (reason[:1000], job["id"]),
        )
        record_event(cur, job, "job.failed", before="running", after="failed",
                     detail=reason)
        conn.commit()


def process_once(conn) -> bool:
    """Process one job. Returns False if the queue was empty."""
    job = claim_job(conn)
    if job is None:
        return False

    obs.set_request_id(job["request_id"] or "")
    ctx = {
        "job_id": job["id"],
        "company_id": job["company_id"],
        "kind": job["kind"],
        "attempt": job["attempts"],
    }
    log.info("job.claimed", extra=ctx)

    try:
        with LeaseKeeper(job) as lease:
            payload = do_work(job)
        if lease.lost:
            log.warning("job.lease_lost", extra=ctx)
        elif complete(conn, job, payload):
            log.info("job.done", extra=ctx)
        else:
            log.info("job.superseded", extra=ctx)
    except Exception as exc:
        log.exception("job.failed", extra={**ctx, "reason": str(exc)})
        fail(conn, job, f"{type(exc).__name__}: {exc}")

    obs.set_request_id("")
    return True


def connect_with_retry():
    delay = 1
    while not _shutdown:
        try:
            return psycopg.connect(os.environ["DATABASE_URL"])
        except psycopg.OperationalError as exc:
            log.warning("db.connect_failed", extra={"retry_in": delay, "reason": str(exc)})
            time.sleep(delay)
            delay = min(delay * 2, MAX_BACKOFF_SECONDS)
    return None


def main() -> None:
    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    conn = connect_with_retry()
    log.info("worker.started", extra={"worker_id": WORKER_ID})
    idle_delay = POLL_MIN_SECONDS
    next_purge = 0.0

    while not _shutdown:
        try:
            reap_expired(conn)

            if time.monotonic() >= next_purge:
                purged = purge_expired_results(conn)
                if purged:
                    log.info("results.purged", extra={"count": purged})
                next_purge = time.monotonic() + PURGE_EVERY_SECONDS

            if process_once(conn):
                idle_delay = POLL_MIN_SECONDS
            else:
                # Sleep in slices so a signal is honoured without waiting out
                # the full interval.
                slept = 0.0
                while slept < idle_delay and not _shutdown:
                    time.sleep(0.1)
                    slept += 0.1
                idle_delay = min(idle_delay * 2, POLL_MAX_SECONDS)

            # Touched only after a full successful cycle. Touching it at the top
            # would report healthy while the loop failed on every iteration.
            pathlib.Path(HEARTBEAT_PATH).touch()
        except psycopg.OperationalError:
            log.exception("db.connection_lost")
            try:
                conn.close()
            except Exception:
                pass
            conn = connect_with_retry()
            if conn is None:
                break
        except Exception:
            # Infrastructure failure, not a job failure, which process_once
            # already handled. The loop must survive, or the in-flight job stays
            # orphaned until its lease expires.
            #
            # The rollback is what makes surviving useful: a failed statement
            # leaves the transaction aborted, and without resetting it every
            # later command raises InFailedSqlTransaction and the worker spins
            # forever while still looking alive.
            log.exception("worker.loop_error")
            try:
                conn.rollback()
            except Exception:
                log.exception("worker.rollback_failed")
            time.sleep(POLL_MAX_SECONDS)

    if conn is not None:
        conn.close()
    log.info("worker.stopped", extra={"worker_id": WORKER_ID})


if __name__ == "__main__":
    main()
