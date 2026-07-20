const BASE = "/api";

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

  // Session control
  startProject: (id) => fetch(`${BASE}/projects/${id}/start`, { method: "POST" }).then(r => r.json()),
  pauseProject: (id) => fetch(`${BASE}/projects/${id}/pause`, { method: "POST" }).then(r => r.json()),
  closeAll: () => fetch(`${BASE}/close-all`, { method: "POST" }).then(r => r.json()),

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

  uploadFile: (id, file) => {
    const fd = new FormData();
    fd.append("file", file, file.name || "pasted-image.png");
    return fetch(`${BASE}/projects/${id}/upload`, { method: "POST", body: fd }).then(r => r.json());
  },

  // Project files (preview)
  listFiles: (id, path = "") =>
    fetch(`${BASE}/projects/${id}/fs-list?path=${encodeURIComponent(path)}`).then(r => r.json()),

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

  // Testing
  getTests: (id) => fetch(`${BASE}/projects/${id}/tests`).then(r => r.json()),
  createTest: (id, data) => fetch(`${BASE}/projects/${id}/tests`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  }).then(r => r.json()),
  runTests: (id) => fetch(`${BASE}/projects/${id}/tests/run`, { method: "POST" }).then(r => r.json()),

  // Running
  getRunning: () => fetch(`${BASE}/running`).then(r => r.json()),
};

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
