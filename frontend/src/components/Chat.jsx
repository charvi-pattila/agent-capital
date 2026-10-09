import { useState, useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { api, streamCouncil } from "../api";
import Icon from "./Icon";

// Claude Code's own interactive select prompts (numbered options, one marked
// as current) render as plain text in the terminal mirror. Detect the block
// around the current option so a bare digit keypress can be translated into
// the Up/Down+Enter navigation that actually selects it — typing the digit
// literally doesn't do anything, it just falls into free text entry,
// discarding the list.
//
// The exact glyph marking the current option isn't a single fixed thing —
// the simple y/n trust dialogs use "❯", but a richer multi-question prompt
// (seen via screenshot, not directly captured) appeared to use something
// else, which silently broke digit-selection while raw arrow/Enter forwarding
// kept working fine (those don't depend on this parser at all). Rather than
// chase every possible glyph, prefer tmux's own reported cursor row — ground
// truth, independent of styling — and only fall back to scanning for a known
// pointer glyph when no usable cursor position is available.
const OPTION_LINE = /^\s*[^\s\d]?\s*(\d+)\.\s+(.+?)\s*$/;
const CURSOR_GLYPHS = ["❯", "›", "▸", "▶", "➤", "→"];
const CURSOR_LINE_RE = new RegExp(`^\\s*(?:${CURSOR_GLYPHS.join("|")})\\s*(\\d+)\\.\\s+`);

function parseSelectPrompt(content, cursor) {
  if (!content) return null;
  const lines = content.split("\n");

  let cursorLine = -1;
  if (cursor && cursor.visible && cursor.y >= 0 && cursor.y < lines.length
      && OPTION_LINE.test(lines[cursor.y])) {
    cursorLine = cursor.y;
  } else {
    for (let i = lines.length - 1; i >= 0; i--) {
      if (CURSOR_LINE_RE.test(lines[i])) { cursorLine = i; break; }
    }
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
      if (i === cursorLine) cursorPos = options.length;
      options.push({ number: m[1], label: m[2] });
    }
  }
  if (options.length < 2 || cursorPos === -1) return null;
  return { options, cursorPos };
}

// Raw bytes a terminal sends for the keys we forward by name.
const KEY_BYTES = {
  Enter: "\r", BSpace: "\x7f", Tab: "\t", BTab: "\x1b[Z", Escape: "\x1b", Space: " ",
  Up: "\x1b[A", Down: "\x1b[B", Right: "\x1b[C", Left: "\x1b[D",
  PageUp: "\x1b[5~", PageDown: "\x1b[6~",
};
const enc = new TextEncoder();

export default function Chat({ projectId, status }) {
  // Newest text snapshot of the pane (slow SSE feed). The xterm view draws the
  // screen itself; this only exists so the select-prompt chips and digit
  // shortcuts can parse the current menu.
  const [live, setLive] = useState({ content: "", cursor: null });
  const [offline, setOffline] = useState(true);
  const [sending, setSending] = useState(false);
  const [images, setImages] = useState([]); // [{ file, previewUrl }]
  const [attachError, setAttachError] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [councilState, setCouncilState] = useState("idle");
  const hostRef = useRef(null);   // xterm mounts here
  const xtermRef = useRef(null);
  const wsRef = useRef(null);
  const latestCursorRef = useRef(null);
  const inputRef = useRef(null);
  // Non-keystroke actions (chips, image upload) go over HTTP and must land in
  // order — fetches can resolve out of order, so chain them.
  const chainRef = useRef(Promise.resolve());
  const enqueue = (fn) => {
    chainRef.current = chainRef.current.then(fn).catch(() => {});
  };

  // Keystrokes ride the terminal websocket as raw bytes (no per-key HTTP round
  // trip); if it isn't open yet they fall back to the HTTP endpoints.
  const sendRaw = (str) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === 1) { ws.send(enc.encode(str)); return true; }
    return false;
  };
  const sendNamed = (name) => {
    if (!sendRaw(KEY_BYTES[name])) enqueue(() => api.sendKeys(projectId, [name]));
  };
  const typeText = (text) => {
    if (!sendRaw(text)) enqueue(() => api.typeText(projectId, text));
  };

  const selectPrompt = !offline ? parseSelectPrompt(live.content, live.cursor) : null;

  const chooseOption = (targetIdx) => {
    const delta = targetIdx - selectPrompt.cursorPos;
    const step = delta > 0 ? "Down" : "Up";
    const keys = Array(Math.abs(delta)).fill(step);
    keys.push("Enter");
    enqueue(() => api.sendKeys(projectId, keys));
  };

  // Multi-select mode: an alternative to the single-pick chips above, for
  // prompts where more than one option needs picking before confirming (a
  // checkbox-style list, à la a typical CLI multi-select — Space toggles the
  // highlighted row, Enter confirms whatever's checked). The user opts into
  // this explicitly (there's no reliable way to auto-detect "this menu is a
  // checkbox list" vs. "pick exactly one" from rendered text alone — already
  // got burned once this project trying to infer structure from glyphs).
  //
  // menuCursorRef tracks where WE believe the CLI's real cursor sits while
  // driving it ourselves — resynced from the live parse only when a prompt
  // newly appears (the `selectPrompt === null` transition below), not on
  // every render. Space doesn't move the CLI's cursor, only Up/Down do, so
  // this ref (not the possibly-stale last-polled cursorPos) is what stays
  // correct across several rapid clicks fired before the next SSE update.
  const [multiMode, setMultiMode] = useState(false);
  const [checked, setChecked] = useState(new Set());
  const menuCursorRef = useRef(0);
  useEffect(() => {
    if (selectPrompt) menuCursorRef.current = selectPrompt.cursorPos;
    else { setMultiMode(false); setChecked(new Set()); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectPrompt === null]);

  const toggleCheck = (idx) => {
    const delta = idx - menuCursorRef.current;
    const step = delta > 0 ? "Down" : "Up";
    const keys = Array(Math.abs(delta)).fill(step);
    keys.push("Space");
    menuCursorRef.current = idx;
    enqueue(() => api.sendKeys(projectId, keys));
    setChecked(prev => {
      const next = new Set(prev);
      next.has(idx) ? next.delete(idx) : next.add(idx);
      return next;
    });
  };

  const confirmMultiSelect = () => {
    enqueue(() => api.sendKeys(projectId, ["Enter"]));
    setMultiMode(false);
    setChecked(new Set());
  };

  useEffect(() => {
    let es;
    let reconnectTimer;
    let cancelled = false;

    const connect = () => {
      es = new EventSource(`/api/projects/${projectId}/terminal?slow=1`);

      es.onmessage = (e) => {
        const data = JSON.parse(e.data);
        setOffline(!!data.offline);
        if (data.cursor) latestCursorRef.current = data.cursor;
        if (data.content !== undefined) {
          setLive({ content: data.content, cursor: latestCursorRef.current });
        }
      };

      // The connection can drop (dev server restart, network blip) without
      // the browser's built-in auto-retry always kicking back in reliably —
      // force a fresh EventSource so the terminal (and the input, which is
      // disabled while `offline`) doesn't get stuck dead with no recovery.
      es.onerror = () => {
        es.close();
        if (!cancelled) reconnectTimer = setTimeout(connect, 2000);
      };
    };
    connect();

    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer);
      if (es) es.close();
    };
  }, [projectId]);

  // The council pastes its fix-feedback into this same tmux session — while
  // that's in flight, block typing here so the two don't interleave.
  useEffect(() => {
    const stop = streamCouncil(projectId, d => setCouncilState(d.state || "idle"));
    return stop;
  }, [projectId]);

  // The terminal itself: xterm.js drawing the bytes of a pty attached to the
  // tmux session. While connected the server sizes the tmux window to this
  // element (cols x rows), so Claude's TUI lays out like in any real terminal.
  useEffect(() => {
    if (offline) return;
    const css = getComputedStyle(document.documentElement);
    const term = new Terminal({
      fontFamily: '"Monaco", "Menlo", "Courier New", monospace',
      fontSize: 13,
      scrollback: 0, // Claude's TUI owns scrolling (PageUp/PageDown, below)
      cursorInactiveStyle: "block", // typing happens in the input row, not xterm
      theme: {
        background: css.getPropertyValue("--term").trim() || "#000",
        foreground: css.getPropertyValue("--term-text").trim() || "#ddd",
      },
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(hostRef.current);
    xtermRef.current = term;

    let ws;
    let reconnectTimer;
    let cancelled = false;
    const sendSize = () => {
      if (ws && ws.readyState === 1) {
        ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
      }
    };
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/api/projects/${projectId}/pty`);
      ws.binaryType = "arraybuffer";
      wsRef.current = ws;
      ws.onopen = () => { fit.fit(); sendSize(); };
      ws.onmessage = (e) => term.write(new Uint8Array(e.data));
      ws.onclose = () => {
        if (!cancelled && !document.hidden) reconnectTimer = setTimeout(connect, 2000);
      };
    };
    connect();
    // A hidden/backgrounded page (phone app switch, bfcache) must let go of the
    // terminal: while attached, the tmux window keeps this page's size.
    const onHidden = () => {
      if (document.hidden) {
        clearTimeout(reconnectTimer);
        if (ws) ws.close();
      } else if (!ws || ws.readyState > 1) {
        connect();
      }
    };
    document.addEventListener("visibilitychange", onHidden);
    window.addEventListener("pagehide", onHidden);

    // Paste (term.paste) and anything xterm itself captures go out as input too.
    const dataSub = term.onData(d => sendRaw(d));
    const sizeSub = term.onResize(sendSize);
    const ro = new ResizeObserver(() => fit.fit());
    ro.observe(hostRef.current);

    // Claude's TUI runs in the alternate screen (no scrollback), so wheel and
    // swipe become PageUp/PageDown, the same as the old text mirror did.
    let wheelAcc = 0;
    term.attachCustomWheelEventHandler((ev) => {
      wheelAcc += ev.deltaY;
      while (Math.abs(wheelAcc) >= 80) {
        sendRaw(KEY_BYTES[wheelAcc < 0 ? "PageUp" : "PageDown"]);
        wheelAcc -= Math.sign(wheelAcc) * 80;
      }
      return false;
    });
    let touchY = null;
    const el = hostRef.current;
    const onTouchStart = (e) => { touchY = e.touches[0].clientY; };
    const onTouchMove = (e) => {
      if (touchY === null) return;
      e.preventDefault();
      const dy = e.touches[0].clientY - touchY;
      if (Math.abs(dy) >= 48) {
        sendRaw(KEY_BYTES[dy > 0 ? "PageUp" : "PageDown"]);
        touchY = e.touches[0].clientY;
      }
    };
    el.addEventListener("touchstart", onTouchStart, { passive: true });
    el.addEventListener("touchmove", onTouchMove, { passive: false });

    return () => {
      cancelled = true;
      clearTimeout(reconnectTimer);
      document.removeEventListener("visibilitychange", onHidden);
      window.removeEventListener("pagehide", onHidden);
      el.removeEventListener("touchstart", onTouchStart);
      el.removeEventListener("touchmove", onTouchMove);
      ro.disconnect();
      dataSub.dispose();
      sizeSub.dispose();
      if (ws) ws.close();
      wsRef.current = null;
      xtermRef.current = null;
      term.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, offline]);

  // Revoke object URLs on unmount (ref avoids the stale initial-array closure)
  const imagesRef = useRef(images);
  imagesRef.current = images;
  useEffect(() => () => imagesRef.current.forEach(img => URL.revokeObjectURL(img.previewUrl)), []);

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
      return;
    }
    const text = e.clipboardData?.getData("text/plain");
    if (text) {
      e.preventDefault();
      xtermRef.current?.paste(text); // bracketed when Claude asked for it
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

  // Submitting with images attached still needs a real compose step: upload
  // each file, then paste the "[Image attached: path]" reference lines in at
  // the cursor (right after whatever's already been typed live) and submit —
  // reuses the same bracketed-paste primitive the automated flows (Council,
  // Split) send messages through.
  const submitWithImages = async () => {
    setSending(true);
    setAttachError("");
    try {
      const paths = [];
      for (const img of images) {
        const res = await api.uploadFile(projectId, img.file);
        if (!res.path) throw new Error(res.error || "upload failed");
        paths.push(res.path);
      }
      const lines = paths.map(p => `[Image attached: ${p}]`).join("\n");
      await api.sendMessage(projectId, lines);
      images.forEach(img => URL.revokeObjectURL(img.previewUrl));
      setImages([]);
    } catch (e) {
      // Keep the attachments so nothing is lost and Enter can retry.
      setAttachError(`Couldn't send image — ${e.message || "upload failed"}. Tap Enter to retry.`);
    }
    setSending(false);
  };

  // Enter from either the keyboard or the phone key strip: with images
  // attached it has to go through the upload-then-paste step, not a bare key.
  const submitEnter = () => {
    if (images.length > 0) enqueue(submitWithImages);
    else sendNamed("Enter");
  };

  const ARROW_KEY = { ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right" };

  // Forwards every keystroke straight into the tmux session as it's typed —
  // the same interface as a real terminal (or `ssh`), rather than composing
  // a message in a box and submitting it as one chunk. Arrow keys navigate
  // Claude Code's own interactive prompts, Tab/Backspace/Escape do whatever
  // they do in its CLI, and plain characters echo live in the terminal mirror.
  const handleKeyDown = (e) => {
    if (offline || sending || councilState === "fixing") return;
    if (e.metaKey || e.ctrlKey || e.altKey) return; // leave OS/browser shortcuts alone
    // The checklist owns cursor navigation while it's open — raw typing here
    // would fight with menuCursorRef's optimistic tracking of the real
    // terminal cursor's position.
    if (multiMode) return;

    // While a select-prompt is showing, a digit matching one of the listed
    // options selects it via Up/Down+Enter instead of being forwarded as a
    // literal character (which the CLI doesn't treat as a shortcut — it just
    // types the digit into whatever's focused and Enter submits it as a
    // stray message). Re-evaluated on every keystroke, not just the first:
    // gating this to a single "first keystroke since the prompt appeared"
    // used to mean a second press of the same digit (e.g. because the first
    // one hadn't visibly landed yet) fell through to literal typing instead.
    if (selectPrompt && /^[1-9]$/.test(e.key)) {
      const idx = selectPrompt.options.findIndex(o => o.number === e.key);
      if (idx !== -1) {
        e.preventDefault();
        chooseOption(idx);
        return;
      }
    }

    if (e.key === "Enter") {
      e.preventDefault();
      submitEnter();
      return;
    }
    if (e.key === "Backspace") {
      e.preventDefault();
      sendNamed("BSpace");
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      sendNamed(e.shiftKey ? "BTab" : "Tab");
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      sendNamed("Escape");
      return;
    }
    if (ARROW_KEY[e.key]) {
      e.preventDefault();
      sendNamed(ARROW_KEY[e.key]);
      return;
    }
    if (e.key.length === 1) {
      e.preventDefault();
      typeText(e.key);
    }
  };

  // Key strip (phone): the keys Claude Code's TUI needs that a soft keyboard
  // hides. pointerdown is cancelled so tapping one doesn't blur the input and
  // dismiss the keyboard mid-conversation.
  const sendKey = (key) => {
    if (key === "Enter") return submitEnter();
    sendNamed(key);
  };
  const keepFocus = (e) => e.preventDefault();

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

      <div className="terminal-body xterm-body" onClick={() => inputRef.current?.focus()}>
        {offline && (
          <div className="terminal-offline">
            {status === "running"
              ? "Connecting to session..."
              : "Session is stopped. Press Start to launch Claude."}
          </div>
        )}
        {!offline && <div className="terminal-host" ref={hostRef} />}
      </div>

      {selectPrompt && !multiMode && (
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
          <button type="button" className="multi-select-toggle" onClick={() => setMultiMode(true)}>
            Select multiple
          </button>
        </div>
      )}

      {selectPrompt && multiMode && (
        <div className="multi-select-panel">
          <div className="multi-select-list">
            {selectPrompt.options.map((opt, i) => (
              <label className="multi-select-row" key={i}>
                <span
                  className={"multi-select-box" + (checked.has(i) ? " checked" : "")}
                  onClick={() => toggleCheck(i)}
                >
                  {checked.has(i) && <Icon name="check" size={12} strokeWidth={2.5} />}
                </span>
                <span className="multi-select-label">{opt.label}</span>
              </label>
            ))}
          </div>
          <div className="multi-select-actions">
            <button type="button" className="btn-ghost" onClick={() => setMultiMode(false)}>Cancel</button>
            <button type="button" className="btn-primary" onClick={confirmMultiSelect} disabled={checked.size === 0}>
              Confirm ({checked.size})
            </button>
          </div>
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
          <span className="attach-hint">
            {attachError || `${images.length} image${images.length > 1 ? "s" : ""} attached`}
          </span>
        </div>
      )}

      {councilState === "fixing" && (
        <div className="council-banner" style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <Icon name="scale" size={15} /> Review findings are being sent to Claude — input is paused so it doesn't get mixed in.
        </div>
      )}

      {!offline && councilState !== "fixing" && (
        <div className="key-strip">
          <button type="button" className="key" onPointerDown={keepFocus} onClick={() => sendKey("Escape")}>Esc</button>
          <button type="button" className="key" onPointerDown={keepFocus} onClick={() => sendKey("Up")} aria-label="Up"><Icon name="arrow-up" size={18} /></button>
          <button type="button" className="key" onPointerDown={keepFocus} onClick={() => sendKey("Down")} aria-label="Down"><Icon name="arrow-down" size={18} /></button>
          <button type="button" className="key" onPointerDown={keepFocus} onClick={() => sendKey("Tab")}>Tab</button>
          <button type="button" className="key" onPointerDown={keepFocus} onClick={() => sendKey("Enter")} aria-label="Enter"><Icon name="enter" size={18} /></button>
          <button type="button" className="key key-type" onClick={() => inputRef.current?.focus()}><Icon name="keyboard" size={18} /> Type</button>
        </div>
      )}

      <form className="terminal-input-row" onSubmit={e => e.preventDefault()}>
        <span className="terminal-prompt">❯</span>
        {/* Uncontrolled and always empty: every keystroke is forwarded and
            preventDefault'd (see handleKeyDown) rather than accumulated here —
            the terminal mirror above is the only place typed text appears,
            same as a real terminal. */}
        <input
          ref={inputRef}
          type="text"
          className="terminal-input"
          defaultValue=""
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
          placeholder={
            councilState === "fixing"
              ? "Claude is addressing review feedback..."
              : offline ? "Start session to type..." : "Tap here and type — live terminal input"
          }
          disabled={offline || sending || councilState === "fixing"}
        />
        {sending && <span className="terminal-send-hint">sending…</span>}
      </form>
    </div>
  );
}
