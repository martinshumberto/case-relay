const FALLBACK_API = "http://localhost:8000";

if (!import.meta.env.VITE_API_URL) {
  console.warn(`VITE_API_URL não definida — usando ${FALLBACK_API}`);
}

export const API = import.meta.env.VITE_API_URL ?? FALLBACK_API;

export type JobStatus = "queued" | "running" | "done" | "failed" | "cancelled";

export interface Job {
  id: number;
  kind: string;
  status: JobStatus;
  created_at: string;
  attempts: number;
  max_attempts: number;
  failure_reason: string | null;
  result_count: number;
}

export interface AdminJob {
  id: number;
  company_id: number;
  kind: string;
  status: JobStatus;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** Shown to the user so they can quote it in a support ticket. */
    readonly requestId: string | null,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// fetch only rejects on network errors: an HTTP error status resolves normally,
// so {"detail": ...} would reach components as if it were valid data.
async function request<T>(path: string, auth: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, {
    ...init,
    headers: { "X-Auth": auth, "Content-Type": "application/json", ...init?.headers },
  });

  const requestId = response.headers.get("X-Request-Id");

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail =
        typeof body?.detail === "string" ? body.detail : JSON.stringify(body?.detail ?? body);
    } catch {
      // no JSON body: keep statusText
    }
    throw new ApiError(response.status, detail, requestId);
  }

  return (await response.json()) as T;
}

export function get<T>(path: string, auth: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, auth, { signal });
}

export function post<T>(path: string, auth: string, body?: unknown): Promise<T> {
  return request<T>(path, auth, {
    method: "POST",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}
