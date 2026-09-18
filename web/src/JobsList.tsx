import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError, get, post, type Job, type Page } from "./api";

const PAGE_SIZE = 20;

const CANCELAVEL = new Set(["queued", "running"]);

const COR: Record<string, string> = {
  queued: "#666",
  running: "#0b6",
  done: "#06c",
  failed: "#c00",
  cancelled: "#999",
};

function JobRow({ job, auth }: { job: Job; auth: string }) {
  const queryClient = useQueryClient();

  const action = useMutation({
    mutationFn: (rota: "cancel" | "retry") => post(`/jobs/${job.id}/${rota}`, auth),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });

  const failure = action.error as ApiError | null;

  return (
    <li style={{ padding: "8px 0", borderBottom: "1px solid #eee" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        {/* The id lets the user reference this job in a support ticket. */}
        <code style={{ color: "#999" }}>#{job.id}</code>
        <strong>{job.kind}</strong>
        <span style={{ color: COR[job.status] ?? "#333" }}>{job.status}</span>
        <span style={{ color: "#999", fontSize: 12 }}>
          {new Date(job.created_at).toLocaleString()}
        </span>
        {job.attempts > 0 && (
          <span style={{ color: "#999", fontSize: 12 }}>
            tentativa {job.attempts}/{job.max_attempts}
          </span>
        )}
        {job.result_count > 0 && <span style={{ fontSize: 12 }}>✓ resultado</span>}

        {CANCELAVEL.has(job.status) && (
          <button
            onClick={() => action.mutate("cancel")}
            disabled={action.isPending}
            aria-label={`Cancelar job ${job.id}`}
          >
            {action.isPending ? "…" : "Cancelar"}
          </button>
        )}
        {job.status === "failed" && job.attempts < job.max_attempts && (
          <button
            onClick={() => action.mutate("retry")}
            disabled={action.isPending}
            aria-label={`Reprocessar job ${job.id}`}
          >
            {action.isPending ? "…" : "Reprocessar"}
          </button>
        )}
      </div>

      {job.failure_reason && (
        <div style={{ color: "#c00", fontSize: 12, marginTop: 4 }}>
          motivo: {job.failure_reason}
        </div>
      )}

      {failure && (
        <div role="alert" style={{ color: "#c00", fontSize: 12, marginTop: 4 }}>
          {failure.message}
          {failure.requestId && <span style={{ color: "#999" }}> (req {failure.requestId})</span>}
        </div>
      )}
    </li>
  );
}

export function JobsList({ auth }: { auth: string }) {
  const {
    data,
    error,
    isPending,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
  } = useInfiniteQuery<Page<Job>>({
    queryKey: ["jobs", auth],
    queryFn: ({ pageParam, signal }) => {
      const cursor = pageParam as string | null;
      const query = `limit=${PAGE_SIZE}` + (cursor ? `&cursor=${encodeURIComponent(cursor)}` : "");
      return get<Page<Job>>(`/jobs?${query}`, auth, signal);
    },
    initialPageParam: null,
    getNextPageParam: (lastPage) => lastPage.next_cursor,
    // Polling revalidates every loaded page. Someone paging deep is browsing
    // history rather than watching the queue, so the interval grows with the
    // page count to keep the cost from scaling with it.
    refetchInterval: (query) => {
      const pages = query.state.data?.pages ?? [];
      const hasActive = pages
        .flatMap((p) => p.items)
        .some((j) => j.status === "queued" || j.status === "running");
      if (pages.length > 1) return 30000;
      return hasActive ? 1500 : 10000;
    },
    refetchIntervalInBackground: false,
  });

  if (isPending) return <p style={{ color: "#999" }}>Carregando…</p>;

  if (error) {
    const e = error as ApiError;
    return (
      <p role="alert" style={{ color: "#c00" }}>
        Não foi possível carregar os jobs: {e.message}
        {e.requestId && <span style={{ color: "#999" }}> (req {e.requestId})</span>}
      </p>
    );
  }

  const jobs = data.pages.flatMap((p) => p.items);

  if (!jobs.length) return <p style={{ color: "#999" }}>Nenhum job ainda.</p>;

  return (
    <>
      <ul aria-live="polite" style={{ listStyle: "none", padding: 0 }}>
        {jobs.map((job) => (
          <JobRow key={job.id} job={job} auth={auth} />
        ))}
      </ul>

      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 12 }}>
        {hasNextPage && (
          <button
            onClick={() => fetchNextPage()}
            disabled={isFetchingNextPage}
            aria-label="Carregar mais jobs"
          >
            {isFetchingNextPage ? "Carregando…" : "Carregar mais"}
          </button>
        )}
        {/* Shows how many are loaded, not the total: a COUNT(*) per page would
            reintroduce the cost pagination exists to avoid. */}
        <span style={{ color: "#999", fontSize: 12 }}>
          {jobs.length} job{jobs.length > 1 ? "s" : ""} carregado
          {jobs.length > 1 ? "s" : ""}
          {!hasNextPage && jobs.length > PAGE_SIZE && " — fim da lista"}
        </span>
      </div>
    </>
  );
}
