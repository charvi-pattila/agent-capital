import { useState, useEffect, useRef } from "react";
import Icon, { ProjectMark } from "./Icon";
import { api } from "../api";

// phase: "confirm" → "closing" → "done" | "error" | "none-running"
export default function CloseAllModal({ onClose }) {
  const [phase, setPhase] = useState("loading");
  const [targets, setTargets] = useState([]); // projects being closed
  const [remaining, setRemaining] = useState([]); // ids still running
  const [report, setReport] = useState(null); // daily-report outcome from close-all
  const pollRef = useRef(null);

  useEffect(() => {
    api.getRunning().then(list => {
      if (!list.length) {
        setPhase("none-running");
      } else {
        setTargets(list);
        setRemaining(list.map(p => p.id));
        setPhase("confirm");
      }
    }).catch(() => setPhase("error"));
    return () => clearInterval(pollRef.current);
  }, []);

  const startClosing = async () => {
    setPhase("closing");
    pollRef.current = setInterval(async () => {
      try {
        const list = await api.getRunning();
        setRemaining(list.map(p => p.id));
      } catch (_) {}
    }, 2000);

    try {
      const res = await api.closeAll();
      clearInterval(pollRef.current);
      setRemaining([]);
      setReport(res.report || null);
      setPhase("done");
      // Stay open when there's a report outcome worth reading (especially a
      // failed send) instead of auto-dismissing it out from under the user.
      if (res.report?.emailed) setTimeout(onClose, 4000);
    } catch (_) {
      clearInterval(pollRef.current);
      setPhase("error");
    }
  };

  const total = targets.length;
  const closed = total - remaining.length;
  const pct = total ? Math.round((closed / total) * 100) : 0;
  const canDismiss = phase !== "closing";

  return (
    <div className="modal-overlay" onClick={(e) => canDismiss && e.target === e.currentTarget && onClose()}>
      <div className="modal close-modal">
        {phase === "loading" && <div className="close-modal-body">Checking running sessions...</div>}

        {phase === "none-running" && (
          <>
            <h2>Nothing to close</h2>
            <div className="close-modal-body">No sessions are currently running.</div>
            <div className="modal-actions">
              <button className="btn btn-primary" onClick={onClose}>OK</button>
            </div>
          </>
        )}

        {phase === "confirm" && (
          <>
            <h2>Close all sessions?</h2>
            <div className="close-modal-body">
              Claude will save a session summary for each project below, then the sessions will be closed.
              This can take a minute or two.
            </div>
            <ul className="close-modal-list">
              {targets.map(p => (
                <li key={p.id}><ProjectMark name={p.name} size={22} /> {p.name}</li>
              ))}
            </ul>
            <div className="modal-actions">
              <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
              <button className="btn btn-primary" onClick={startClosing}>
                Save &amp; close {total} session{total !== 1 ? "s" : ""}
              </button>
            </div>
          </>
        )}

        {(phase === "closing" || phase === "done") && (
          <>
            <h2>{phase === "done" ? "All sessions closed" : "Saving & closing sessions..."}</h2>
            <div className="progress-track">
              <div className={"progress-fill" + (phase === "done" ? " done" : "")} style={{ width: `${pct}%` }} />
            </div>
            <div className="close-modal-body" style={{ marginTop: 10 }}>
              {phase === "done"
                ? `Saved ${total} project${total !== 1 ? "s" : ""} — safe to walk away.`
                : `${closed} of ${total} closed — Claude is writing session summaries...`}
            </div>
            <ul className="close-modal-list">
              {targets.map(p => {
                const stillRunning = remaining.includes(p.id);
                return (
                  <li key={p.id} className={stillRunning ? "" : "closed"}>
                    <ProjectMark name={p.name} size={22} /> {p.name}
                    <span className="close-item-status">
                      {stillRunning ? <span className="spinner" /> : <><Icon name="check" size={14} /> saved</>}
                    </span>
                  </li>
                );
              })}
            </ul>
            {phase === "done" && report && (
              <div className={"report-note" + (report.emailed ? " sent" : "")}>
                {report.emailed
                  ? <><Icon name="check" size={14} /> Daily report emailed — {report.projects} project{report.projects !== 1 ? "s" : ""} covered.</>
                  : <><Icon name="file" size={14} /> {report.detail}{report.pdf ? <div className="report-path">{report.pdf}</div> : null}</>}
              </div>
            )}
            {phase === "done" && (
              <div className="modal-actions">
                <button className="btn btn-primary" onClick={onClose}>Done</button>
              </div>
            )}
          </>
        )}

        {phase === "error" && (
          <>
            <h2>Something went wrong</h2>
            <div className="close-modal-body">Couldn't finish closing all sessions. Check the Running tab to see what's still open.</div>
            <div className="modal-actions">
              <button className="btn btn-primary" onClick={onClose}>Close</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
