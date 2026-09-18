import { Component, lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Toast } from "../components/Toast";
import { requestJSON } from "../lib/api";

const LibraryPage = lazy(() => import("../pages/LibraryPage").then((module) => ({ default: module.LibraryPage })));
const PlayerPage = lazy(() => import("../pages/PlayerPage").then((module) => ({ default: module.PlayerPage })));

class AppErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("AVWEB 页面错误", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="fatal-state app-fatal-state">
          <h1>页面加载失败</h1>
          <p>{this.state.error.message || "前端发生未知错误"}</p>
          <button className="button" type="button" onClick={() => window.location.reload()}>重新载入</button>
        </div>
      );
    }
    return this.props.children;
  }
}

export function App() {
  const [user, setUser] = useState(null);
  const [toast, setToast] = useState(null);
  const timer = useRef(null);
  const isPlayer = window.location.pathname.endsWith("/watch.html") || window.location.pathname.endsWith("/watch");

  useEffect(() => {
    requestJSON("/api/me", { timeout: 10000 })
      .then((data) => setUser(data?.user ? String(data.user) : "guest"))
      .catch(() => setUser("guest"));
    return () => window.clearTimeout(timer.current);
  }, []);

  const notify = useCallback((message, type = "info") => {
    window.clearTimeout(timer.current);
    setToast({ message, type, id: Date.now() });
    timer.current = window.setTimeout(() => setToast(null), 3400);
  }, []);

  return (
    <AppErrorBoundary>
      {user === null ? (
        <div className="boot-state"><span className="boot-mark">AV</span><span>正在载入</span></div>
      ) : (
        <Suspense fallback={<div className="boot-state"><span className="boot-mark">AV</span><span>正在载入</span></div>}>
          {isPlayer
            ? <PlayerPage user={user} notify={notify} />
            : <LibraryPage user={user} notify={notify} />}
        </Suspense>
      )}
      <Toast toast={toast} />
    </AppErrorBoundary>
  );
}
