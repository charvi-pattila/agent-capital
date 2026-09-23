import { useState, useEffect } from "react";
import Icon from "./Icon";
import { api } from "../api";

// Phone notifications. The browser subscribes to its push service with the
// server's VAPID public key and hands the subscription to the backend, which
// then sends "Hey <name>, Agent Capital: <project> needs your response"
// whenever a session finishes a reply or sits on a prompt (server.py
// notify_phone). On iPhone this only works from the app installed to the Home
// Screen, and permission can only be requested from a tap - hence a modal
// with an explicit button rather than asking on load.

const urlBase64ToUint8Array = (base64) => {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const raw = window.atob((base64 + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
};

const supported = () => "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
const isIOS = () => /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isInstalled = () => window.matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;

const deviceLabel = () => {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android phone";
  if (/Macintosh/.test(ua)) return "Mac";
  if (/Windows/.test(ua)) return "Windows PC";
  return "Browser";
};

export default function AlertsModal({ onClose }) {
  const [state, setState] = useState(null); // server: { available, public_key, name, devices }
  const [name, setName] = useState("");
  const [current, setCurrent] = useState(null); // this browser's PushSubscription
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState(null); // { kind: "ok"|"err", text }

  const refresh = () => api.getPushState().then((s) => {
    setState(s);
    setName((prev) => prev || s.name || "");
  }).catch(() => setState({ available: false }));

  useEffect(() => {
    refresh();
    if (supported()) {
      navigator.serviceWorker.ready
        .then((reg) => reg.pushManager.getSubscription())
        .then((sub) => setCurrent(sub))
        .catch(() => {});
    }
  }, []);

  const thisDeviceOn = !!current && !!state?.devices?.some((d) => d.endpoint === current.endpoint);

  const enable = async () => {
    setBusy(true);
    setNote(null);
    try {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        throw new Error(permission === "denied"
          ? "Notifications are blocked for this app. Allow them in the phone's Settings > Notifications, then try again."
          : "Permission was not granted.");
      }
      const reg = await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();
      if (!sub) {
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: urlBase64ToUint8Array(state.public_key),
        });
      }
      const res = await api.subscribePush({ subscription: sub.toJSON(), name: name.trim(), label: deviceLabel() });
      if (res.error) throw new Error(res.error);
      setCurrent(sub);
      await refresh();
      setNote({ kind: "ok", text: "Alerts are on for this device. Send a test to check it arrives." });
    } catch (err) {
      setNote({ kind: "err", text: err.message || String(err) });
    } finally {
      setBusy(false);
    }
  };

  const disable = async () => {
    setBusy(true);
    setNote(null);
    try {
      if (current) {
        await api.unsubscribePush(current.endpoint);
        await current.unsubscribe().catch(() => {});
      }
      setCurrent(null);
      await refresh();
      setNote({ kind: "ok", text: "Alerts are off for this device." });
    } catch (err) {
      setNote({ kind: "err", text: err.message || String(err) });
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    setBusy(true);
    setNote(null);
    try {
      const res = await api.testPush(current ? current.endpoint : undefined);
      if (res.error) throw new Error(res.error);
      setNote({ kind: "ok", text: "Test sent. It can take a few seconds to show up." });
    } catch (err) {
      setNote({ kind: "err", text: `Could not send: ${err.message || err}` });
    } finally {
      setBusy(false);
    }
  };

  const saveName = async () => {
    setBusy(true);
    try {
      await api.savePushSettings({ name: name.trim() });
      await refresh();
      setNote({ kind: "ok", text: "Name saved." });
    } finally {
      setBusy(false);
    }
  };

  const preview = `Hey${name.trim() ? " " + name.trim() : ""}, Agent Capital`;

  let blocker = null;
  if (state && !state.available) {
    blocker = "The server is missing the push library. Run pip install -r backend/requirements.txt there and restart it.";
  } else if (!supported()) {
    blocker = isIOS() && !isInstalled()
      ? "On iPhone, alerts only work from the installed app. In Safari tap Share, then Add to Home Screen, and open Agent Capital from there."
      : "This browser does not support push notifications.";
  } else if (window.location.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(window.location.hostname)) {
    blocker = "Push needs an https address. Open the app through its Tailscale URL.";
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal alerts-modal" onClick={(e) => e.stopPropagation()}>
        <h2><Icon name="bell" size={18} /> Phone alerts</h2>
        <div className="alerts-intro">
          Get a notification when a project is waiting on you. It looks like:
        </div>
        <div className="alerts-preview">
          <div className="alerts-preview-title">{preview}</div>
          <div className="alerts-preview-body">Lucky Planner needs your response: Do you want to proceed? (y/n)</div>
        </div>

        <div className="form-group">
          <label>Your name (for the greeting)</label>
          <div style={{ display: "flex", gap: 8 }}>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Charvi" />
            {state?.name !== name.trim() && (
              <button type="button" className="btn btn-ghost" onClick={saveName} disabled={busy}>Save</button>
            )}
          </div>
        </div>

        {blocker ? (
          <div className="form-error">{blocker}</div>
        ) : !state ? (
          <div style={{ color: "var(--text2)" }}>Loading…</div>
        ) : (
          <div className="alerts-device">
            <div>
              <div style={{ fontWeight: 600 }}>This device ({deviceLabel()})</div>
              <div style={{ color: "var(--text2)", fontSize: 13 }}>
                {thisDeviceOn ? "Alerts are on" : "Alerts are off"}
              </div>
            </div>
            {thisDeviceOn ? (
              <div style={{ display: "flex", gap: 8 }}>
                <button type="button" className="btn btn-ghost" onClick={test} disabled={busy}>Send test</button>
                <button type="button" className="btn btn-ghost" onClick={disable} disabled={busy}>Turn off</button>
              </div>
            ) : (
              <button type="button" className="btn btn-primary" onClick={enable} disabled={busy || !state.public_key}>
                {busy ? "Working…" : "Turn on"}
              </button>
            )}
          </div>
        )}

        {state?.devices?.length > 0 && (
          <div className="alerts-devices">
            {state.devices.length} device{state.devices.length !== 1 ? "s" : ""} subscribed:{" "}
            {state.devices.map((d) => d.label || "unnamed").join(", ")}
          </div>
        )}

        {note && <div className={note.kind === "err" ? "form-error" : "form-ok"}>{note.text}</div>}

        <div className="modal-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}
