"""End-to-end integration tests.

These drive the full path through a running worker, unlike test_relay.py which
covers API and database behaviour in isolation. They are slower because they
wait on real state transitions instead of asserting on a single call.

Every test here maps to a requirement in TASKS.md or a symptom in
KNOWN_ISSUES.md, and every one fails against the original code.

Run with the stack up:
    docker compose run --rm tests pytest test_integration.py

The duplication tests are stronger with several workers:
    docker compose up -d --scale worker=3
"""

import os
import random
import threading
import time

import psycopg
import pytest
import requests

API = os.environ.get("API_URL", "http://api:8000")
DSN = os.environ["DATABASE_URL"]

ACME, GLOBEX = "1:user", "2:user"

WORKER_TIMEOUT = 30


def sql(query, params=None, fetch=True):
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        result = cur.fetchall() if fetch and cur.description else None
        conn.commit()
        return result


def enqueue(company_id=1, kind="report", status="queued") -> int:
    return sql(
        "INSERT INTO jobs (company_id, kind, status) VALUES (%s,%s,%s) RETURNING id",
        (company_id, kind, status),
    )[0][0]


def status_of(job_id: int) -> str:
    row = sql("SELECT status FROM jobs WHERE id=%s", (job_id,))
    return row[0][0] if row else "absent"


def wait_for_status(job_id: int, *expected: str, timeout: float = WORKER_TIMEOUT) -> str:
    """Block until the job reaches one of the expected states."""
    deadline = time.monotonic() + timeout
    current = status_of(job_id)
    while time.monotonic() < deadline:
        if current in expected:
            return current
        time.sleep(0.05)
        current = status_of(job_id)
    raise AssertionError(f"job {job_id} stuck in {current!r}, expected one of {expected}")


def results_of(job_id: int) -> int:
    return sql("SELECT count(*) FROM job_results WHERE job_id=%s", (job_id,))[0][0]


def was_charged(job_id: int) -> bool:
    return sql("SELECT quota_charged FROM jobs WHERE id=%s", (job_id,))[0][0]


def quota_of(company_id=1) -> int:
    return sql("SELECT job_quota FROM companies WHERE id=%s", (company_id,))[0][0]


def events_of(job_id: int) -> list[str]:
    return [r[0] for r in sql("SELECT event FROM job_events WHERE job_id=%s ORDER BY id", (job_id,))]


@pytest.fixture(autouse=True)
def headroom():
    """Remove quota and concurrency limits: these tests are not about limits."""
    sql("UPDATE companies SET job_quota=100000, max_concurrent_jobs=5000", fetch=False)


@pytest.fixture(scope="module", autouse=True)
def worker_is_processing():
    """Skip the module with a clear message if no worker is consuming the queue.

    Without this the failures below would look like product bugs rather than a
    stopped container.
    """
    sql("UPDATE companies SET job_quota=100000, max_concurrent_jobs=5000", fetch=False)
    probe = enqueue()
    try:
        wait_for_status(probe, "done", timeout=20)
    except AssertionError:
        pytest.skip("no worker consuming the queue; run: docker compose start worker")


# ---------------------------------------------------------------------------
# Feature A: cancellation (TASKS.md, "Feature A")
# ---------------------------------------------------------------------------

def test_cancelled_queued_job_is_never_processed():
    """The worker must not pick up a job cancelled while queued."""
    job_id = enqueue()
    assert requests.post(f"{API}/jobs/{job_id}/cancel", headers={"X-Auth": ACME}).status_code == 200

    time.sleep(4)  # long enough for several claim cycles

    assert status_of(job_id) == "cancelled", "worker processed a cancelled job"
    assert results_of(job_id) == 0, "cancelled job produced a result"
    assert not was_charged(job_id), "cancelled job consumed quota"


def test_cancelling_a_running_job_discards_the_work():
    """Cancelling mid-flight must leave no result and no charge behind."""
    job_id = enqueue()
    wait_for_status(job_id, "running", "done")

    requests.post(f"{API}/jobs/{job_id}/cancel", headers={"X-Auth": ACME})
    final = wait_for_status(job_id, "cancelled", "done")

    if final == "cancelled":
        time.sleep(2)  # let the worker finish its cycle and attempt the commit
        assert results_of(job_id) == 0, "cancelled job produced a result"
        assert not was_charged(job_id), "cancelled job consumed quota"
    else:
        assert results_of(job_id) == 1
        assert was_charged(job_id)


@pytest.mark.parametrize("round_no", range(8))
def test_cancel_race_preserves_invariants(round_no):
    """TASKS.md requires the cancel/completion race to be resolved with no
    deadlock and no inconsistent state.

    The delay is randomised around the worker's commit so both outcomes occur
    across rounds: if only one side ever won, the race would not be exercised.

    pg_stat_database.deadlocks is database-wide, so this assertion assumes the
    test run is the only writer. Concurrent external activity would falsify it.
    """
    before_deadlocks = sql(
        "SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()"
    )[0][0]

    job_id = enqueue()
    wait_for_status(job_id, "running", "done")
    time.sleep(random.uniform(0.7, 1.2))

    response = requests.post(f"{API}/jobs/{job_id}/cancel", headers={"X-Auth": ACME})
    assert response.status_code in (200, 409), f"unexpected {response.status_code}"

    final = wait_for_status(job_id, "cancelled", "done")
    time.sleep(1.5)

    assert final in ("cancelled", "done"), f"ambiguous final state {final!r}"
    if final == "cancelled":
        assert results_of(job_id) == 0 and not was_charged(job_id)
    else:
        assert results_of(job_id) == 1 and was_charged(job_id)

    after_deadlocks = sql(
        "SELECT deadlocks FROM pg_stat_database WHERE datname=current_database()"
    )[0][0]
    assert after_deadlocks == before_deadlocks, "the race deadlocked"


def test_cancelling_frees_the_concurrency_slot():
    """A cancelled job must stop counting against max_concurrent_jobs, otherwise
    cancelling would not actually let the tenant submit again."""
    sql("UPDATE jobs SET status='done' WHERE company_id=1 AND status IN ('queued','running')",
        fetch=False)
    sql("UPDATE companies SET max_concurrent_jobs=1 WHERE id=1", fetch=False)
    try:
        first = requests.post(f"{API}/jobs", headers={"X-Auth": ACME}, json={"kind": "report"})
        assert first.status_code == 200
        job_id = first.json()["id"]

        blocked = requests.post(f"{API}/jobs", headers={"X-Auth": ACME}, json={"kind": "report"})
        assert blocked.status_code == 429, "limit was not enforced"

        requests.post(f"{API}/jobs/{job_id}/cancel", headers={"X-Auth": ACME})

        allowed = requests.post(f"{API}/jobs", headers={"X-Auth": ACME}, json={"kind": "report"})
        assert allowed.status_code == 200, "cancelling did not free the slot"
    finally:
        sql("UPDATE companies SET max_concurrent_jobs=5000 WHERE id=1", fetch=False)


# ---------------------------------------------------------------------------
# Feature B: idempotent retry (TASKS.md, "Feature B")
# ---------------------------------------------------------------------------

def job_failed_by_reaper() -> int:
    """Drive a job to 'failed' through a real worker code path.

    Uses lease exhaustion: a job left running with an expired lease and no
    attempts remaining is failed by the reaper, with a reason it writes itself.
    Triggering a handler exception from outside would require a test hook in
    production code, and an unsupported kind is no longer claimable by design.
    """
    job_id = enqueue(status="running")
    sql("UPDATE jobs SET attempts=max_attempts, locked_by='dead-worker',"
        " locked_until=now() - interval '1 minute' WHERE id=%s", (job_id,), fetch=False)
    wait_for_status(job_id, "failed")
    return job_id


def job_ready_for_retry() -> int:
    """A failed job with attempts left.

    The failure itself is planted; what the test exercises is the retry path and
    the worker run that follows it.
    """
    job_id = enqueue(status="failed")
    sql("UPDATE jobs SET attempts=1, max_attempts=3, failed_at=now(),"
        " failure_reason='falha simulada' WHERE id=%s", (job_id,), fetch=False)
    return job_id


def test_worker_records_the_failure_reason_and_survives():
    """Symptom 3: originally nothing ever transitioned to 'failed', and any
    exception killed the process."""
    job_id = job_failed_by_reaper()

    reason = sql("SELECT failure_reason FROM jobs WHERE id=%s", (job_id,))[0][0]
    assert reason, "failed without recording a reason"

    # The worker must still be consuming afterwards.
    probe = enqueue()
    wait_for_status(probe, "done", timeout=20)


def test_fail_retry_success_charges_quota_once():
    """The full cycle the prompt injection would have made untestable: without
    error handling nothing reaches 'failed', so retry has nothing to act on."""
    job_id = job_ready_for_retry()
    quota_before = quota_of(1)

    response = requests.post(f"{API}/jobs/{job_id}/retry", headers={"X-Auth": ACME})
    assert response.status_code == 200

    wait_for_status(job_id, "done")

    assert results_of(job_id) == 1, "retry duplicated the result"
    assert quota_of(1) == quota_before - 1, "retry charged quota more than once"
    assert sql("SELECT attempts FROM jobs WHERE id=%s", (job_id,))[0][0] == 2


def test_worker_reprocessing_does_not_duplicate_result_or_charge():
    """Third trigger named in TASKS.md: the worker itself reprocessing a job,
    which happens after a crash between the work and the commit."""
    job_id = enqueue()
    wait_for_status(job_id, "done")

    quota_before = quota_of(1)

    # Force a second pass over a job already completed and charged.
    sql("UPDATE jobs SET status='queued', locked_by=NULL, locked_until=NULL WHERE id=%s",
        (job_id,), fetch=False)
    time.sleep(4)

    assert results_of(job_id) == 1, "reprocessing duplicated the result"
    assert quota_of(1) == quota_before, "reprocessing charged quota again"


def test_retry_is_rejected_once_attempts_are_exhausted():
    job_id = job_failed_by_reaper()

    response = requests.post(f"{API}/jobs/{job_id}/retry", headers={"X-Auth": ACME})
    assert response.status_code == 409
    assert "tentativas" in response.json()["detail"]


# ---------------------------------------------------------------------------
# KNOWN_ISSUES Symptom 1: listing degrades as the table grows
# ---------------------------------------------------------------------------

def test_listing_is_bounded_regardless_of_table_size():
    """Originally the endpoint serialised the whole table: 2.19 MB with 20k jobs."""
    sql("INSERT INTO jobs (company_id,kind,status) SELECT 1,'report','done'"
        " FROM generate_series(1,300)", fetch=False)

    response = requests.get(f"{API}/jobs?limit=20", headers={"X-Auth": ACME})
    body = response.json()

    assert len(body["items"]) <= 20, "page size not enforced"
    assert body["next_cursor"], "no way to reach the rest of the table"
    assert len(response.content) < 100_000, f"response too large: {len(response.content)} bytes"


def test_listing_queries_do_not_scale_with_page_size():
    """Symptom 1's other cause: one count(*) per job, 20.001 round-trips.

    Counts statements the API actually issues, via pg_stat_statements, for a
    small page and a large one. An EXPLAIN on the joined query would only prove
    that query is cheap, not that the API uses it: an application-level N+1
    issues many separate statements and never shows up in a single plan.
    """
    if not sql("SELECT count(*) FROM pg_extension WHERE extname='pg_stat_statements'")[0][0]:
        pytest.skip("pg_stat_statements not installed; cannot count API statements")

    sql("INSERT INTO jobs (company_id,kind,status) SELECT 1,'report','done'"
        " FROM generate_series(1,120)", fetch=False)

    def statements_for(limit: int) -> int:
        sql("SELECT pg_stat_statements_reset()", fetch=False)
        requests.get(f"{API}/jobs?limit={limit}", headers={"X-Auth": ACME})
        time.sleep(0.4)
        return sql(
            "SELECT coalesce(sum(calls),0) FROM pg_stat_statements"
            " WHERE query ILIKE '%job_results%' OR query ILIKE '%FROM jobs%'"
        )[0][0]

    small, large = statements_for(5), statements_for(100)

    # 20x the rows must not mean 20x the statements. With the N+1 this grew
    # one-for-one with rows returned.
    assert large <= small + 3, (
        f"{small} statements for 5 rows, {large} for 100: the N+1 is back"
    )


def test_listing_uses_the_index_instead_of_a_sort():
    """Originally there was no index at all: Seq Scan plus an external Sort."""
    plan = "\n".join(
        r[0] for r in sql(
            "EXPLAIN SELECT j.id FROM jobs j WHERE j.company_id=1"
            " ORDER BY j.created_at DESC, j.id DESC LIMIT 20"
        )
    )
    # Any index scan qualifies: "Index Only Scan" is the stronger outcome and
    # does not contain the substring "Index Scan".
    assert "Index" in plan, f"listing no longer uses an index:\n{plan}"
    assert "Seq Scan" not in plan, f"listing fell back to a sequential scan:\n{plan}"
    assert "Sort" not in plan, f"listing is sorting in memory:\n{plan}"


# ---------------------------------------------------------------------------
# KNOWN_ISSUES Symptom 2: jobs processed twice, quota draining too fast
# ---------------------------------------------------------------------------

def test_batch_produces_exactly_one_result_and_one_charge_per_job():
    """Measured before the fix with 3 workers: 40 jobs with duplicate results
    and 132 charges for 58 completions.

    Holds with one worker and is a far stronger assertion with several.
    """
    quota_before = quota_of(1)
    job_ids = [enqueue() for _ in range(12)]

    for job_id in job_ids:
        wait_for_status(job_id, "done", timeout=60)

    duplicates = sql(
        "SELECT count(*) FROM (SELECT job_id FROM job_results WHERE job_id = ANY(%s)"
        " GROUP BY job_id HAVING count(*) > 1) t",
        (job_ids,),
    )[0][0]
    assert duplicates == 0, f"{duplicates} jobs produced duplicate results"

    charged = sql("SELECT count(*) FROM jobs WHERE id = ANY(%s) AND quota_charged", (job_ids,))[0][0]
    assert charged == len(job_ids)
    assert quota_of(1) == quota_before - len(job_ids), "charges do not match completions"


def test_quota_never_goes_negative_under_load():
    """The CHECK constraint is the last line of defence: it survives an
    application bug, which a convention does not."""
    assert sql("SELECT count(*) FROM companies WHERE job_quota < 0")[0][0] == 0

    with pytest.raises(psycopg.errors.CheckViolation):
        sql("UPDATE companies SET job_quota = -1 WHERE id=1", fetch=False)


def test_concurrent_submissions_cannot_exceed_the_limit():
    """The brief's repro (two curl &) does not open the window: process overhead
    serialises the requests. A synchronised barrier does."""
    sql("UPDATE jobs SET status='done' WHERE company_id=1 AND status IN ('queued','running')",
        fetch=False)
    sql("UPDATE companies SET max_concurrent_jobs=2 WHERE id=1", fetch=False)
    try:
        barrier = threading.Barrier(20)
        codes, lock = [], threading.Lock()

        def submit():
            session = requests.Session()
            try:
                session.get(f"{API}/health", timeout=5)  # warm the connection
            except Exception:
                pass
            barrier.wait()
            try:
                code = session.post(f"{API}/jobs", headers={"X-Auth": ACME},
                                    json={"kind": "report"}, timeout=30).status_code
            except Exception:
                code = 0
            with lock:
                codes.append(code)

        threads = [threading.Thread(target=submit) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        active = sql(
            "SELECT count(*) FROM jobs WHERE company_id=1 AND status IN ('queued','running')"
        )[0][0]
        assert codes.count(200) <= 2, f"{codes.count(200)} accepted against a limit of 2"
        assert active <= 2, f"{active} active jobs against a limit of 2"
    finally:
        sql("UPDATE companies SET max_concurrent_jobs=5000 WHERE id=1", fetch=False)


# ---------------------------------------------------------------------------
# KNOWN_ISSUES Symptom 3: a failed job cannot be traced
# ---------------------------------------------------------------------------

def test_submission_is_traceable_from_api_to_worker():
    """The request_id is the field that ties an HTTP submission to its
    processing. Originally no such identifier existed."""
    request_id = f"trace-{random.randrange(10**9)}"
    response = requests.post(
        f"{API}/jobs",
        headers={"X-Auth": ACME, "X-Request-Id": request_id},
        json={"kind": "report"},
    )
    assert response.headers.get("X-Request-Id") == request_id, "correlation chain broken"

    job_id = response.json()["id"]
    assert sql("SELECT request_id FROM jobs WHERE id=%s", (job_id,))[0][0] == request_id

    wait_for_status(job_id, "done")

    # The worker must carry the same id into the events it writes.
    carried = sql(
        "SELECT count(*) FROM job_events WHERE job_id=%s AND request_id=%s AND actor LIKE 'worker:%%'",
        (job_id, request_id),
    )[0][0]
    assert carried > 0, "worker did not propagate the request_id"


def test_event_trail_covers_the_whole_lifecycle():
    """jobs.status holds only the current state; the trail answers what happened."""
    job_id = job_ready_for_retry()
    requests.post(f"{API}/jobs/{job_id}/retry", headers={"X-Auth": ACME})
    wait_for_status(job_id, "done")

    trail = events_of(job_id)
    for expected in ("job.created", "job.retried", "job.claimed", "job.done"):
        assert expected in trail, f"{expected} missing from the trail: {trail}"
    assert trail.index("job.retried") < trail.index("job.done")


def test_trail_records_jobs_created_outside_the_api():
    """A database trigger guarantees the origin event, so a job inserted by a
    load script or a manual fix does not start its timeline mid-story."""
    job_id = enqueue()
    assert "job.created" in events_of(job_id)

    actor = sql("SELECT actor FROM job_events WHERE job_id=%s AND event='job.created'",
                (job_id,))[0][0]
    assert actor == "db"


def test_orphaned_running_job_is_returned_to_the_queue():
    """A worker killed mid-flight used to strand its job in 'running' forever,
    permanently burning one of the tenant's concurrency slots."""
    job_id = enqueue(status="running")
    sql("UPDATE jobs SET locked_by='dead-worker', locked_until=now() - interval '1 minute',"
        " attempts=1 WHERE id=%s", (job_id,), fetch=False)

    recovered = wait_for_status(job_id, "queued", "done", timeout=30)
    assert recovered in ("queued", "done"), "reaper did not recover the orphan"

    assert "job.reaped" in events_of(job_id), "recovery was not recorded in the trail"


def test_job_events_are_isolated_by_tenant():
    """The trail must not become a side channel around the other fixes."""
    job_id = enqueue()
    assert requests.get(f"{API}/jobs/{job_id}/events", headers={"X-Auth": ACME}).status_code == 200
    assert requests.get(f"{API}/jobs/{job_id}/events", headers={"X-Auth": GLOBEX}).status_code == 404


# ---------------------------------------------------------------------------
# Lease renewal (worker.LeaseKeeper)
#
# Driven against the worker module directly: the simulated handlers finish in a
# second, so no job the stack can produce lasts long enough to exercise renewal
# end to end. The database, the SQL and the thread are the real ones.
# ---------------------------------------------------------------------------

def lease_expiry(job_id: int):
    return sql("SELECT locked_until FROM jobs WHERE id=%s", (job_id,))[0][0]


@pytest.fixture
def lease_job():
    """Build a job held by this process, with a lease expiring in `seconds`.

    The window matters: too short and the live reaper requeues the job mid-test,
    which would look like a renewal failure.
    """
    import worker

    def build(seconds: int, owner: str | None = None):
        job_id = enqueue(status="running")
        sql("UPDATE jobs SET locked_by=%s, locked_until=now() + make_interval(secs => %s)"
            " WHERE id=%s", (owner or worker.WORKER_ID, seconds, job_id), fetch=False)
        return job_id

    return build


@pytest.fixture
def fast_renewal(monkeypatch):
    import worker

    monkeypatch.setattr(worker, "LEASE_RENEW_SECONDS", 1)
    return worker


def test_lease_is_extended_while_the_job_is_still_running(lease_job, fast_renewal):
    """A job outlasting the lease used to be handed to another worker while it
    was still being processed, throwing away the work in flight."""
    worker = fast_renewal
    job_id = lease_job(5)
    before = lease_expiry(job_id)

    with worker.LeaseKeeper({"id": job_id}) as lease:
        time.sleep(2.5)
        after = lease_expiry(job_id)

    assert not lease.lost
    assert after > before, "the lease did not advance while the job was running"


def test_lease_renewal_detects_that_the_job_was_taken_away(lease_job, fast_renewal):
    """Cancellation, reaping or a competing worker all show up the same way: the
    conditional update matches nothing. The worker must notice rather than keep
    spending the attempt on a result nobody will accept."""
    worker = fast_renewal
    job_id = lease_job(60)

    with worker.LeaseKeeper({"id": job_id}) as lease:
        sql("UPDATE jobs SET status='cancelled' WHERE id=%s", (job_id,), fetch=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not lease.lost:
            time.sleep(0.05)

    assert lease.lost, "losing the lease went unnoticed"


def test_lease_renewal_never_touches_another_workers_job(lease_job, fast_renewal):
    """The renewal is scoped by locked_by, so a stale keeper cannot extend a
    lease that now belongs to somebody else."""
    worker = fast_renewal
    job_id = lease_job(60, owner="another-worker")
    before = lease_expiry(job_id)

    with worker.LeaseKeeper({"id": job_id}) as lease:
        time.sleep(2.5)

    assert lease.lost
    assert lease_expiry(job_id) == before, "extended a lease belonging to another worker"
