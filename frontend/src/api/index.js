const BASE = "/api";

// Every API call goes through here so a 401 (login cookie missing or expired)
// sends the browser to the login page instead of leaving a blank app.
function fetch(url, opts) {
  return window.fetch(url, opts).then(r => {
    if (r.status === 401 && !window.location.pathname.startsWith("/login")) {
      window.location.assign("/login");
      return new Promise(() => {}); // navigation is happening; never resolve
    }
    return r;
  });
}

export const api = {
  // Projects
  getProjects: () => fetch(`${BASE}/projects`).then(r => r.json()),
  createProject: (data) => fetch(`${BASE}/projects`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }).then(r => r.json()),
  getProject: (id) => fetch(`${BASE}/projects/${id}`).then(r => r.json()),
  deleteProject: (id) => fetch(`${BASE}/projects/${id}`, { method: "DELETE" }).then(r => r.json()),
  updateProject: (id, data) => fetch(`${BASE}/projects/${id}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }).then(r => r.json()),
  generateDescription: (id) => fetch(`${BASE}/projects/${id}/generate-description`, { method: "POST" }).then(r => r.json()),

  // Session control
  startProject: (id) => fetch(`${BASE}/projects/${id}/start`, { method: "POST" }).then(r => r.json()),
  pauseProject: (id) => fetch(`${BASE}/projects/${id}/pause`, { method: "POST" }).then(r => r.json()),
  closeAll: () => fetch(`${BASE}/close-all`, { method: "POST" }).then(r => r.json()),

  // Daily report
  getEmailConfig: () => fetch(`${BASE}/email-config`).then(r => r.json()),
  sendDailyReport: () => fetch(`${BASE}/daily-report`, { method: "POST" }).then(r => r.json()),

  // Chat
  markSeen: (id) => fetch(`${BASE}/projects/${id}/seen`, { method: "POST" }).then(r => r.json()),
  getMessages: (id) => fetch(`${BASE}/projects/${id}/messages`).then(r => r.json()),
  sendMessage: (id, message) => fetch(`${BASE}/projects/${id}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  }).then(r => r.json()),

  scrollTerminal: (id, direction, count = 1) => fetch(`${BASE}/projects/${id}/scroll`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ direction, count }),
  }).then(r => r.json()),

  sendKeys: (id, keys) => fetch(`${BASE}/projects/${id}/send-keys`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keys }),
  }).then(r => r.json()),

  typeText: (id, text) => fetch(`${BASE}/projects/${id}/type`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  }).then(r => r.json()),

  uploadFile: (id, file) => {
    const fd = new FormData();
    fd.append("file", file, file.name || "pasted-image.png");
    return fetch(`${BASE}/projects/${id}/upload`, { method: "POST", body: fd }).then(r => r.json());
  },

  // Project files (preview)
  listFiles: (id, path = "") =>
    fetch(`${BASE}/projects/${id}/fs-list?path=${encodeURIComponent(path)}`).then(r => r.json()),

  // Phone notifications (Web Push)
  getPushState: () => fetch(`${BASE}/push/state`).then(r => r.json()),
  subscribePush: (data) => fetch(`${BASE}/push/subscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }).then(r => r.json()),
  unsubscribePush: (endpoint) => fetch(`${BASE}/push/unsubscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoint }),
  }).then(r => r.json()),
  savePushSettings: (data) => fetch(`${BASE}/push/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }).then(r => r.json()),
  testPush: (endpoint) => fetch(`${BASE}/push/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoint }),
  }).then(r => r.json()),

  // Import
  getImportCandidates: () => fetch(`${BASE}/import/candidates`).then(r => r.json()),
  importProjects: (projects) => fetch(`${BASE}/import`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ projects }),
  }).then(r => r.json()),

  // Memory
  getMemory: (id) => fetch(`${BASE}/projects/${id}/memory`).then(r => r.json()),
  updateMemory: (id, type, content, component = null) => fetch(`${BASE}/projects/${id}/memory`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type, content, component }),
  }).then(r => r.json()),
  getGlobalMemory: () => fetch(`${BASE}/global-memory`).then(r => r.json()),
  updateGlobalMemory: (content) => fetch(`${BASE}/global-memory`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  }).then(r => r.json()),

  // Testing
  getTests: (id) => fetch(`${BASE}/projects/${id}/tests`).then(r => r.json()),
  createTest: (id, data) => fetch(`${BASE}/projects/${id}/tests`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }).then(r => r.json()),
  runTests: (id) => fetch(`${BASE}/projects/${id}/tests/run`, { method: "POST" }).then(r => r.json()),
  testImageUrl: (id, testId, kind) => `${BASE}/projects/${id}/tests/${testId}/image/${kind}`,
  acceptTestBaseline: (id, testId) => fetch(`${BASE}/projects/${id}/tests/${testId}/accept-baseline`, { method: "POST" }).then(r => r.json()),

  // Running
  getRunning: () => fetch(`${BASE}/running`).then(r => r.json()),
  getContext: (id) => fetch(`${BASE}/projects/${id}/context`).then(r => r.json()),
  trimContext: (id) => fetch(`${BASE}/projects/${id}/context/trim`, { method: "POST" }).then(r => r.json()),

  // Council
  getCouncil: (id) => fetch(`${BASE}/projects/${id}/council`).then(r => r.json()),
  runCouncil: (id) => fetch(`${BASE}/projects/${id}/council/run`, { method: "POST" }).then(r => r.json()),

  // Split (parallel mini-agents)
  getSplit: (id) => fetch(`${BASE}/projects/${id}/split`).then(r => r.json()),
  planSplit: (id, prompt, count) => post(`${BASE}/projects/${id}/split/plan`, { prompt, count }),
  launchSplit: (id, prompt, branches) => post(`${BASE}/projects/${id}/split/launch`, { prompt, branches }),
  mergeSplit: (id, commitBase = false) => post(`${BASE}/projects/${id}/split/merge`, { commit_base: commitBase }),
  cleanupSplit: (id, deleteBranches = false) => post(`${BASE}/projects/${id}/split/cleanup`, { delete_branches: deleteBranches }),
  splitBranchMessage: (id, bid, message) => post(`${BASE}/projects/${id}/split/branches/${bid}/message`, { message }),
  splitBranchRebase: (id, bid) => post(`${BASE}/projects/${id}/split/branches/${bid}/rebase`, {}),
  splitBranchRestart: (id, bid) => post(`${BASE}/projects/${id}/split/branches/${bid}/restart`, {}),
};

function post(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then(r => r.json());
}

export const streamMessages = (projectId, onMessage) => {
  const es = new EventSource(`/api/projects/${projectId}/stream`);
  es.onmessage = (e) => onMessage(JSON.parse(e.data));
  es.onerror = () => { es.close(); setTimeout(() => streamMessages(projectId, onMessage), 2000); };
  return () => es.close();
};

export const streamRunning = (onUpdate) => {
  const es = new EventSource(`/api/running/stream`);
  es.onmessage = (e) => onUpdate(JSON.parse(e.data));
  es.onerror = () => { es.close(); setTimeout(() => streamRunning(onUpdate), 2000); };
  return () => es.close();
};

export const streamSplit = (projectId, onUpdate) => {
  const es = new EventSource(`/api/projects/${projectId}/split/stream`);
  es.onmessage = (e) => onUpdate(JSON.parse(e.data));
  es.onerror = () => { es.close(); setTimeout(() => streamSplit(projectId, onUpdate), 2000); };
  return () => es.close();
};

export const streamCouncil = (projectId, onUpdate) => {
  const es = new EventSource(`/api/projects/${projectId}/council/stream`);
  es.onmessage = (e) => onUpdate(JSON.parse(e.data));
  es.onerror = () => { es.close(); setTimeout(() => streamCouncil(projectId, onUpdate), 2000); };
  return () => es.close();
};
