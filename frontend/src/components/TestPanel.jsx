import { useState, useEffect } from "react";
import { api } from "../api";

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

  const statusIcon = (s) => ({ passing: "✅", failing: "❌", running: "⏳", pending: "⏸" }[s] || "⏸");

  return (
    <div className="test-panel">
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 20 }}>
        <div style={{ fontWeight: 600, fontSize: 16 }}>Tests</div>
        <div style={{ display: "flex", gap: 10 }}>
          <button className="btn btn-ghost btn-sm" onClick={() => setShowForm(true)}>+ Add Test</button>
          <button className="btn btn-primary btn-sm" onClick={runTests} disabled={running || tests.length === 0}>
            {running ? "Running..." : "▶ Run All"}
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
          <div style={{ fontSize: 32, marginBottom: 8 }}>🧪</div>
          <div>No tests yet. Add a test to get started.</div>
        </div>
      ) : (
        <div className="test-list">
          {tests.map((t, i) => (
            <div key={i} className="test-item">
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span style={{ fontSize: 18 }}>{statusIcon(t.status)}</span>
                <div>
                  <div style={{ fontWeight: 500, fontSize: 14 }}>{t.description || t.type}</div>
                  {t.last_run && (
                    <div style={{ fontSize: 12, color: "var(--text3)" }}>Last run: {new Date(t.last_run).toLocaleString()}</div>
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
          ))}
        </div>
      )}
    </div>
  );
}
