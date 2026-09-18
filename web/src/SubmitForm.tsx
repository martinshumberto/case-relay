import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, post } from "./api";

export function SubmitForm({ auth }: { auth: string }) {
  const [kind, setKind] = useState("report");
  const queryClient = useQueryClient();

  const submit = useMutation({
    mutationFn: () => post<{ id: number }>("/jobs", auth, { kind }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });

  const failure = submit.error as ApiError | null;

  return (
    <div style={{ margin: "12px 0" }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <select value={kind} onChange={(e) => setKind(e.target.value)} aria-label="Tipo do job">
          <option value="report">report</option>
          <option value="import">import</option>
        </select>

        {/* disabled stops a double click from issuing two POSTs. */}
        <button onClick={() => submit.mutate()} disabled={submit.isPending}>
          {submit.isPending ? "Enviando…" : "Enviar job"}
        </button>
      </div>

      {failure && (
        <p role="alert" style={{ color: "#c00", fontSize: 13 }}>
          {failure.message}
          {failure.requestId && <span style={{ color: "#999" }}> (req {failure.requestId})</span>}
        </p>
      )}
      {submit.isSuccess && !failure && (
        <p style={{ color: "#0b6", fontSize: 13 }}>Job #{submit.data?.id} enfileirado.</p>
      )}
    </div>
  );
}
