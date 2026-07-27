import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { api } from "../api";
import Chat from "../components/Chat";
import MemoryPanel from "../components/MemoryPanel";
import TestPanel from "../components/TestPanel";
import PreviewPanel from "../components/PreviewPanel";
import AboutPanel from "../components/AboutPanel";
import CouncilPanel from "../components/CouncilPanel";

const TABS = ["Chat", "Memory", "Testing", "Preview", "Council", "About"];

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
      <div className="project-header">
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <button className="btn btn-ghost btn-sm" onClick={() => navigate("/")}>← Back</button>
          <span style={{ fontSize: 28 }}>{project.emoji}</span>
          <div>
            <div className="page-title">{project.name}</div>
            <div className="page-subtitle">{project.description || "No description"}</div>
          </div>
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          <span className={`tag tag-${status}`}>{status}</span>
          {status !== "running" ? (
            <button className="btn btn-primary btn-sm" onClick={handleStart}>▶ Start</button>
          ) : (
            <button className="btn btn-ghost btn-sm" onClick={handlePause}>⏸ Pause</button>
          )}
        </div>
      </div>

      {project.limit_paused && (
        <div className="limit-banner">
          ⏳ Paused for Claude usage limit — progress saved. Auto-resuming at{" "}
          <b>{new Date(project.limit_resume_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}</b>
          {" "}(you'll get a text).
        </div>
      )}

      <div className="project-tabs">
        {TABS.map(t => (
          <button
            key={t}
            className={"tab-btn" + (tab === t ? " active" : "")}
            onClick={() => setTab(t)}
          >
            {t}
          </button>
        ))}
      </div>

      <div className="tab-content">
        {tab === "Chat" && <Chat projectId={id} status={status} />}
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
