import Icon, { ProjectMark } from "./Icon";

// Desktop: a card in a grid. Phone: the same markup as a list row —
// mark · name/description · status · chevron (last session, dates and
// the action buttons hide; the row opens the project, where Start lives).
export default function ProjectCard({ project, onStart, onPause, onDelete, onClick }) {
  const status = project.status || "stopped";
  const lastSession = project.last_session_summary || "No sessions yet";

  return (
    <div className="project-card" onClick={onClick}>
      <div className="project-card-top">
        <ProjectMark name={project.name} size={40} />
        <div className="project-card-titles">
          <div className="project-card-name">{project.name}</div>
          {project.description && <div className="project-card-desc">{project.description}</div>}
        </div>
        <div className="project-card-side">
          <span className={`tag tag-${status}`}>{status}</span>
          {onDelete && status !== "running" && (
            <button className="card-delete" title="Delete project" aria-label="Delete project" onClick={onDelete}>
              <Icon name="trash" size={16} />
            </button>
          )}
        </div>
        <Icon name="chevron" size={18} className="project-card-chev" />
      </div>
      <div className="project-card-last">
        <span style={{ color: "var(--text3)", fontSize: 12 }}>Last session</span>
        <span style={{ fontSize: 13, color: "var(--text2)", marginTop: 2 }}>{lastSession}</span>
      </div>
      <div className="project-card-actions">
        {status !== "running" ? (
          <button className="btn btn-primary btn-sm" onClick={onStart}><Icon name="play" size={14} /> Start</button>
        ) : (
          <button className="btn btn-ghost btn-sm" onClick={onPause}><Icon name="pause" size={14} /> Pause</button>
        )}
        <span style={{ fontSize: 12, color: "var(--text3)" }}>
          {project.created ? new Date(project.created).toLocaleDateString() : ""}
        </span>
      </div>
    </div>
  );
}
