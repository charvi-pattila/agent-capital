import { useState, useEffect, useRef } from "react";
import { api } from "../api";

export default function Chat({ projectId, status }) {
  const [content, setContent] = useState("");
  const [offline, setOffline] = useState(true);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [images, setImages] = useState([]); // [{ file, previewUrl }]
  const [dragOver, setDragOver] = useState(false);
  // "Pinned" = following live output. Scrolling up pauses the feed entirely
  // (frozen text is readable/selectable while Claude streams); the latest
  // content keeps buffering in latestRef and is applied on resume.
  const [pinned, setPinned] = useState(true);
  const termRef = useRef(null);
  const esRef = useRef(null);
  const pinnedRef = useRef(true);
  const latestRef = useRef("");
  const touchingRef = useRef(false);
  const scrollCooldownRef = useRef(false);

  const pauseLive = () => { pinnedRef.current = false; setPinned(false); };
  const resumeLive = () => {
    pinnedRef.current = true;
    setPinned(true);
    setContent(latestRef.current);
    requestAnimationFrame(() => {
      if (termRef.current) termRef.current.scrollTop = termRef.current.scrollHeight;
    });
  };

  useEffect(() => {
    if (esRef.current) esRef.current.close();

    const es = new EventSource(`/api/projects/${projectId}/terminal`);
    esRef.current = es;

    es.onmessage = (e) => {
      const data = JSON.parse(e.data);
      setOffline(!!data.offline);
      if (data.content !== undefined) {
        latestRef.current = data.content;
        if (pinnedRef.current) setContent(data.content);
      }
    };

    es.onerror = () => {
      setTimeout(() => {}, 2000);
    };

    return () => es.close();
  }, [projectId]);

  // Keep the view glued to the bottom while following (runs after render,
  // so scrollHeight reflects the new content)
  useEffect(() => {
    if (!offline && pinnedRef.current && termRef.current) {
      termRef.current.scrollTop = termRef.current.scrollHeight;
    }
  }, [content, offline]);

  // Revoke object URLs on unmount (ref avoids the stale initial-array closure)
  const imagesRef = useRef(images);
  imagesRef.current = images;
  useEffect(() => () => imagesRef.current.forEach(img => URL.revokeObjectURL(img.previewUrl)), []);

  const handleScroll = () => {
    if (!termRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = termRef.current;
    const atBottom = scrollHeight - scrollTop - clientHeight < 60;
    if (atBottom && !pinnedRef.current) resumeLive();
    else if (!atBottom && pinnedRef.current) pauseLive();
  };

  // The pane is now sized tall enough that Claude Code's TUI renders a good
  // chunk of scrollback into one capture, so normal native DOM scrolling
  // handles most of it (real scrollbar, smooth motion). Only once the user
  // hits the very top and keeps trying to go further do we ask the CLI's own
  // TUI to page back further (it has no true scrollback of its own — it's
  // running in tmux's alternate screen buffer — so this is the only way to
  // reveal older history; it says as much in its own UI: "scroll with PgUp/PgDn").
  const handleWheel = (e) => {
    if (offline || !termRef.current) return;
    const atTop = termRef.current.scrollTop <= 0;
    if (!(atTop && e.deltaY < 0)) return; // let native scroll handle everything else
    e.preventDefault();
    if (scrollCooldownRef.current) return;
    scrollCooldownRef.current = true;
    api.scrollTerminal(projectId, "up", 3).finally(() => {
      setTimeout(() => { scrollCooldownRef.current = false; }, 350);
    });
  };

  const addImageFiles = (files) => {
    const imgs = [...files].filter(f => f.type.startsWith("image/"));
    if (!imgs.length) return false;
    setImages(prev => [
      ...prev,
      ...imgs.map(file => ({ file, previewUrl: URL.createObjectURL(file) })),
    ]);
    return true;
  };

  const handlePaste = (e) => {
    if (offline) return;
    const files = [...(e.clipboardData?.items || [])]
      .filter(it => it.kind === "file" && it.type.startsWith("image/"))
      .map(it => it.getAsFile())
      .filter(Boolean);
    if (files.length) {
      e.preventDefault();
      addImageFiles(files);
    }
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setDragOver(false);
    if (offline) return;
    addImageFiles(e.dataTransfer?.files || []);
  };

  const removeImage = (idx) => {
    setImages(prev => {
      URL.revokeObjectURL(prev[idx].previewUrl);
      return prev.filter((_, i) => i !== idx);
    });
  };

  const send = async (e) => {
    e.preventDefault();
    const text = input.trim();
    if ((!text && images.length === 0) || sending) return;
    setSending(true);
    try {
      const paths = [];
      for (const img of images) {
        const res = await api.uploadFile(projectId, img.file);
        if (res.path) paths.push(res.path);
      }
      const parts = [];
      if (text) parts.push(text);
      if (paths.length && !text) parts.push("Look at the attached image(s):");
      paths.forEach(p => parts.push(`[Image attached: ${p}]`));
      await api.sendMessage(projectId, parts.join("\n"));
      setInput("");
      images.forEach(img => URL.revokeObjectURL(img.previewUrl));
      setImages([]);
    } catch (_) {}
    setSending(false);
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(e);
    }
  };

  return (
    <div
      className={"terminal-container" + (dragOver ? " drag-over" : "")}
      onDragOver={(e) => { e.preventDefault(); if (!offline) setDragOver(true); }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setDragOver(false); }}
      onDrop={handleDrop}
    >
      <div className="terminal-header">
        <div className="terminal-dots">
          <span className="dot red" />
          <span className="dot yellow" />
          <span className="dot green" />
        </div>
        <span className="terminal-title">claude — tmux</span>
        <span className={`terminal-status ${offline ? "offline" : "online"}`}>
          {offline ? "● offline" : "● live"}
        </span>
      </div>

      <div
        className="terminal-body"
        ref={termRef}
        onScroll={handleScroll}
        onWheel={handleWheel}
        onTouchStart={() => { touchingRef.current = true; }}
        onTouchEnd={() => { touchingRef.current = false; handleScroll(); }}
      >
        {offline ? (
          <div className="terminal-offline">
            {status === "running"
              ? "Connecting to session..."
              : "Session is stopped. Press Start to launch Claude."}
          </div>
        ) : (
          <pre className="terminal-output">{content}</pre>
        )}
      </div>

      {images.length > 0 && (
        <div className="attach-row">
          {images.map((img, i) => (
            <div className="attach-chip" key={img.previewUrl}>
              <img src={img.previewUrl} alt="attachment" />
              <button
                type="button"
                className="attach-remove"
                title="Remove image"
                onClick={() => removeImage(i)}
              >×</button>
            </div>
          ))}
          <span className="attach-hint">{images.length} image{images.length > 1 ? "s" : ""} attached</span>
        </div>
      )}

      <form className="terminal-input-row" onSubmit={send}>
        <span className="terminal-prompt">❯</span>
        <textarea
          className="terminal-input"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={offline ? "Start session to send messages..." : "Type a message... (paste or drop screenshots, Enter to send)"}
          disabled={offline || sending}
          rows={1}
        />
        <button
          className="btn btn-primary btn-sm"
          type="submit"
          disabled={offline || sending || (!input.trim() && images.length === 0)}
        >
          {sending ? "..." : "Send"}
        </button>
      </form>
    </div>
  );
}
