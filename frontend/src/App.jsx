import { useState, useEffect } from "react";
import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Project from "./pages/Project";
import Running from "./pages/Running";
import CloseAllModal from "./components/CloseAllModal";
import { api } from "./api";
import "./App.css";

export default function App() {
  const [showCloseModal, setShowCloseModal] = useState(false);
  const [runningCount, setRunningCount] = useState(0);
  const [unreadCount, setUnreadCount] = useState(0);

  useEffect(() => {
    const poll = () => api.getRunning().then(list => {
      setRunningCount(list.length);
      setUnreadCount(list.reduce((n, p) => n + (p.unread || 0), 0));
    }).catch(() => {});
    poll();
    const t = setInterval(poll, 5000);
    return () => clearInterval(t);
  }, []);

  return (
    <BrowserRouter>
      <div className="app">
        <nav className="sidebar">
          <div className="sidebar-logo">
            <span className="logo-icon">⚡</span>
            <span className="logo-text">Claude Manager</span>
          </div>
          <div className="sidebar-section">Workspace</div>
          <NavLink to="/" end className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
            <span className="nav-icon">🗂️</span> Projects
          </NavLink>
          <NavLink to="/running" className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
            <span className="nav-icon">⚡</span> Running
            {runningCount > 0 && (
              <span className={"nav-badge" + (unreadCount > 0 ? " alert" : "")}>
                {unreadCount > 0 ? unreadCount : runningCount}
              </span>
            )}
          </NavLink>
          <div className="sidebar-footer">
            <button
              className="btn btn-ghost btn-sm btn-block"
              onClick={() => setShowCloseModal(true)}
              disabled={showCloseModal}
            >
              ⏻ Close All
            </button>
          </div>
        </nav>
        <main className="content">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/project/:id" element={<Project />} />
            <Route path="/running" element={<Running />} />
          </Routes>
        </main>
        {showCloseModal && <CloseAllModal onClose={() => setShowCloseModal(false)} />}
      </div>
    </BrowserRouter>
  );
}
