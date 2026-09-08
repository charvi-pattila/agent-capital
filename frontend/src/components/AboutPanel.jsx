import { useEffect, useState } from "react";
import QRCode from "qrcode";
import { api } from "../api";

/* "Install as an app" card. Shows the address the phone should bookmark:
   - public_url when the backend knows one (PUBLIC_URL in backend/.env, or the
     machine's Tailscale https name). Works from anywhere, never changes.
   - otherwise the LAN name. On a Mac that's the .local (Bonjour) name, which
     keeps resolving when the router hands out a new Wi-Fi IP; a raw
     192.168.x.x link goes stale. */
function InstallCard() {
  const [info, setInfo] = useState(null);
  const [failed, setFailed] = useState(false);
  const [qr, setQr] = useState("");

  useEffect(() => {
    fetch("/api/hostname")
      .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setInfo)
      .catch(() => setFailed(true));
  }, []);

  const proto = typeof window !== "undefined" ? window.location.protocol : "http:";
  const lanUrl = info ? `${proto}//${info.local_hostname}:${info.port}` : "";
  const url = info?.public_url || lanUrl;
  const isMac = !info?.platform || info.platform === "macos"; // older backend: no platform field
  const machine = isMac ? "the Mac" : "the server";

  useEffect(() => {
    if (!url) return;
    QRCode.toDataURL(url, { margin: 0, width: 280, errorCorrectionLevel: "M", color: { dark: "#0d0d11", light: "#ffffff" } })
      .then(setQr)
      .catch(() => setQr(""));
  }, [url]);

  return (
    <div className="install-card">
      {qr ? (
        <img className="install-qr" src={qr} alt={`QR code for ${url}`} />
      ) : (
        <div className="install-qr-placeholder">{failed ? "QR unavailable" : "Loading…"}</div>
      )}
      <div className="install-main">
        {url ? (
          <a className="install-url" href={url}>{url}</a>
        ) : (
          <span className="install-note" style={{ color: "var(--text3)" }}>
            {failed ? "Couldn't read the server's address from the backend." : "Looking up this machine's address…"}
          </span>
        )}
        {info?.public_url ? (
          <p className="install-note">
            Scan the code or open that address from anywhere — it goes over Tailscale, so the phone
            needs the Tailscale app signed in to the same account, but not the same Wi-Fi.
            {lanUrl ? <> On the home network <code>{lanUrl}</code> also works.</> : null}
          </p>
        ) : isMac ? (
          <p className="install-note">
            Scan the code or open that address on the same Wi-Fi. It uses the Mac's <code>.local</code> name,
            so it keeps working when the router hands the Mac a new IP
            {info?.lan_ip ? <> (currently <code>{info.lan_ip}</code>)</> : null}, unlike a link to the raw IP.
          </p>
        ) : (
          <p className="install-note">
            No public address is configured yet. Set <code>PUBLIC_URL</code> in <code>backend/.env</code>
            (or sign the server into Tailscale) — see <code>docs/WINDOWS-SERVER.md</code>. Until then this
            address only works from the server itself
            {info?.lan_ip ? <> (LAN IP <code>{info.lan_ip}</code>)</> : null}.
          </p>
        )}
        <div className="install-steps">
          <div className="install-step">
            <strong>iPhone:</strong> open the address in Safari, tap Share, then <strong>Add to Home Screen</strong>.
            <br />It launches full-screen like a native app.
          </div>
          <div className="install-step">
            <strong>Mac:</strong> open it in Safari, then <strong>File → Add to Dock</strong>.
            <br />It gets its own window and Dock icon.
          </div>
        </div>
      </div>
    </div>
  );
}

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

      <div className="memory-section">
        <div className="memory-section-title">Install as an app</div>
        <InstallCard />
      </div>
    </div>
  );
}
