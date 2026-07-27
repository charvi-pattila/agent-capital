import { useState, useEffect, useRef } from "react";
import { api, streamCouncil } from "../api";

// Claude Code's own interactive select prompts (numbered options with a "❯"
// cursor, navigated via arrow keys) render as plain text in the terminal
// mirror. Detect the block around the cursor so it can be offered as
// clickable chips instead of requiring the user to arrow through manually.
const OPTION_LINE = /^\s*(❯)?\s*(\d+)\.\s+(.+?)\s*$/;

function parseSelectPrompt(content) {
  if (!content) return null;
  const lines = content.split("\n");

  let cursorLine = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = lines[i].match(OPTION_LINE);
    if (m && m[1]) { cursorLine = i; break; }
  }
  if (cursorLine === -1) return null;

  let start = cursorLine;
  while (start - 1 >= 0 && lines[start - 1].trim() !== "") start--;
  let end = cursorLine;
  while (end + 1 < lines.length && lines[end + 1].trim() !== "") end++;

  const options = [];
  let cursorPos = -1;
  for (let i = start; i <= end; i++) {
    const m = lines[i].match(OPTION_LINE);
    if (m) {
      if (m[1]) cursorPos = options.length;
      options.push({ number: m[2], label: m[3] });
    }
  }
  if (options.length < 2 || cursorPos === -1) return null;
  return { options, cursorPos };
}

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
  const [councilState, setCouncilState] = useState("idle");
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

  // The council pastes its fix-feedback into this same tmux session — while
  // that's in flight, block typing here so the two don't interleave.
  useEffect(() => {
    const stop = streamCouncil(projectId, d => setCouncilState(d.state || "idle"));
    return stop;
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

  const selectPrompt = !offline ? parseSelectPrompt(content) : null;

  const chooseOption = (targetIdx) => {
    if (!selectPrompt) return;
    const delta = targetIdx - selectPrompt.cursorPos;
    const step = delta > 0 ? "Down" : "Up";
    const keys = Array(Math.abs(delta)).fill(step);
    keys.push("Enter");
    api.sendKeys(projectId, keys).catch(() => {});
  };

  const handleKeyDown = (e) => {
    // While an interactive select prompt is showing, a bare number key picks
    // that option directly (same Up/Down+Enter forwarding as clicking a chip)
    // instead of being typed as text — only when the box is still empty, so
    // it never hijacks a real answer that happens to start with a digit.
    if (selectPrompt && input === "" && /^[1-9]$/.test(e.key)) {
      const idx = selectPrompt.options.findIndex(o => o.number === e.key);
      if (idx !== -1) {
        e.preventDefault();
        chooseOption(idx);
        return;
      }
    }
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

      {selectPrompt && (
        <div className="select-prompt-row">
          {selectPrompt.options.map((opt, i) => (
            <button
              type="button"
              key={i}
              className={"select-chip" + (i === selectPrompt.cursorPos ? " active" : "")}
              onClick={() => chooseOption(i)}
            >
              {opt.number}. {opt.label.length > 44 ? opt.label.slice(0, 44) + "…" : opt.label}
            </button>
          ))}
        </div>
      )}

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

      {councilState === "fixing" && (
        <div className="council-banner">
          ⚖️ Council is sending feedback to Claude — input is paused so it doesn't get mixed in.
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
          placeholder={
            councilState === "fixing"
              ? "Council is talking to Claude right now..."
              : offline ? "Start session to send messages..." : "Type and press Enter..."
          }
          disabled={offline || sending || councilState === "fixing"}
          rows={1}
        />
        <span className="terminal-send-hint">{sending ? "sending…" : "↵"}</span>
      </form>
    </div>
  );
}
