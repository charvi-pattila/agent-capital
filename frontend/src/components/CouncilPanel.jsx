import { useEffect, useState } from "react";
import { api, streamCouncil } from "../api";

const STATE_LABEL = {
  idle: "Idle",
  running_council: "Reviewer reading…",
  fixing: "Claude addressing feedback…",
  approved: "Approved",
  done: "Closed by Claude",
  gave_up: "Needs attention",
};

const AUTHOR_LABEL = {
  DONE: "Claude closed the review",
  AGAIN: "Claude asked for another pass",
  NONE: "Claude replied without a decision (treated as closed)",
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
  const good = state === "approved" || state === "done";
  const runs = [...(council.runs || [])].reverse();

  return (
    <div className="memory-panel">
      <div className="memory-section">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div className="memory-section-title">Review</div>
          <span className={"tag " + (good ? "tag-running" : state === "gave_up" ? "tag-failed" : busy ? "tag-paused" : "tag-stopped")}>
            {STATE_LABEL[state] || state}
          </span>
        </div>
        <p style={{ color: "var(--text2)", fontSize: 13, lineHeight: 1.6 }}>
          After every change, one independent Claude reviewer reads the diff and either approves
          it or reports concrete problems. Problems go back to the Claude that made the change;
          it fixes what it agrees with, then decides whether the review is done or should run
          once more. Its call ends the loop (3 rounds max as a backstop).
        </p>
        {!gitRepo && (
          <p style={{ color: "var(--yellow)", fontSize: 13 }}>
            This project isn't a git repo, so the reviewer can't detect changes yet. Run{" "}
            <code>git init</code> in the project folder to enable it.
          </p>
        )}
        {state === "fixing" && (
          <p style={{ color: "var(--yellow)", fontSize: 13 }}>
            Sending review findings into the live session right now — avoid typing in Chat
            until this finishes to prevent input from getting mixed together.
          </p>
        )}
        <div>
          <button className="btn btn-ghost btn-sm" onClick={runNow} disabled={starting || busy}>
            {starting ? "Starting…" : "Review now"}
          </button>
        </div>
      </div>

      <div className="memory-section">
        <div className="memory-section-title">History</div>
        {runs.length === 0 && <p style={{ color: "var(--text3)", fontSize: 13 }}>No reviews yet.</p>}
        {runs.map(run => <ReviewRun key={run.id} run={run} />)}
      </div>
    </div>
  );
}

function ReviewRun({ run }) {
  const [open, setOpen] = useState(false);
  // Older runs (from the 5-tester council) carry several entries here; new
  // ones carry exactly one. Both render the same way.
  const testers = run.testers || [];
  const approveCount = testers.filter(t => t.verdict === "APPROVE").length;
  const finished = !!run.finished;
  const author = run.author;

  let tagClass, tagText;
  if (!finished) { tagClass = "tag-paused"; tagText = "reviewing…"; }
  else if (run.aborted) { tagClass = "tag-stopped"; tagText = "interrupted"; }
  else if (run.approved) { tagClass = "tag-running"; tagText = "approved"; }
  else if (author?.decision === "AGAIN") { tagClass = "tag-paused"; tagText = "fixed · re-reviewed"; }
  else if (author) { tagClass = "tag-running"; tagText = "fixed · closed"; }
  else if (testers.length > 1) { tagClass = "tag-failed"; tagText = `${approveCount}/${testers.length} approved`; }
  else { tagClass = "tag-failed"; tagText = "rejected"; }

  return (
    <div
      className="memory-item"
      onClick={() => setOpen(o => !o)}
      style={{ cursor: "pointer", flexDirection: "column", alignItems: "stretch" }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div>
          <span style={{ fontWeight: 600 }}>Round {run.attempt}</span>{" "}
          <span style={{ color: "var(--text3)", fontSize: 12 }}>
            {new Date(run.started).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}
          </span>
        </div>
        <span className={"tag " + tagClass}>{tagText}</span>
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
          {author && (
            <div style={{ fontSize: 13 }}>
              <span style={{ fontWeight: 600, color: author.decision === "AGAIN" ? "var(--yellow)" : "var(--green)" }}>
                {AUTHOR_LABEL[author.decision] || author.decision}
              </span>
              {author.reply && (
                <div style={{ color: "var(--text2)", marginTop: 2, whiteSpace: "pre-wrap" }}>{author.reply}</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
