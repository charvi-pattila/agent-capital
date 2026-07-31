import { useEffect, useRef, useState } from "react";
import { api, streamSplit } from "../api";

const STATE_LABEL = {
  idle: "No split running",
  planning: "Planning…",
  planned: "Review the plan",
  running: "Agents working",
  merging: "Merging…",
  merged: "Merged",
  conflict: "Conflict",
};

const BRANCH_TAG = {
  starting: "tag-paused",
  working: "tag-running",
  idle: "tag-paused",
  stopped: "tag-stopped",
  merged: "tag-running",
  conflict: "tag-failed",
  empty: "tag-stopped",
};

const BRANCH_LABEL = {
  starting: "starting…",
  working: "working",
  idle: "idle — may be done or waiting",
  stopped: "session stopped",
  merged: "merged",
  conflict: "conflict",
  empty: "no changes",
};

export default function SplitPanel({ projectId }) {
  const [split, setSplit] = useState(null);
  const [prompt, setPrompt] = useState("");
  const [count, setCount] = useState(3);
  const [draft, setDraft] = useState([]);
  const [selected, setSelected] = useState(null);
  const [busy, setBusy] = useState("");
  const [deleteBranches, setDeleteBranches] = useState(false);
  const seededRef = useRef("");

  useEffect(() => {
    let mounted = true;
    api.getSplit(projectId).then(d => {
      if (!mounted) return;
      setSplit(d);
      setPrompt(d.prompt || "");
    });
    const stop = streamSplit(projectId, d => setSplit(d));
    return () => { mounted = false; stop(); };
  }, [projectId]);

  // Seed the editable plan from a freshly-returned proposal — but only once per
  // proposal, so a later SSE frame doesn't wipe out edits in progress.
  useEffect(() => {
    const proposed = split?.proposed;
    if (split?.state !== "planned" || !proposed?.length) return;
    const sig = JSON.stringify(proposed);
    if (seededRef.current === sig) return;
    seededRef.current = sig;
    setDraft(proposed.map(b => ({ ...b, include: true })));
  }, [split]);

  // Default the open terminal to the first agent of a run.
  useEffect(() => {
    const branches = split?.run?.branches;
    if (!branches?.length) { setSelected(null); return; }
    setSelected(s => (branches.some(b => b.id === s) ? s : branches[0].id));
  }, [split?.run?.id, split?.run?.branches?.length]);

  if (!split) return <div style={{ color: "var(--text2)", padding: 20 }}>Loading…</div>;

  const state = split.state || "idle";
  const run = split.run;

  const doPlan = async () => {
    if (!prompt.trim()) return;
    setBusy("plan");
    seededRef.current = "";
    try { await api.planSplit(projectId, prompt, count); } catch (_) {}
    setBusy("");
  };

  const doLaunch = async () => {
    const branches = draft.filter(b => b.include && b.name.trim() && b.task.trim());
    if (branches.length < 2) return;
    setBusy("launch");
    try { await api.launchSplit(projectId, prompt, branches); } catch (_) {}
    setBusy("");
  };

  const doMerge = async (commitBase = false) => {
    setBusy("merge");
    try { await api.mergeSplit(projectId, commitBase); } catch (_) {}
    setBusy("");
  };

  const doCleanup = async () => {
    const unmerged = (run?.branches || []).filter(b => b.status !== "merged" && b.status !== "empty");
    const warn = unmerged.length
      ? `${unmerged.length} branch(es) aren't merged yet.` +
        (deleteBranches ? " Deleting the branches throws that work away for good." : " Their branches will be kept so you can merge them by hand.")
      : "";
    if (warn && !window.confirm(`${warn}\n\nClose this split run?`)) return;
    setBusy("cleanup");
    try { await api.cleanupSplit(projectId, deleteBranches); } catch (_) {}
    setBusy("");
  };

  const header = (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
      <div className="memory-section-title" style={{ marginBottom: 0 }}>Split</div>
      <span className={"tag " + (state === "merged" ? "tag-running" : state === "conflict" ? "tag-failed" : state === "idle" ? "tag-stopped" : "tag-paused")}>
        {STATE_LABEL[state] || state}
      </span>
    </div>
  );

  if (!split.can_split) {
    return (
      <div className="memory-panel">
        <div className="memory-section">
          {header}
          <p style={{ color: "var(--yellow)", fontSize: 13, lineHeight: 1.6 }}>{split.blocker}</p>
          <p style={{ color: "var(--text2)", fontSize: 13, lineHeight: 1.6 }}>
            Splits give each mini-agent its own git worktree and branch, so they can work in
            parallel without overwriting each other — that needs a git repo with at least one commit.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="memory-panel">
      <div className="memory-section">
        {header}
        <p style={{ color: "var(--text2)", fontSize: 13, lineHeight: 1.6 }}>
          For a task too big to walk through one step at a time. Claude reads the repo and proposes
          independent pieces; you edit them; each approved piece gets its own agent, branch and
          worktree, all running at once. Correct any single agent without disturbing the others,
          then merge the branches back into <code>{split.base_branch || "the base branch"}</code> together.
        </p>
        {split.error && <p style={{ color: "var(--red)", fontSize: 13 }}>{split.error}</p>}
      </div>

      {state === "idle" && (
        <div className="memory-section">
          <div className="memory-section-title">The task to split</div>
          <textarea
            className="memory-textarea"
            style={{ minHeight: 130 }}
            value={prompt}
            onChange={e => setPrompt(e.target.value)}
            placeholder="Describe the whole job, the same way you'd type it into Chat. Claude will work out how it divides."
          />
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <label style={{ fontSize: 13, color: "var(--text2)", display: "inline-flex", alignItems: "center", gap: 6, whiteSpace: "nowrap" }}>
              Aim for{" "}
              <select
                value={count}
                onChange={e => setCount(Number(e.target.value))}
                style={{ background: "var(--surface2)", color: "var(--text)", border: "1px solid var(--border)", borderRadius: 6, padding: "4px 8px" }}
              >
                {[2, 3, 4, 5, 6].map(n => <option key={n} value={n}>{n}</option>)}
              </select>{" "}
              agents
            </label>
            <button className="btn btn-primary btn-sm" onClick={doPlan} disabled={!prompt.trim() || busy === "plan"}>
              {busy === "plan" ? "Planning…" : "⑂ Plan the split"}
            </button>
          </div>
          <p className="form-hint">
            The planner reads your code before answering, so this takes a minute or two.
          </p>
        </div>
      )}

      {state === "planning" && (
        <div className="memory-section">
          <div className="memory-item" style={{ cursor: "default", flexDirection: "column", alignItems: "stretch", gap: 6 }}>
            <div style={{ fontWeight: 600 }}>Reading the repo and working out the split…</div>
            <div style={{ color: "var(--text2)", fontSize: 13, whiteSpace: "pre-wrap" }}>{split.prompt}</div>
          </div>
        </div>
      )}

      {state === "planned" && (
        <PlanEditor
          draft={draft}
          setDraft={setDraft}
          onLaunch={doLaunch}
          onDiscard={() => { seededRef.current = ""; setDraft([]); api.cleanupSplit(projectId); }}
          busy={busy === "launch"}
        />
      )}

      {run && (
        <>
          {split.needs_base_commit?.length > 0 && (
            <div className="memory-section">
              <div className="memory-item" style={{ cursor: "default", flexDirection: "column", alignItems: "stretch", gap: 8, borderColor: "var(--yellow)" }}>
                <div style={{ fontWeight: 600, color: "var(--yellow)" }}>
                  {split.base_branch || "The base branch"} has uncommitted changes
                </div>
                <div style={{ color: "var(--text2)", fontSize: 13 }}>
                  Merging would fold these into the merge commit. Commit them first as a snapshot, then merge:
                  <div className="memory-preview">{split.needs_base_commit.join("\n")}</div>
                </div>
                <div>
                  <button className="btn btn-primary btn-sm" onClick={() => doMerge(true)} disabled={busy === "merge"}>
                    Commit these, then merge
                  </button>
                </div>
              </div>
            </div>
          )}

          <div className="memory-section">
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
              <div className="memory-section-title" style={{ marginBottom: 0 }}>
                {run.branches.length} agents · base {run.base_branch}
              </div>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <label className="form-check" style={{ fontSize: 12, color: "var(--text3)" }}>
                  <input type="checkbox" checked={deleteBranches} onChange={e => setDeleteBranches(e.target.checked)} />
                  delete branches on close
                </label>
                <button className="btn btn-ghost btn-sm" onClick={doCleanup} disabled={busy === "cleanup"}>
                  {busy === "cleanup" ? "Closing…" : "Close run"}
                </button>
                <button className="btn btn-primary btn-sm" onClick={() => doMerge(false)} disabled={busy === "merge" || state === "merging"}>
                  {busy === "merge" || state === "merging" ? "Merging…" : "⤵ Merge all"}
                </button>
              </div>
            </div>

            <div className="split-layout">
              <div className="split-branch-list">
                {run.branches.map(b => (
                  <BranchCard
                    key={b.id}
                    branch={b}
                    active={b.id === selected}
                    onSelect={() => setSelected(b.id)}
                    onRebase={() => api.splitBranchRebase(projectId, b.id)}
                    onRestart={() => api.splitBranchRestart(projectId, b.id)}
                  />
                ))}
              </div>
              {selected && (
                <AgentTerminal
                  key={selected}
                  projectId={projectId}
                  branch={run.branches.find(b => b.id === selected)}
                />
              )}
            </div>
          </div>
        </>
      )}

      {!run && split.history?.length > 0 && (
        <div className="memory-section">
          <div className="memory-section-title">Past splits</div>
          {split.history.map(h => (
            <div className="memory-item" key={h.id} style={{ cursor: "default", flexDirection: "column", alignItems: "stretch", gap: 6 }}>
              <div style={{ display: "flex", justifyContent: "space-between", gap: 10 }}>
                <span style={{ fontWeight: 600, fontSize: 13 }}>{h.branches.length} agents</span>
                <span className={"tag " + (h.final_state === "merged" ? "tag-running" : "tag-stopped")}>
                  {h.final_state === "merged" ? "merged" : h.final_state || "closed"}
                </span>
              </div>
              <div style={{ color: "var(--text2)", fontSize: 12 }}>{(h.prompt || "").slice(0, 160)}</div>
              <div style={{ color: "var(--text3)", fontSize: 12 }}>
                {h.branches.map(b => b.name).join(" · ")}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function PlanEditor({ draft, setDraft, onLaunch, onDiscard, busy }) {
  const update = (i, patch) => setDraft(d => d.map((b, idx) => (idx === i ? { ...b, ...patch } : b)));
  const remove = (i) => setDraft(d => d.filter((_, idx) => idx !== i));
  const add = () => setDraft(d => [...d, { name: "", task: "", files: [], include: true }]);
  const chosen = draft.filter(b => b.include && b.name.trim() && b.task.trim()).length;

  return (
    <div className="memory-section">
      <div className="memory-section-title">Proposed branches — edit anything before launching</div>
      {draft.map((b, i) => (
        <div className="memory-item" key={i} style={{ cursor: "default", flexDirection: "column", alignItems: "stretch", gap: 8, opacity: b.include ? 1 : 0.5 }}>
          <div className="split-branch-head">
            <input
              type="checkbox"
              checked={b.include}
              onChange={e => update(i, { include: e.target.checked })}
              title="Include this branch"
            />
            <input
              className="chat-input"
              style={{ borderRadius: 8, padding: "6px 12px", fontWeight: 600 }}
              value={b.name}
              onChange={e => update(i, { name: e.target.value })}
              placeholder="branch name"
            />
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => remove(i)} title="Remove">✕</button>
          </div>
          <textarea
            className="memory-textarea"
            style={{ minHeight: 110, fontSize: 12.5 }}
            value={b.task}
            onChange={e => update(i, { task: e.target.value })}
            placeholder="Everything this agent will be told."
          />
          {b.files?.length > 0 && (
            <div style={{ color: "var(--text3)", fontSize: 12, fontFamily: "monospace" }}>
              {b.files.join("  ·  ")}
            </div>
          )}
        </div>
      ))}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button className="btn btn-ghost btn-sm" onClick={add}>+ Add branch</button>
        <button className="btn btn-ghost btn-sm" onClick={onDiscard}>Start over</button>
        <button className="btn btn-primary btn-sm" onClick={onLaunch} disabled={chosen < 2 || busy}>
          {busy ? "Launching…" : `▶ Launch ${chosen} agents`}
        </button>
      </div>
      {chosen < 2 && <p className="form-hint">Keep at least 2 branches — fewer than that is just a normal chat message.</p>}
    </div>
  );
}

function BranchCard({ branch, active, onSelect, onRebase, onRestart }) {
  const files = branch.changed_files || [];
  return (
    <div
      className={"memory-item split-branch" + (active ? " active" : "")}
      onClick={onSelect}
      style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
        <span style={{ fontWeight: 600, fontSize: 13 }}>{branch.name}</span>
        <span className={"tag " + (BRANCH_TAG[branch.status] || "tag-stopped")}>
          {BRANCH_LABEL[branch.status] || branch.status}
        </span>
      </div>
      <div style={{ color: "var(--text3)", fontSize: 11, fontFamily: "monospace" }}>{branch.branch}</div>
      <div style={{ color: "var(--text2)", fontSize: 12 }}>
        {files.length ? `${files.length} file${files.length > 1 ? "s" : ""} changed` : "no changes yet"}
      </div>
      {branch.status === "conflict" && (
        <>
          <div className="memory-preview" style={{ color: "var(--red)" }}>
            {(branch.conflicts || []).join("\n") || branch.note}
          </div>
          <button className="btn btn-ghost btn-sm" onClick={e => { e.stopPropagation(); onRebase(); }}>
            Ask this agent to resolve it
          </button>
        </>
      )}
      {branch.status === "stopped" && (
        <button className="btn btn-ghost btn-sm" onClick={e => { e.stopPropagation(); onRestart(); }}>
          Restart agent
        </button>
      )}
      {branch.note && branch.status !== "conflict" && (
        <div style={{ color: "var(--text3)", fontSize: 12 }}>{branch.note}</div>
      )}
    </div>
  );
}

function AgentTerminal({ projectId, branch }) {
  const [content, setContent] = useState("");
  const [offline, setOffline] = useState(true);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const bodyRef = useRef(null);
  const pinnedRef = useRef(true);

  useEffect(() => {
    const es = new EventSource(`/api/projects/${projectId}/split/branches/${branch.id}/terminal`);
    es.onmessage = (e) => {
      const data = JSON.parse(e.data);
      setOffline(!!data.offline);
      if (data.content !== undefined) setContent(data.content);
    };
    return () => es.close();
  }, [projectId, branch.id]);

  useEffect(() => {
    if (pinnedRef.current && bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [content]);

  const handleScroll = () => {
    if (!bodyRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = bodyRef.current;
    pinnedRef.current = scrollHeight - scrollTop - clientHeight < 60;
  };

  const send = async (e) => {
    e.preventDefault();
    const text = input.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      await api.splitBranchMessage(projectId, branch.id, text);
      setInput("");
    } catch (_) {}
    setSending(false);
  };

  return (
    <div className="terminal-container split-terminal">
      <div className="terminal-header">
        <div className="terminal-dots">
          <span className="dot red" /><span className="dot yellow" /><span className="dot green" />
        </div>
        <span className="terminal-title">{branch.name} — {branch.branch}</span>
        <span className={`terminal-status ${offline ? "offline" : "online"}`}>
          {offline ? "● offline" : "● live"}
        </span>
      </div>
      <div className="terminal-body" ref={bodyRef} onScroll={handleScroll}>
        {offline
          ? <div className="terminal-offline">This agent's session isn't running. Use Restart agent to bring it back.</div>
          : <pre className="terminal-output">{content}</pre>}
      </div>
      <form className="terminal-input-row" onSubmit={send}>
        <span className="terminal-prompt">❯</span>
        <textarea
          className="terminal-input"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(e); } }}
          placeholder={offline ? "Agent session stopped…" : `Correct just this agent…`}
          disabled={offline || sending}
          rows={1}
        />
        <span className="terminal-send-hint">{sending ? "sending…" : "↵"}</span>
      </form>
    </div>
  );
}
