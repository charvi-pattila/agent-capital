import { useState, useEffect } from "react";
import { api } from "../api";
import Icon from "./Icon";

export default function TestPanel({ projectId }) {
  const [tests, setTests] = useState([]);
  const [running, setRunning] = useState(false);
  const [form, setForm] = useState({ type: "description", description: "", target_url: "" });
  const [showForm, setShowForm] = useState(false);

  const load = () => api.getTests(projectId).then(setTests);
  useEffect(() => { load(); }, [projectId]);

  const addTest = async (e) => {
    e.preventDefault();
    await api.createTest(projectId, form);
    setForm({ type: "description", description: "", target_url: "" });
    setShowForm(false);
    load();
  };

  const runTests = async () => {
    setRunning(true);
    await api.runTests(projectId);
    setRunning(false);
    load();
  };

  const statusIcon = (s) => {
    const spec = { passing: ["check", "var(--green)"], failing: ["x", "var(--red)"], running: ["clock", "var(--yellow)"], pending: ["pause", "var(--text3)"], baseline_saved: ["camera", "var(--accent2)"] }[s] || ["pause", "var(--text3)"];
    return <Icon name={spec[0]} size={16} style={{ color: spec[1], verticalAlign: "-3px" }} />;
  };

  const acceptBaseline = async (testId) => {
    await api.acceptTestBaseline(projectId, testId);
    load();
  };

  return (
    <div className="test-panel">
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 20 }}>
        <div style={{ fontWeight: 600, fontSize: 16 }}>Tests</div>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="btn btn-ghost btn-sm" onClick={() => setShowForm(true)}>+ Add Test</button>
          <button className="btn btn-primary btn-sm" onClick={runTests} disabled={running || tests.length === 0}>
            {running ? "Running..." : <><Icon name="play" size={14} /> Run all</>}
          </button>
        </div>
      </div>

      {showForm && (
        <form className="test-form" onSubmit={addTest}>
          <div className="form-group">
            <label>Test Type</label>
            <select value={form.type} onChange={e => setForm(f => ({ ...f, type: e.target.value }))}>
              <option value="description">Description match</option>
              <option value="screenshot">Screenshot comparison</option>
              <option value="url">URL response check</option>
            </select>
          </div>
          <div className="form-group">
            <label>Description / Expected behavior</label>
            <textarea
              value={form.description}
              onChange={e => setForm(f => ({ ...f, description: e.target.value }))}
              placeholder="Describe what success looks like..."
            />
          </div>
          {(form.type === "screenshot" || form.type === "url") && (
            <div className="form-group">
              <label>Target URL</label>
              <input
                value={form.target_url}
                onChange={e => setForm(f => ({ ...f, target_url: e.target.value }))}
                placeholder="http://localhost:3000"
              />
            </div>
          )}
          <div className="modal-actions" style={{ margin: 0 }}>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setShowForm(false)}>Cancel</button>
            <button type="submit" className="btn btn-primary btn-sm">Add</button>
          </div>
        </form>
      )}

      {tests.length === 0 ? (
        <div style={{ textAlign: "center", paddingTop: 40, color: "var(--text2)" }}>
          <div className="empty-icon"><Icon name="flask" size={32} /></div>
          <div>No tests yet. Add a test to get started.</div>
        </div>
      ) : (
        <div className="test-list">
          {tests.map((t, i) => (
            <div key={i} className="test-item" style={{ flexDirection: "column", alignItems: "stretch", gap: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span style={{ fontSize: 18 }}>{statusIcon(t.status)}</span>
                  <div>
                    <div style={{ fontWeight: 500, fontSize: 14 }}>{t.description || t.type}</div>
                    {t.last_run && (
                      <div style={{ fontSize: 12, color: "var(--text3)" }}>Last run: {new Date(t.last_run).toLocaleString()}</div>
                    )}
                    {t.type === "screenshot" && typeof t.diff_percent === "number" && (
                      <div style={{ fontSize: 12, color: "var(--text3)" }}>{t.diff_percent}% pixels differ from baseline</div>
                    )}
                    {t.error && (
                      <div style={{ fontSize: 12, color: "var(--red)", marginTop: 4 }}>{t.error}</div>
                    )}
                  </div>
                </div>
                <span className={`tag ${t.status === "passing" ? "tag-running" : t.status === "failing" ? "tag-paused" : "tag-stopped"}`}>
                  {t.status || "pending"}
                </span>
              </div>

              {t.type === "screenshot" && t.last_run && (
                <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-start" }}>
                  {["baseline", "latest", "diff"].map(kind => (
                    <div key={kind} style={{ textAlign: "center" }}>
                      <div style={{ fontSize: 11, color: "var(--text3)", marginBottom: 4, textTransform: "capitalize" }}>{kind}</div>
                      <img
                        src={`${api.testImageUrl(projectId, t.id, kind)}?v=${t.last_run}`}
                        alt={kind}
                        style={{ width: 140, height: "auto", borderRadius: 6, border: "1px solid var(--border)", background: "#fff" }}
                        onError={(e) => { e.target.style.visibility = "hidden"; }}
                      />
                    </div>
                  ))}
                  {t.status !== "baseline_saved" && (
                    <button
                      className="btn btn-ghost btn-sm"
                      style={{ alignSelf: "center" }}
                      onClick={() => acceptBaseline(t.id)}
                    >Accept as new baseline</button>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
