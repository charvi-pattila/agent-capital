import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import ProjectCard from "../components/ProjectCard";
import NewProjectModal from "../components/NewProjectModal";
import ImportModal from "../components/ImportModal";
import Icon from "../components/Icon";

export default function Dashboard() {
  const [projects, setProjects] = useState([]);
  const [showModal, setShowModal] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const navigate = useNavigate();

  const load = () => api.getProjects().then(setProjects).finally(() => setLoading(false));

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, []);

  const handleCreate = async (data) => {
    // Surface failures instead of swallowing them: a 401 (logged out), a 500,
    // or a network error used to leave the modal open with no feedback at all.
    let proj;
    try {
      proj = await api.createProject(data);
    } catch (err) {
      throw new Error(`Could not reach the server (${err.message}).`);
    }
    if (!proj || !proj.id) {
      throw new Error(proj && proj.error ? `Server said: ${proj.error}` : "Server returned no project.");
    }
    setShowModal(false);
    navigate(`/project/${proj.id}`);
  };

  const handleStart = async (id, e) => {
    e.preventDefault();
    await api.startProject(id);
    load();
  };

  const handlePause = async (id, e) => {
    e.preventDefault();
    await api.pauseProject(id);
    load();
  };

  const handleDelete = async (project, e) => {
    e.stopPropagation();
    const ok = window.confirm(
      `Delete "${project.name}" from the dashboard?\n\nThis removes its chat history, memory and tests. The code folder on disk is NOT touched.`
    );
    if (!ok) return;
    await api.deleteProject(project.id);
    load();
  };

  const q = query.trim().toLowerCase();
  const rank = (p) => (p.status === "running" ? 0 : p.status === "paused" ? 1 : 2);
  const visible = (q
    ? projects.filter(p =>
        (p.name || "").toLowerCase().includes(q) ||
        (p.description || "").toLowerCase().includes(q))
    : projects
  ).slice().sort((a, b) => rank(a) - rank(b));

  return (
    <div>
      <div className="page-header">
        <div>
          <div className="page-title">Projects</div>
          <div className="page-subtitle">{projects.length} project{projects.length !== 1 ? "s" : ""}</div>
        </div>
        <div className="page-header-actions" style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {projects.length > 0 && (
            <input
              className="search-input"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Search projects…"
            />
          )}
          <button className="btn btn-ghost" onClick={() => setShowImport(true)} title="Import folders">
            <Icon name="folder" size={16} /><span className="btn-label">Import folders</span>
          </button>
          <button className="btn btn-primary" onClick={() => setShowModal(true)} title="New project">
            <Icon name="plus" size={16} /><span className="btn-label">New Project</span>
          </button>
        </div>
      </div>

      {loading ? (
        <div style={{ color: "var(--text2)", textAlign: "center", paddingTop: 60 }}>Loading...</div>
      ) : projects.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon"><Icon name="spark" size={40} /></div>
          <div style={{ fontSize: 18, fontWeight: 600, marginBottom: 8 }}>No projects yet</div>
          <div style={{ color: "var(--text2)", marginBottom: 24 }}>Create a project or import folders you already have</div>
          <div style={{ display: "flex", gap: 10, justifyContent: "center" }}>
            <button className="btn btn-ghost" onClick={() => setShowImport(true)}><Icon name="folder" size={16} /> Import folders</button>
            <button className="btn btn-primary" onClick={() => setShowModal(true)}><Icon name="plus" size={16} /> New Project</button>
          </div>
        </div>
      ) : visible.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon"><Icon name="search" size={30} /></div>
          <div style={{ color: "var(--text2)" }}>No projects match "{query}"</div>
        </div>
      ) : (
        <div className="project-grid">
          {visible.map(p => (
            <ProjectCard
              key={p.id}
              project={p}
              onStart={(e) => handleStart(p.id, e)}
              onPause={(e) => handlePause(p.id, e)}
              onDelete={(e) => handleDelete(p, e)}
              onClick={() => navigate(`/project/${p.id}`)}
            />
          ))}
        </div>
      )}

      {showModal && (
        <NewProjectModal
          onClose={() => setShowModal(false)}
          onCreate={handleCreate}
        />
      )}
      {showImport && (
        <ImportModal
          onClose={() => setShowImport(false)}
          onImported={() => { setShowImport(false); load(); }}
        />
      )}
    </div>
  );
}
