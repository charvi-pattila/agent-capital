import { useState, useEffect, useRef } from "react";
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

// tmux always captures a full pane-height's worth of lines (the session is
// created tall on purpose, see spawn_claude_tmux, so a good chunk of
// scrollback fits in one capture) — once the actual output is short, most of
// that is blank padding below the last real line, which otherwise renders as
// a big dead gap between the visible content and the input box below it.
// Trim it, but never past the cursor's own row (kept even if that row looks
// blank, e.g. an empty prompt with nothing typed yet).
//
// A prior version of this also cut everything before the last long run of
// blank lines, to handle Ink leaving a stale boot-banner frame sitting way
// above a freshly-opened menu (rows 7-185 blank, banner at 1-6, menu at
// 186-199). That was wrong in general: verified live against a real, ongoing
// conversation (not a fresh boot) where Claude's actual last reply — the
// question the user still needed to answer — sat at rows 1-40, followed by
// ~156 *legitimately blank* rows before the input line (completely normal:
// the pane is deliberately tall, and Ink just doesn't fill it). The old logic
// treated that as "stale" and deleted the real conversation from view. Only
// trailing blank (after the last real line, never seen, never scrolled to)
// is safe to cut — never lines before it, no matter how much blank space
// follows them.
function trimDeadSpace(content, cursor) {
  const lines = content.split("\n");
  const minKeep = cursor && cursor.y >= 0 ? cursor.y : 0;
  let last = lines.length - 1;
  while (last > minKeep && lines[last].trim() === "") last--;
  return { text: lines.slice(0, last + 1).join("\n"), cursor };
}

// The trailing trim above doesn't touch blank space *between* two real
// blocks — verified live: a normal reply ending "Tell me which registrar..."
// sat right above ~137 genuinely blank rows before the next input line, all
// of it legitimate (Ink just doesn't fill the deliberately-tall pane), none
// of it safe to delete outright (that's exactly the mistake reverted above).
// But it's still a wall of dead space between two things the user actually
// needs to read together. Difference from the reverted approach: this never
// removes a non-blank line, ever — it only shortens a blank *run* down to a
// small fixed size, so nothing that was ever real text can be lost, only
// excess padding. `indexMap` remaps each original row to its new position so
// the cursor (and hence the auto-scroll target) still lands correctly.
const MAX_BLANK_RUN = 3;
function collapseBlankRuns(content, cursor) {
  const lines = content.split("\n");
  const outLines = [];
  const indexMap = new Array(lines.length);
  let blankRun = 0;
  for (let i = 0; i < lines.length; i++) {
    if (lines[i].trim() === "") {
      blankRun++;
      if (blankRun <= MAX_BLANK_RUN) {
        indexMap[i] = outLines.length;
        outLines.push(lines[i]);
      } else {
        indexMap[i] = outLines.length - 1; // folds into the last kept blank line
      }
    } else {
      blankRun = 0;
      indexMap[i] = outLines.length;
      outLines.push(lines[i]);
    }
  }
  const newCursor = cursor && cursor.y >= 0 && cursor.y < indexMap.length
    ? { ...cursor, y: indexMap[cursor.y] }
    : cursor;
  return { text: outLines.join("\n"), cursor: newCursor };
}

// Renders one line of the terminal mirror, splicing in a blinking caret span
// when this is the line tmux's cursor actually sits on and reports it visible
// — otherwise x/y can be stale (e.g. wherever it last sat before a selection
// menu hid it) and a fake blinking caret on old, unrelated text would be
// actively misleading. A blank line still needs *something* rendered or the
// div collapses to zero height (breaking the line-based scroll math below).
function renderLine(line, isCursorLine, cursor) {
  if (!isCursorLine || !cursor || !cursor.visible) return line || " ";
  const before = line.slice(0, cursor.x);
  const at = line[cursor.x] ?? " ";
  const after = line.slice(cursor.x + 1);
  return (
    <>
      {before}
      <span className="term-cursor">{at}</span>
      {after}
    </>
  );
}

export default function Chat({ projectId, status }) {
  const [content, setContent] = useState("");
  const [cursor, setCursor] = useState(null); // { x, y, visible } — tmux's real cursor cell
  // Always the newest frame, even while the view is paused for reading (in
  // which case `content`/`cursor` above deliberately stop updating). Anything
  // that *acts* on the terminal — the select-prompt chips, digit shortcuts —
  // must key off this, never the frozen display copy: keys are computed as
  // Up/Down deltas from the highlighted row, and a delta taken against a
  // stale frame walks the real cursor to the wrong option.
  const [live, setLive] = useState({ content: "", cursor: null });
  const [offline, setOffline] = useState(true);
  const [sending, setSending] = useState(false);
  const [images, setImages] = useState([]); // [{ file, previewUrl }]
  const [dragOver, setDragOver] = useState(false);
  // "Pinned" = following live output. Scrolling up pauses the feed entirely
  // (frozen text is readable/selectable while Claude streams); the latest
  // content keeps buffering in latestRef and is applied on resume.
  const [pinned, setPinned] = useState(true);
  const [councilState, setCouncilState] = useState("idle");
  const termRef = useRef(null);
  const targetLineRef = useRef(null); // the DOM node for the line auto-scroll should land on
  const esRef = useRef(null);
  const pinnedRef = useRef(true);
  const latestRef = useRef("");
  const latestCursorRef = useRef(null);
  const touchingRef = useRef(false);
  const scrollCooldownRef = useRef(false);
  const inputRef = useRef(null);
  // Forwarded keystrokes/pastes must land in the order they were typed —
  // fetches can resolve out of order over the network, so chain them.
  const chainRef = useRef(Promise.resolve());
  const enqueue = (fn) => {
    chainRef.current = chainRef.current.then(fn).catch(() => {});
  };

  // Printable characters typed in quick succession (fast typing, key repeat,
  // IME) are coalesced into one /type call instead of one round trip per
  // character — each round trip is small on its own, but firing one per
  // keystroke let fast typing outrun the network and visibly queue up.
  const pendingTextRef = useRef("");
  const flushTimerRef = useRef(null);
  const flushPendingText = () => {
    if (flushTimerRef.current) {
      clearTimeout(flushTimerRef.current);
      flushTimerRef.current = null;
    }
    if (pendingTextRef.current) {
      const text = pendingTextRef.current;
      pendingTextRef.current = "";
      enqueue(() => api.typeText(projectId, text));
    }
  };
  const queueChar = (ch) => {
    pendingTextRef.current += ch;
    if (!flushTimerRef.current) {
      flushTimerRef.current = setTimeout(() => {
        flushTimerRef.current = null;
        flushPendingText();
      }, 12);
    }
  };
  useEffect(() => () => flushPendingText(), [projectId]);

  const selectPrompt = !offline ? parseSelectPrompt(live.content, live.cursor) : null;
  // parseSelectPrompt above (and everything that forwards keystrokes) works
  // off the raw, untrimmed *live* content/cursor — trimming only affects
  // what's rendered, so row indices used to detect the current menu stay
  // valid, and a paused-for-reading view can't feed it a stale menu.
  const trimmed = !offline ? trimDeadSpace(content, cursor) : { text: content, cursor };
  const { text: displayText, cursor: displayCursor } = !offline
    ? collapseBlankRuns(trimmed.text, trimmed.cursor)
    : trimmed;
  const displayLines = displayText.split("\n");
  // Where auto-scroll should land: the last *rendered* line. Trailing blank
  // padding is already trimmed and long blank runs collapsed above, so this
  // is the actually-current line, not the raw bottom of the (deliberately
  // huge, mostly-blank) tmux pane.
  //
  // This used to target the CLI's cursor row instead, which broke every
  // interactive select prompt: while a menu is open, Ink hides the cursor and
  // parks it on the highlighted option — the *middle* of the menu — so
  // aligning that row to the container's bottom edge scrolled the remaining
  // options out of view, and (worse) left enough content below the fold that
  // handleScroll's at-bottom check failed on the programmatic scroll and
  // silently paused live updates. From then on the frame stayed frozen on the
  // first question while the real terminal moved on, so chip clicks computed
  // Up/Down deltas against a stale menu and landed on the wrong option
  // (typically "Type something." / Esc → "User declined to answer").
  // Verified in headless Chrome against a real AskUserQuestion dialog.
  const targetLineIdx = displayLines.length - 1;

  const chooseOption = (targetIdx) => {
    flushPendingText();
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

  // Scrolls so the current line's bottom edge sits at the container's bottom
  // edge — not `scrollTop = scrollHeight`, which lands on the literal end of
  // the tmux pane. A normal reply can leave 100+ blank rows before the next
  // prompt (the pane is deliberately tall; Ink just doesn't fill it), so that
  // showed a viewport almost entirely full of blank space instead of the
  // line the user's actually looking at. Uses viewport-relative rects (not
  // offsetTop) so it doesn't depend on which ancestor ends up as offsetParent.
  const scrollToTarget = () => {
    const body = termRef.current;
    const target = targetLineRef.current;
    if (!body) return;
    if (!target) { body.scrollTop = body.scrollHeight; return; }
    const delta = target.getBoundingClientRect().bottom - body.getBoundingClientRect().bottom;
    body.scrollTop = Math.max(0, body.scrollTop + delta);
  };

  const pauseLive = () => { pinnedRef.current = false; setPinned(false); };
  const resumeLive = () => {
    pinnedRef.current = true;
    setPinned(true);
    setContent(latestRef.current);
    setCursor(latestCursorRef.current);
    requestAnimationFrame(scrollToTarget);
  };

  useEffect(() => {
    let es;
    let reconnectTimer;
    let cancelled = false;

    const connect = () => {
      es = new EventSource(`/api/projects/${projectId}/terminal`);
      esRef.current = es;

      es.onmessage = (e) => {
        const data = JSON.parse(e.data);
        setOffline(!!data.offline);
        if (data.content !== undefined) {
          latestRef.current = data.content;
          if (pinnedRef.current) setContent(data.content);
        }
        if (data.cursor) {
          latestCursorRef.current = data.cursor;
          if (pinnedRef.current) setCursor(data.cursor);
        }
        if (data.content !== undefined) {
          setLive({ content: latestRef.current, cursor: latestCursorRef.current });
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

  // Keep the view glued to the current line while following (runs after
  // render, so the target line's rect reflects the new content).
  useEffect(() => {
    if (!offline && pinnedRef.current) scrollToTarget();
  }, [content, cursor, offline]);

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
      return;
    }
    const text = e.clipboardData?.getData("text/plain");
    if (text) {
      e.preventDefault();
      flushPendingText();
      enqueue(() => api.typeText(projectId, text));
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
    try {
      const paths = [];
      for (const img of images) {
        const res = await api.uploadFile(projectId, img.file);
        if (res.path) paths.push(res.path);
      }
      const lines = paths.map(p => `[Image attached: ${p}]`).join("\n");
      if (lines) await api.sendMessage(projectId, lines);
      else await api.sendKeys(projectId, ["Enter"]);
      images.forEach(img => URL.revokeObjectURL(img.previewUrl));
      setImages([]);
    } catch (_) {}
    setSending(false);
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
      flushPendingText();
      // Chained after the flush above (not fired independently) so the image
      // upload can't race ahead of and land before text just typed live.
      if (images.length > 0) enqueue(submitWithImages);
      else enqueue(() => api.sendKeys(projectId, ["Enter"]));
      return;
    }
    if (e.key === "Backspace") {
      e.preventDefault();
      flushPendingText();
      enqueue(() => api.sendKeys(projectId, ["BSpace"]));
      return;
    }
    if (e.key === "Tab") {
      e.preventDefault();
      flushPendingText();
      enqueue(() => api.sendKeys(projectId, [e.shiftKey ? "BTab" : "Tab"]));
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      flushPendingText();
      enqueue(() => api.sendKeys(projectId, ["Escape"]));
      return;
    }
    if (ARROW_KEY[e.key]) {
      e.preventDefault();
      flushPendingText();
      enqueue(() => api.sendKeys(projectId, [ARROW_KEY[e.key]]));
      return;
    }
    if (e.key.length === 1) {
      e.preventDefault();
      queueChar(e.key);
    }
  };

  // Key strip (phone): the keys Claude Code's TUI needs that a soft keyboard
  // hides. pointerdown is cancelled so tapping one doesn't blur the input and
  // dismiss the keyboard mid-conversation.
  const sendKey = (key) => {
    flushPendingText();
    enqueue(() => api.sendKeys(projectId, [key]));
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

      <div
        className="terminal-body"
        ref={termRef}
        onScroll={handleScroll}
        onWheel={handleWheel}
        onTouchStart={() => { touchingRef.current = true; }}
        onTouchEnd={() => { touchingRef.current = false; handleScroll(); }}
        onClick={() => inputRef.current?.focus()}
      >
        <div className="terminal-body-inner">
          {offline ? (
            <div className="terminal-offline">
              {status === "running"
                ? "Connecting to session..."
                : "Session is stopped. Press Start to launch Claude."}
            </div>
          ) : (
            <pre className="terminal-output">
              {displayLines.map((line, i) => (
                <div key={i} ref={i === targetLineIdx ? targetLineRef : null}>
                  {renderLine(line, !!displayCursor && i === displayCursor.y, displayCursor)}
                </div>
              ))}
            </pre>
          )}
        </div>
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
          <span className="attach-hint">{images.length} image{images.length > 1 ? "s" : ""} attached</span>
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
