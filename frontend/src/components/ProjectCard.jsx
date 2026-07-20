export default function ProjectCard({ project, onStart, onPause, onDelete, onClick }) {
  const status = project.status || "stopped";
  const lastSession = project.last_session_summary || "No sessions yet";

  return (
    <div className="project-card" onClick={onClick}>
      <div className="project-card-header">
        <span className="project-emoji">{project.emoji}</span>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span className={`tag tag-${status}`}>{status}</span>
          {onDelete && status !== "running" && (
            <button className="card-delete" title="Delete project" onClick={onDelete}>🗑</button>
          )}
        </div>
      </div>
      <div className="project-card-name">{project.name}</div>
      {project.description && (
        <div className="project-card-desc">{project.description}</div>
      )}
      <div className="project-card-last">
        <span style={{ color: "var(--text3)", fontSize: 12 }}>Last session</span>
        <span style={{ fontSize: 13, color: "var(--text2)", marginTop: 2 }}>{lastSession}</span>
      </div>
      <div className="project-card-actions">
        {status !== "running" ? (
          <button className="btn btn-primary btn-sm" onClick={onStart}>▶ Start</button>
        ) : (
          <button className="btn btn-ghost btn-sm" onClick={onPause}>⏸ Pause</button>
        )}
        <span style={{ fontSize: 12, color: "var(--text3)" }}>
          {project.created ? new Date(project.created).toLocaleDateString() : ""}
        </span>
      </div>
    </div>
  );
}
