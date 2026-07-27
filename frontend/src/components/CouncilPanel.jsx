import { useEffect, useState } from "react";
import { api, streamCouncil } from "../api";

const STATE_LABEL = {
  idle: "Idle",
  running_council: "Council reviewing…",
  fixing: "Sending feedback to Claude…",
  approved: "Approved",
  gave_up: "Needs attention",
};

export default function CouncilPanel({ projectId }) {
  const [council, setCouncil] = useState(null);
  const [gitRepo, setGitRepo] = useState(true);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    let mounted = true;
    api.getCouncil(projectId).then(d => {
      if (!mounted) return;
      setCouncil(d);
      setGitRepo(d.git_repo !== false);
    });
    const stop = streamCouncil(projectId, d => setCouncil(d));
    return () => { mounted = false; stop(); };
  }, [projectId]);

  const runNow = async () => {
    setStarting(true);
    try { await api.runCouncil(projectId); } catch (_) {}
    setTimeout(() => setStarting(false), 2000);
  };

  if (!council) return <div style={{ color: "var(--text2)", padding: 20 }}>Loading…</div>;

  const state = council.state || "idle";
  const busy = state === "running_council" || state === "fixing";
  const runs = [...(council.runs || [])].reverse();

  return (
    <div className="memory-panel">
      <div className="memory-section">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div className="memory-section-title">Council</div>
          <span className={"tag " + (state === "approved" ? "tag-running" : state === "gave_up" ? "tag-failed" : busy ? "tag-paused" : "tag-stopped")}>
            {STATE_LABEL[state] || state}
          </span>
        </div>
        <p style={{ color: "var(--text2)", fontSize: 13, lineHeight: 1.6 }}>
          After every change, 5 independent Claude testers each read the diff, invent their own
          test cases, and vote. All 5 must approve — otherwise their feedback is sent back to
          Claude to fix and the council re-runs, up to 3 attempts.
        </p>
        {!gitRepo && (
          <p style={{ color: "var(--yellow)", fontSize: 13 }}>
            This project isn't a git repo, so the council can't detect changes yet. Run{" "}
            <code>git init</code> in the project folder to enable it.
          </p>
        )}
        {state === "fixing" && (
          <p style={{ color: "var(--yellow)", fontSize: 13 }}>
            Sending council feedback into the live session right now — avoid typing in Chat
            until this finishes to prevent input from getting mixed together.
          </p>
        )}
        <div>
          <button className="btn btn-ghost btn-sm" onClick={runNow} disabled={starting || busy}>
            {starting ? "Starting…" : "Run council now"}
          </button>
        </div>
      </div>

      <div className="memory-section">
        <div className="memory-section-title">History</div>
        {runs.length === 0 && <p style={{ color: "var(--text3)", fontSize: 13 }}>No council runs yet.</p>}
        {runs.map(run => <CouncilRun key={run.id} run={run} />)}
      </div>
    </div>
  );
}

function CouncilRun({ run }) {
  const [open, setOpen] = useState(false);
  const testers = run.testers || [];
  const approveCount = testers.filter(t => t.verdict === "APPROVE").length;
  const finished = !!run.finished;

  return (
    <div
      className="memory-item"
      onClick={() => setOpen(o => !o)}
      style={{ cursor: "pointer", flexDirection: "column", alignItems: "stretch" }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <span style={{ fontWeight: 600 }}>Attempt {run.attempt}</span>{" "}
          <span style={{ color: "var(--text3)", fontSize: 12 }}>
            {new Date(run.started).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
          </span>
        </div>
        <span className={"tag " + (!finished ? "tag-paused" : approveCount === testers.length ? "tag-running" : "tag-failed")}>
          {finished ? `${approveCount}/${testers.length} approved` : "reviewing…"}
        </span>
      </div>
      {open && (
        <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 10 }}>
          {testers.map((t, i) => (
            <div key={i} style={{ fontSize: 13 }}>
              <span style={{ fontWeight: 600, color: t.verdict === "APPROVE" ? "var(--green)" : "var(--red)" }}>
                {t.verdict === "APPROVE" ? "✓" : "✗"} {t.name}
              </span>
              <div style={{ color: "var(--text2)", marginTop: 2, whiteSpace: "pre-wrap" }}>{t.reason}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
