import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";

function UpdateCard({ title, line, note, button, run, busy, setBusy, onDone }) {
  const [result, setResult] = useState(null);
  const go = async () => {
    setBusy(true);
    setResult(null);
    try {
      setResult(await run());
    } catch (e) {
      // Updating the app restarts the server, which drops this very request.
      setResult({ ok: true, output: "Server is restarting. Reload in a few seconds." });
    }
    setBusy(false);
    onDone();
  };
  return (
    <div className="memory-section">
      <div className="memory-section-title">{title}</div>
      <p style={{ color: "var(--text2)" }}>{line}</p>
      {note && <p style={{ color: "var(--text3)", fontSize: 13 }}>{note}</p>}
      <button className="btn btn-primary btn-sm" onClick={go} disabled={busy}>
        {busy ? "Working…" : button}
      </button>
      {result && (
        <pre style={{ marginTop: 12, fontSize: 12, whiteSpace: "pre-wrap", color: result.ok ? "var(--text2)" : "var(--danger, #e5484d)" }}>
          {result.error || result.output || (result.ok ? "Done." : "Failed.")}
        </pre>
      )}
    </div>
  );
}

export default function Profile() {
  const [st, setSt] = useState(null);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const editSelf = () => api.openSelfProject().then(p => navigate(`/project/${p.id}`));
  const load = () => api.getUpdateStatus().then(setSt).catch(() => {});
  useEffect(() => { load(); }, []);

  const behind = st?.app_behind;
  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Profile</div>
          <div className="page-subtitle">Keep Claude Code and Agent Capital up to date</div>
        </div>
      </div>
      <div className="memory-panel">
        <UpdateCard
          title="Claude Code"
          line={st ? st.claude_version || "Not found" : "Checking…"}
          button="Update Claude Code"
          run={api.updateClaude}
          busy={busy} setBusy={setBusy} onDone={load}
        />
        <UpdateCard
          title="Agent Capital"
          line={st
            ? `${st.app_head}${behind ? ` · ${behind} update${behind > 1 ? "s" : ""} available` : behind === 0 ? " · up to date" : ""}`
            : "Checking…"}
          note={st?.app_dirty
            ? "This copy has local changes. The update only fast-forwards, so it may refuse."
            : "Pulls the latest code, rebuilds, and restarts the server. Running sessions may be interrupted."}
          button="Update Agent Capital"
          run={api.updateApp}
          busy={busy} setBusy={setBusy} onDone={() => setTimeout(load, 8000)}
        />
        <div className="memory-section">
          <div className="memory-section-title">Edit Agent Capital</div>
          <p style={{ color: "var(--text3)", fontSize: 13 }}>
            Opens this app's own code as a project. Chat with Claude to change it, commit and push,
            then use Update Agent Capital above to rebuild and restart.
          </p>
          <button className="btn btn-ghost btn-sm" onClick={editSelf}>Open Agent Capital's code</button>
        </div>
      </div>
    </div>
  );
}
