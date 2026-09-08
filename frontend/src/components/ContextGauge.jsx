import { useEffect, useState } from "react";
import { api } from "../api";
import Icon from "./Icon";

// Live context size of a running session, plus a manual "trim" that does the
// same checkpoint-then-/compact the backend monitor does automatically once a
// session crosses the bloat line. Bar colour: grey below half the threshold,
// yellow above it, red once the monitor would act.
export default function ContextGauge({ projectId, initial }) {
  const [ctx, setCtx] = useState(initial || null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    const load = () => api.getContext(projectId).then(c => { if (alive) setCtx(c); }).catch(() => {});
    load();
    const t = setInterval(load, 15000);
    return () => { alive = false; clearInterval(t); };
  }, [projectId]);

  if (!ctx || ctx.tokens == null) return null;

  const trimming = !!ctx.trim_state || busy;
  const level = ctx.bloated ? "hot" : ctx.tokens >= ctx.threshold / 2 ? "warn" : "";
  const pct = ctx.threshold ? Math.min(100, Math.round((ctx.tokens / ctx.threshold) * 100)) : 0;
  const hours = ctx.hours != null ? ` · ${ctx.hours}h` : "";
  const title = `${ctx.tokens.toLocaleString()} tokens in context${hours}${ctx.trims ? ` · trimmed ${ctx.trims}×` : ""}`;

  const trim = async () => {
    setBusy(true); setError("");
    try {
      const r = await api.trimContext(projectId);
      if (!r.started) setError(r.error || "Couldn't start");
    } catch (_) { setError("Couldn't start"); }
    setTimeout(() => setBusy(false), 3000);
  };

  return (
    <span className="gauge" title={title}>
      <span className="gauge-bar"><span className={"gauge-fill " + level} style={{ width: `${pct}%`, display: "block" }} /></span>
      <span style={{ fontSize: 12, color: "var(--text2)", whiteSpace: "nowrap" }}>
        {trimming ? (ctx.trim_state === "compacting" ? "compacting…" : "checkpointing…") : `${Math.round(ctx.tokens / 1000)}k`}
      </span>
      {!trimming && (
        <button className="btn btn-ghost btn-sm btn-icon" onClick={trim} title="Trim: save a checkpoint to memory, then compact the session's context" aria-label="Trim context">
          <Icon name="scissors" size={15} />
        </button>
      )}
      {error && <span style={{ color: "var(--yellow)", fontSize: 12 }}>{error}</span>}
    </span>
  );
}
