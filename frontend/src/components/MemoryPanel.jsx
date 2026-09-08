import { useState, useEffect } from "react";
import { api } from "../api";
import Icon from "./Icon";

const MEMORY_TYPES = [
  { key: "last_session", label: "Last Session", icon: "note", desc: "What happened last time, where we left off" },
  { key: "errors", label: "Errors & Fixes", icon: "bug", desc: "Problems encountered and how they were solved" },
  { key: "skills", label: "Skills", icon: "spark", desc: "Reusable patterns and learned techniques" },
];

export default function MemoryPanel({ projectId }) {
  const [memory, setMemory] = useState({});
  const [components, setComponents] = useState([]);
  const [globalMemory, setGlobalMemory] = useState("");
  const [editing, setEditing] = useState(null);
  const [editContent, setEditContent] = useState("");
  const [newComp, setNewComp] = useState("");
  const [saving, setSaving] = useState(false);

  const load = () => {
    api.getMemory(projectId).then(d => {
      setMemory(d.memory || {});
      setComponents(d.components || []);
    });
    api.getGlobalMemory().then(d => setGlobalMemory(d.content || ""));
  };

  useEffect(() => { load(); }, [projectId]);

  const startEdit = (key, content) => { setEditing(key); setEditContent(content || ""); };

  const save = async () => {
    setSaving(true);
    if (editing === "global") {
      await api.updateGlobalMemory(editContent);
    } else {
      const [type, comp] = editing.startsWith("comp:") ? [null, editing.slice(5)] : [editing, null];
      await api.updateMemory(projectId, type || "component", editContent, comp);
    }
    setEditing(null);
    setSaving(false);
    load();
  };

  const addComponent = async () => {
    const name = newComp.trim();
    if (!name) return;
    await api.updateMemory(projectId, "component", "", name);
    setNewComp("");
    load();
  };

  if (editing) {
    const label = editing === "global"
      ? "Global Memory"
      : editing.startsWith("comp:") ? editing.slice(5) : MEMORY_TYPES.find(t => t.key === editing)?.label;
    return (
      <div className="memory-editor">
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 16 }}>
          <button className="btn btn-ghost btn-sm" onClick={() => setEditing(null)}><Icon name="back" size={14} /> Back</button>
          <span style={{ fontWeight: 600 }}>{label}</span>
        </div>
        <textarea
          className="memory-textarea"
          value={editContent}
          onChange={e => setEditContent(e.target.value)}
          placeholder="Write memory content here..."
        />
        <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
          <button className="btn btn-primary" onClick={save} disabled={saving}>
            {saving ? "Saving..." : "Save"}
          </button>
          <button className="btn btn-ghost" onClick={() => setEditing(null)}>Cancel</button>
        </div>
      </div>
    );
  }

  return (
    <div className="memory-panel">
      <div className="memory-section">
        <div className="memory-section-title">
          Global Memory <span style={{ fontWeight: 400, color: "var(--text3)", fontSize: 12 }}>(shared across all projects)</span>
        </div>
        <div className="memory-item" onClick={() => startEdit("global", globalMemory)}>
          <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
            <Icon name="globe" size={20} style={{ color: "var(--accent2)" }} />
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 600, marginBottom: 2 }}>Shared Notes</div>
              <div style={{ fontSize: 13, color: "var(--text2)" }}>Injected into every project's context, not just this one</div>
              {globalMemory && (
                <div className="memory-preview">{globalMemory.slice(0, 120)}{globalMemory.length > 120 ? "..." : ""}</div>
              )}
            </div>
          </div>
          <button className="btn btn-ghost btn-sm">Edit <Icon name="chevron" size={14} /></button>
        </div>
      </div>

      <div className="memory-section">
        <div className="memory-section-title">Core Memory</div>
        {MEMORY_TYPES.map(t => (
          <div key={t.key} className="memory-item" onClick={() => startEdit(t.key, memory[t.key])}>
            <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
              <Icon name={t.icon} size={20} style={{ color: "var(--accent2)" }} />
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, marginBottom: 2 }}>{t.label}</div>
                <div style={{ fontSize: 13, color: "var(--text2)" }}>{t.desc}</div>
                {memory[t.key] && (
                  <div className="memory-preview">{memory[t.key].slice(0, 120)}{memory[t.key].length > 120 ? "..." : ""}</div>
                )}
              </div>
            </div>
            <button className="btn btn-ghost btn-sm">Edit <Icon name="chevron" size={14} /></button>
          </div>
        ))}
      </div>

      <div className="memory-section">
        <div className="memory-section-title">Components</div>
        {components.map(c => (
          <div key={c.name} className="memory-item" onClick={() => startEdit(`comp:${c.name}`, c.content)}>
            <div style={{ display: "flex", gap: 12, alignItems: "flex-start" }}>
              <Icon name="split" size={20} style={{ color: "var(--accent2)" }} />
              <div style={{ flex: 1 }}>
                <div style={{ fontWeight: 600, marginBottom: 2 }}>{c.name}</div>
                {c.content && (
                  <div className="memory-preview">{c.content.slice(0, 100)}{c.content.length > 100 ? "..." : ""}</div>
                )}
              </div>
            </div>
            <button className="btn btn-ghost btn-sm">Edit <Icon name="chevron" size={14} /></button>
          </div>
        ))}
        <div className="add-component">
          <input
            value={newComp}
            onChange={e => setNewComp(e.target.value)}
            placeholder="New component name..."
            onKeyDown={e => e.key === "Enter" && addComponent()}
          />
          <button className="btn btn-ghost btn-sm" onClick={addComponent}>Add</button>
        </div>
      </div>
    </div>
  );
}
