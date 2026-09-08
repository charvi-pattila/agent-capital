import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../api";
import Chat from "../components/Chat";
import MemoryPanel from "../components/MemoryPanel";
import TestPanel from "../components/TestPanel";
import PreviewPanel from "../components/PreviewPanel";
import AboutPanel from "../components/AboutPanel";
import CouncilPanel from "../components/CouncilPanel";
import SplitPanel from "../components/SplitPanel";
import ContextGauge from "../components/ContextGauge";
import Icon, { ProjectMark } from "../components/Icon";

const TABS = [
  { key: "Chat", icon: "terminal" },
  { key: "Split", icon: "split" },
  { key: "Memory", icon: "brain" },
  { key: "Testing", icon: "flask" },
  { key: "Preview", icon: "eye" },
  { key: "Council", icon: "scale" },
  { key: "About", icon: "info" },
];

export default function Project() {
  const { id } = useParams();
  const navigate = useNavigate();
  const [project, setProject] = useState(null);
  const [tab, setTab] = useState("Chat");
  const [loading, setLoading] = useState(true);

  const load = () => api.getProject(id).then(setProject).catch(() => navigate("/")).finally(() => setLoading(false));

  useEffect(() => {
    load();
    api.markSeen(id).catch(() => {});
    const t = setInterval(() => api.markSeen(id).catch(() => {}), 10000);
    return () => clearInterval(t);
  }, [id]);

  const handleStart = async () => { await api.startProject(id); load(); };
  const handlePause = async () => { await api.pauseProject(id); load(); };

  if (loading) return <div style={{ color: "var(--text2)", textAlign: "center", paddingTop: 60 }}>Loading...</div>;
  if (!project) return null;

  const status = project.status || "stopped";

  return (
    <div className="project-view">
      {/* On the phone this collapses to: back · status dot · name · gauge · Start/Pause
          (the subtitle, mark and status tag hide via CSS). */}
      <div className="project-header">
        <div className="project-header-main" style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
          <button className="btn btn-ghost btn-sm btn-icon" onClick={() => navigate("/")} title="Back to projects" aria-label="Back">
            <Icon name="back" size={18} />
          </button>
          <ProjectMark name={project.name} size={40} />
          <span className={`status-dot ${status}`} />
          <div style={{ minWidth: 0 }}>
            <div className="page-title">{project.name}</div>
            <div className="page-subtitle">{project.description || "No description"}</div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center", flexShrink: 0 }}>
          {status === "running" && <ContextGauge projectId={id} initial={project.context} />}
          <span className={`tag tag-${status}`}>{status}</span>
          {status !== "running" ? (
            <button className="btn btn-primary btn-sm" onClick={handleStart}><Icon name="play" size={14} /> Start</button>
          ) : (
            <button className="btn btn-ghost btn-sm" onClick={handlePause}><Icon name="pause" size={14} /> Pause</button>
          )}
        </div>
      </div>

      <div className="project-tabs">
        {TABS.map(t => (
          <button
            key={t.key}
            className={"tab-btn" + (tab === t.key ? " active" : "")}
            onClick={() => setTab(t.key)}
          >
            <Icon name={t.icon} size={15} />
            {t.key}
          </button>
        ))}
      </div>

      <div className="tab-content">
        {tab === "Chat" && <Chat projectId={id} status={status} />}
        {tab === "Split" && <SplitPanel projectId={id} />}
        {tab === "Memory" && <MemoryPanel projectId={id} />}
        {tab === "Testing" && <TestPanel projectId={id} />}
        {tab === "Preview" && (
          <PreviewPanel
            projectId={id}
            previewUrl={project.preview_url}
            previewFile={project.preview_file}
            serverCmd={project.server_cmd}
            serverRunning={project.server_running}
            onSaved={load}
          />
        )}
        {tab === "Council" && <CouncilPanel projectId={id} />}
        {tab === "About" && <AboutPanel project={project} onSaved={load} />}
      </div>
    </div>
  );
}
