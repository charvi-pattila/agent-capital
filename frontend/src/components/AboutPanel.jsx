import { useState } from "react";
import { api } from "../api";

export default function AboutPanel({ project, onSaved }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(project.description || "");
  const [generating, setGenerating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const generate = async () => {
    setGenerating(true);
    setError("");
    try {
      const updated = await api.generateDescription(project.id);
      if (updated.error) throw new Error(updated.error);
      setText(updated.description || "");
      onSaved();
    } catch (e) {
      setError("Couldn't generate a description — try again or write one yourself.");
    } finally {
      setGenerating(false);
    }
  };

  const save = async () => {
    setSaving(true);
    await api.updateProject(project.id, { description: text });
    setSaving(false);
    setEditing(false);
    onSaved();
  };

  if (editing) {
    return (
      <div className="memory-panel">
        <div className="memory-section">
          <div className="memory-section-title">About this project</div>
          <textarea
            className="memory-textarea"
            value={text}
            onChange={e => setText(e.target.value)}
            placeholder="Describe what this project does..."
            autoFocus
          />
          <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
            <button className="btn btn-primary" onClick={save} disabled={saving}>
              {saving ? "Saving..." : "Save"}
            </button>
            <button
              className="btn btn-ghost"
              onClick={() => { setEditing(false); setText(project.description || ""); }}
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="memory-panel">
      <div className="memory-section">
        <div className="memory-section-title">About this project</div>
        {project.description ? (
          <p style={{ color: "var(--text2)", lineHeight: 1.6, whiteSpace: "pre-wrap" }}>
            {project.description}
          </p>
        ) : (
          <p style={{ color: "var(--text3)" }}>
            No description yet. Generate one by having Claude read through the project's
            code, or write your own.
          </p>
        )}
        {error && <p style={{ color: "var(--danger, #e5484d)", fontSize: 13 }}>{error}</p>}
        <div style={{ display: "flex", gap: 10, marginTop: 4 }}>
          <button
            className="btn btn-ghost btn-sm"
            onClick={() => { setText(project.description || ""); setEditing(true); }}
          >
            Edit
          </button>
          <button className="btn btn-ghost btn-sm" onClick={generate} disabled={generating}>
            {generating ? "Reading project…" : project.description ? "Regenerate from code" : "Generate from code"}
          </button>
        </div>
      </div>
    </div>
  );
}
