import { useState, useEffect } from "react";
import { api } from "../api";
import SheetViewer from "./SheetViewer";

const IMG_EXT = ["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp", "avif"];
const FRAME_EXT = ["html", "htm", "pdf"];
const VIDEO_EXT = ["mp4", "webm", "m4v", "mov"];
const AUDIO_EXT = ["mp3", "wav", "m4a", "ogg", "flac"];
const SHEET_EXT = ["csv", "tsv", "xlsx", "xlsm", "xls"];
const TEXT_EXT = [
  "md", "txt", "json", "log", "js", "jsx", "ts", "tsx", "py", "css", "scss",
  "yml", "yaml", "toml", "sh", "sql", "xml", "ini", "cfg", "env",
];

const extOf = (p) => (p.includes(".") ? p.split(".").pop().toLowerCase() : "");
const fsUrl = (projectId, path) =>
  `/api/projects/${projectId}/fs/` + path.split("/").map(encodeURIComponent).join("/");

const fmtSize = (n) => {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
};

export default function PreviewPanel({ projectId, previewUrl, previewFile, serverCmd, serverRunning, onSaved }) {
  const hasTarget = !!(previewFile || previewUrl);
  const [editing, setEditing] = useState(!hasTarget);
  const [mode, setMode] = useState(previewFile ? "file" : "url");
  const [url, setUrl] = useState(previewUrl || "");
  const [cmd, setCmd] = useState(serverCmd || "");
  const [saving, setSaving] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    setUrl(previewUrl || "");
    setCmd(serverCmd || "");
    setMode(previewFile ? "file" : "url");
    setEditing(!(previewFile || previewUrl));
  }, [previewUrl, previewFile, serverCmd]);

  const isSelfUrl = (u) => {
    try {
      return new URL(u).origin === window.location.origin;
    } catch (_) {
      return false;
    }
  };

  const saveUrl = async (e) => {
    e.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;
    setSaving(true);
    await api.updateProject(projectId, { preview_url: trimmed, preview_file: "", server_cmd: cmd.trim() });
    setSaving(false);
    setEditing(false);
    onSaved && onSaved();
  };

  const saveFile = async (relPath) => {
    setSaving(true);
    await api.updateProject(projectId, { preview_file: relPath, preview_url: "", server_cmd: cmd.trim() });
    setSaving(false);
    setEditing(false);
    onSaved && onSaved();
  };

  if (editing) {
    return (
      <div className="memory-editor">
        <div style={{ fontWeight: 600, marginBottom: 12 }}>Preview source</div>
        <div className="preview-toggle">
          <button
            type="button"
            className={"seg-btn" + (mode === "url" ? " active" : "")}
            onClick={() => setMode("url")}
          >🌐 URL</button>
          <button
            type="button"
            className={"seg-btn" + (mode === "file" ? " active" : "")}
            onClick={() => setMode("file")}
          >📄 Project file</button>
          {hasTarget && (
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              style={{ marginLeft: "auto" }}
              onClick={() => setEditing(false)}
            >Cancel</button>
          )}
        </div>

        {mode === "url" ? (
          <>
            <div style={{ fontSize: 13, color: "var(--text2)", marginBottom: 12 }}>
              The URL where <em>this project's</em> app runs (its own dev server or port) — it will be embedded below.
            </div>
            <form onSubmit={saveUrl} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <div className="form-group" style={{ marginBottom: 0 }}>
                <label>Dev server command (optional)</label>
                <input
                  value={cmd}
                  onChange={e => setCmd(e.target.value)}
                  placeholder="e.g. npm run dev"
                />
                <div className="form-hint">Runs automatically in the project folder whenever you Start this project, so it's already live here.</div>
              </div>
              <div style={{ display: "flex", gap: 10 }}>
                <input
                  style={{ flex: 1 }}
                  value={url}
                  onChange={e => setUrl(e.target.value)}
                  placeholder="e.g. http://localhost:4173"
                  autoFocus
                />
                <button className="btn btn-primary btn-sm" type="submit" disabled={saving || !url.trim()}>
                  {saving ? "Saving..." : "Save"}
                </button>
              </div>
            </form>
            {isSelfUrl(url.trim()) && (
              <div className="preview-warning">
                ⚠️ That's Claude Manager's own URL — you'd be previewing this dashboard inside itself.
                Enter the project app's URL instead.
              </div>
            )}
          </>
        ) : (
          <>
            <div style={{ fontSize: 13, color: "var(--text2)", marginBottom: 12 }}>
              Pick a file from the project folder — spreadsheets, HTML, images, PDFs, markdown…
              it will be rendered below.
            </div>
            <FileBrowser projectId={projectId} onPick={saveFile} saving={saving} />
          </>
        )}
      </div>
    );
  }

  const openHref = previewFile ? fsUrl(projectId, previewFile) : previewUrl;

  return (
    <div className="terminal-container">
      {!previewFile && isSelfUrl(previewUrl) && (
        <div className="preview-warning banner">
          ⚠️ This preview URL points at Claude Manager itself. Click Edit and set it to the project app's own URL.
        </div>
      )}
      <div className="terminal-header">
        <span className="terminal-title" style={{ flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {previewFile ? `📄 ${previewFile}` : previewUrl}
        </span>
        {!previewFile && serverCmd && (
          <span className={`terminal-status ${serverRunning ? "online" : "offline"}`}>
            {serverRunning ? "● server running" : "○ server stopped"}
          </span>
        )}
        <button className="btn btn-ghost btn-sm" onClick={() => setReloadKey(k => k + 1)}>↻ Refresh</button>
        <button className="btn btn-ghost btn-sm" onClick={() => setEditing(true)}>✎ Edit</button>
        <a className="btn btn-ghost btn-sm" href={openHref} target="_blank" rel="noreferrer">Open ↗</a>
      </div>
      {previewFile ? (
        <FileViewer key={reloadKey} projectId={projectId} path={previewFile} reloadKey={reloadKey} />
      ) : (
        <iframe
          key={reloadKey}
          src={previewUrl}
          title="Project preview"
          style={{ width: "100%", flex: 1, minHeight: 0, border: "none", background: "#fff" }}
        />
      )}
    </div>
  );
}

// ── File browser (edit mode) ──────────────────────────────────────────────────

function FileBrowser({ projectId, onPick, saving }) {
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    setError("");
    api.listFiles(projectId, path)
      .then(d => {
        if (d.error) setError(d.error);
        setEntries(d.entries || []);
      })
      .catch(() => setError("Could not read folder"));
  }, [projectId, path]);

  const crumbs = path ? path.split("/") : [];

  return (
    <div className="file-browser">
      <div className="file-crumbs">
        <button type="button" className="crumb-btn" onClick={() => setPath("")}>project</button>
        {crumbs.map((c, i) => (
          <span key={i}>
            {" / "}
            <button type="button" className="crumb-btn" onClick={() => setPath(crumbs.slice(0, i + 1).join("/"))}>
              {c}
            </button>
          </span>
        ))}
      </div>
      <div className="file-list">
        {error && <div className="preview-center">{error}</div>}
        {!error && entries.map(e => {
          const p = path ? `${path}/${e.name}` : e.name;
          return e.dir ? (
            <button type="button" key={e.name} className="file-row" onClick={() => setPath(p)}>
              📁 {e.name}
            </button>
          ) : (
            <button type="button" key={e.name} className="file-row" disabled={saving} onClick={() => onPick(p)}>
              📄 {e.name}
              <span className="file-size">{fmtSize(e.size)}</span>
            </button>
          );
        })}
        {!error && !entries.length && <div className="preview-center">Empty folder</div>}
      </div>
    </div>
  );
}

// ── File viewers ──────────────────────────────────────────────────────────────

function FileViewer({ projectId, path, reloadKey }) {
  const ext = extOf(path);
  const url = fsUrl(projectId, path) + `?v=${reloadKey}`;

  if (FRAME_EXT.includes(ext)) {
    return (
      <iframe
        src={url}
        title="Project preview"
        style={{ width: "100%", flex: 1, minHeight: 0, border: "none", background: "#fff" }}
      />
    );
  }
  if (IMG_EXT.includes(ext)) {
    return (
      <div className="preview-body">
        <img className="preview-img" src={url} alt={path} />
      </div>
    );
  }
  if (VIDEO_EXT.includes(ext)) {
    return (
      <div className="preview-body">
        <video src={url} controls style={{ width: "100%", maxHeight: "100%" }} />
      </div>
    );
  }
  if (AUDIO_EXT.includes(ext)) {
    return (
      <div className="preview-body">
        <div className="preview-center">
          <audio src={url} controls />
        </div>
      </div>
    );
  }
  if (SHEET_EXT.includes(ext)) return <SheetViewer url={url} ext={ext} />;
  if (TEXT_EXT.includes(ext) || !ext) return <TextViewer url={url} />;

  return (
    <div className="preview-body">
      <div className="preview-center">
        <div>Can't render <code>.{ext}</code> files inline.</div>
        <a className="btn btn-ghost btn-sm" href={url} target="_blank" rel="noreferrer">Open / download ↗</a>
      </div>
    </div>
  );
}

function TextViewer({ url }) {
  const [text, setText] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    fetch(url)
      .then(r => (r.ok ? r.text() : Promise.reject()))
      .then(t => {
        if (cancelled) return;
        setText(t.length > 300000 ? t.slice(0, 300000) + "\n… (truncated)" : t);
      })
      .catch(() => !cancelled && setError("Could not load file"));
    return () => { cancelled = true; };
  }, [url]);

  return (
    <div className="preview-body">
      {error ? (
        <div className="preview-center">{error}</div>
      ) : text === null ? (
        <div className="preview-center">Loading…</div>
      ) : (
        <pre className="preview-text">{text}</pre>
      )}
    </div>
  );
}

