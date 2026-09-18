import { useState } from "react";
import { QueryClient, QueryClientProvider, useInfiniteQuery } from "@tanstack/react-query";
import { AuthSwitcher, FAKE_USERS, roleOf } from "./auth";
import { JobsList } from "./JobsList";
import { SubmitForm } from "./SubmitForm";
import { ErrorBoundary } from "./ErrorBoundary";
import { ApiError, get, type AdminJob, type Page } from "./api";

const queryClient = new QueryClient();

function AdminJobs({ auth }: { auth: string }) {
  const [isOpen, setIsOpen] = useState(false);

  const { data, error, isPending, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useInfiniteQuery<Page<AdminJob>>({
      queryKey: ["admin-jobs", auth],
      queryFn: ({ pageParam, signal }) => {
        const cursor = pageParam as string | null;
        const query = "limit=25" + (cursor ? `&cursor=${encodeURIComponent(cursor)}` : "");
        return get<Page<AdminJob>>(`/admin/jobs?${query}`, auth, signal);
      },
      initialPageParam: null,
      getNextPageParam: (lastPage) => lastPage.next_cursor,
      enabled: isOpen,
    });

  return (
    <div style={{ marginTop: 24 }}>
      <button onClick={() => setIsOpen((v) => !v)} aria-expanded={isOpen}>
        {isOpen ? "Ocultar" : "Ver todos (admin)"}
      </button>

      {isOpen && isPending && <p style={{ color: "#999" }}>Carregando…</p>}

      {isOpen && error && (
        <p role="alert" style={{ color: "#c00" }}>
          {(error as ApiError).message}
        </p>
      )}

      {isOpen && data && (
        <>
          <ul style={{ listStyle: "none", padding: 0 }}>
            {data.pages
              .flatMap((p) => p.items)
              .map((j) => (
                <li key={j.id} style={{ padding: "4px 0", fontSize: 14 }}>
                  <code style={{ color: "#999" }}>#{j.id}</code> empresa {j.company_id} · {j.kind} ·{" "}
                  {j.status}
                </li>
              ))}
          </ul>
          {hasNextPage && (
            <button
              onClick={() => fetchNextPage()}
              disabled={isFetchingNextPage}
              aria-label="Carregar mais jobs da visão administrativa"
            >
              {isFetchingNextPage ? "Carregando…" : "Carregar mais"}
            </button>
          )}
        </>
      )}
    </div>
  );
}

export default function App() {
  const [auth, setAuth] = useState<string>(FAKE_USERS[0]);

  return (
    <QueryClientProvider client={queryClient}>
      <div style={{ maxWidth: 720, margin: "40px auto", fontFamily: "sans-serif" }}>
        <h1>Relay</h1>
        <AuthSwitcher auth={auth} onChange={setAuth} />

        <h2>Jobs</h2>
        <ErrorBoundary>
          <SubmitForm auth={auth} />
          <JobsList auth={auth} />
        </ErrorBoundary>

        {roleOf(auth) === "admin" && (
          <ErrorBoundary>
            <AdminJobs auth={auth} />
          </ErrorBoundary>
        )}
      </div>
    </QueryClientProvider>
  );
}
