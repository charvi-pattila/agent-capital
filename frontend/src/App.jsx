import { useState, useEffect } from "react";
import { BrowserRouter, Routes, Route, NavLink, useLocation } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Project from "./pages/Project";
import Running from "./pages/Running";
import CloseAllModal from "./components/CloseAllModal";
import Icon from "./components/Icon";
import { api } from "./api";
import "./App.css";

// The shell needs the current route (a project page hides the phone's bottom
// bar so the terminal gets the whole screen), and useLocation only works
// inside the router — hence App wraps Shell rather than rendering this itself.
function Shell() {
  const [showCloseModal, setShowCloseModal] = useState(false);
  const [runningCount, setRunningCount] = useState(0);
  const [unreadCount, setUnreadCount] = useState(0);
  const { pathname } = useLocation();
  const onProject = pathname.startsWith("/project/");

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
    <div className={"app" + (onProject ? " app--project" : "")}>
      <nav className="sidebar">
        <div className="sidebar-logo">
          <Icon name="logo" size={20} className="logo-icon" style={{ color: "var(--accent)" }} />
          <span className="logo-text">Agent Capitol</span>
        </div>
        <div className="sidebar-section">Workspace</div>
        <NavLink to="/" end className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
          <Icon name="grid" size={18} /> Projects
        </NavLink>
        <NavLink to="/running" className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
          <Icon name="bolt" size={18} /> Running
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
            <Icon name="power" size={16} /> Close All
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
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
}
