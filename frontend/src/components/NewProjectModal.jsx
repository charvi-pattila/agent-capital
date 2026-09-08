import { useState } from "react";

export default function NewProjectModal({ onClose, onCreate }) {
  const [form, setForm] = useState({
    name: "",
    description: "",
    path: "",
    first_message: "",
    server_cmd: "",
    auto_start: true,
  });

  const set = (k) => (e) => setForm(f => ({ ...f, [k]: e.target.value }));

  const submit = (e) => {
    e.preventDefault();
    if (!form.name.trim()) return;
    onCreate(form);
  };

  return (
    <div className="modal-overlay" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal">
        <h2>New Project</h2>
        <form onSubmit={submit}>
          <div className="form-group">
            <label>Project Name *</label>
            <input value={form.name} onChange={set("name")} placeholder="e.g. Portfolio Website" autoFocus required />
          </div>
          <div className="form-group">
            <label>Description</label>
            <input value={form.description} onChange={set("description")} placeholder="What is this project?" />
          </div>
          <div className="form-group">
            <label>Project Path (optional)</label>
            <input value={form.path} onChange={set("path")} placeholder="Leave blank to save under ~/code/my-claude/<project-name>" />
            <div className="form-hint">The folder is created for you if it doesn't exist yet.</div>
          </div>
          <div className="form-group">
            <label>Opening message for Claude (optional)</label>
            <textarea value={form.first_message} onChange={set("first_message")} placeholder="Tell Claude what to do first..." />
          </div>
          <div className="form-group">
            <label>Dev server command (optional)</label>
            <input value={form.server_cmd} onChange={set("server_cmd")} placeholder="e.g. npm run dev" />
            <div className="form-hint">Runs automatically in the project folder whenever you Start this project — set the Preview tab's URL to match once it's running.</div>
          </div>
          <label className="form-check">
            <input
              type="checkbox"
              checked={form.auto_start}
              onChange={e => setForm(f => ({ ...f, auto_start: e.target.checked }))}
            />
            Start a Claude session right away
          </label>
          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn btn-primary">Create Project</button>
          </div>
        </form>
      </div>
    </div>
  );
}
