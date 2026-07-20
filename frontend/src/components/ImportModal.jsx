import { useState, useEffect } from "react";
import { api } from "../api";

export default function ImportModal({ onClose, onImported }) {
  const [candidates, setCandidates] = useState(null); // null = loading
  const [selected, setSelected] = useState({}); // path -> bool
  const [edits, setEdits] = useState({}); // path -> { name }
  const [importing, setImporting] = useState(false);

  useEffect(() => {
    api.getImportCandidates().then(list => {
      setCandidates(list);
      const sel = {};
      list.forEach(c => { sel[c.path] = true; });
      setSelected(sel);
    }).catch(() => setCandidates([]));
  }, []);

  const toggle = (path) => setSelected(s => ({ ...s, [path]: !s[path] }));
  const setName = (path, name) => setEdits(e => ({ ...e, [path]: { ...e[path], name } }));

  const chosen = (candidates || []).filter(c => selected[c.path]);

  const doImport = async () => {
    if (!chosen.length) return;
    setImporting(true);
    try {
      const payload = chosen.map(c => ({
        path: c.path,
        name: (edits[c.path]?.name ?? c.name).trim() || c.name,
        description: c.description,
        emoji: c.emoji,
      }));
      await api.importProjects(payload);
      onImported();
    } catch (_) {
      setImporting(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal import-modal">
        <h2>Import existing folders</h2>
        <div className="import-subtitle">
          Folders found in <code>~/code/my-claude/</code> that aren't projects yet. Uncheck any you don't want.
        </div>

        {candidates === null ? (
          <div className="import-empty">Scanning folders...</div>
        ) : candidates.length === 0 ? (
          <div className="import-empty">🎉 Every folder is already a project — nothing to import.</div>
        ) : (
          <div className="import-list">
            {candidates.map(c => (
              <div className={"import-row" + (selected[c.path] ? " checked" : "")} key={c.path}>
                <input
                  type="checkbox"
                  checked={!!selected[c.path]}
                  onChange={() => toggle(c.path)}
                />
                <span className="import-emoji" onClick={() => toggle(c.path)}>{c.emoji}</span>
                <div className="import-row-main">
                  <input
                    className="import-name"
                    value={edits[c.path]?.name ?? c.name}
                    onChange={e => setName(c.path, e.target.value)}
                    disabled={!selected[c.path]}
                  />
                  <div className="import-desc">{c.description || c.path}</div>
                </div>
              </div>
            ))}
          </div>
        )}

        <div className="modal-actions">
          <button className="btn btn-ghost" onClick={onClose}>Cancel</button>
          <button
            className="btn btn-primary"
            onClick={doImport}
            disabled={importing || chosen.length === 0}
          >
            {importing ? "Importing..." : `Import ${chosen.length} project${chosen.length !== 1 ? "s" : ""}`}
          </button>
        </div>
      </div>
    </div>
  );
}
