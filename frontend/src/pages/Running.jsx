import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api, streamRunning } from "../api";

export default function Running() {
  const [sessions, setSessions] = useState([]);
  const navigate = useNavigate();

  useEffect(() => {
    const load = () => api.getRunning().then(setSessions).catch(() => {});
    load();
    const t = setInterval(load, 3000);
    const unsub = streamRunning(setSessions);
    return () => { clearInterval(t); unsub(); };
  }, []);

  const running = sessions.filter(s => s.status === "running");
  const blocked = sessions.filter(s => s.status === "blocked");

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Running Sessions</div>
          <div className="page-subtitle">{running.length} active · {blocked.length} need input</div>
        </div>
      </div>

      {blocked.length > 0 && (
        <div className="alert-section">
          <div className="alert-header">⚠️ Needs Your Input</div>
          {blocked.map(s => (
            <div key={s.id} className="session-card blocked" onClick={() => navigate(`/project/${s.id}`)}>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <span style={{ fontSize: 24 }}>{s.emoji}</span>
                <div>
                  <div style={{ fontWeight: 600 }}>{s.name}</div>
                  <div style={{ fontSize: 13, color: "var(--yellow)" }}>{s.blocked_reason || "Waiting for input"}</div>
                </div>
              </div>
              <button className="btn btn-primary btn-sm">Respond →</button>
            </div>
          ))}
        </div>
      )}

      {running.length === 0 && blocked.length === 0 ? (
        <div style={{ textAlign: "center", paddingTop: 60, color: "var(--text2)" }}>
          <div style={{ fontSize: 40, marginBottom: 12 }}>😴</div>
          <div>No active sessions</div>
        </div>
      ) : (
        <div className="session-list">
          {running.map(s => (
            <div key={s.id} className="session-card" onClick={() => navigate(`/project/${s.id}`)}>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <div className="status-dot running" />
                <span className="session-emoji-wrap">
                  <span style={{ fontSize: 22 }}>{s.emoji}</span>
                  {s.unread > 0 && <span className="unread-badge">{s.unread > 9 ? "9+" : s.unread}</span>}
                </span>
                <div>
                  <div style={{ fontWeight: 600 }}>{s.name}</div>
                  <div style={{ fontSize: 13, color: "var(--text2)" }}>
                    {s.unread > 0
                      ? <span style={{ color: "var(--red)" }}>{s.unread} new repl{s.unread > 1 ? "ies" : "y"} since you looked</span>
                      : (s.last_activity || "Active")}
                  </div>
                </div>
              </div>
              <span className="tag tag-running">running</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
