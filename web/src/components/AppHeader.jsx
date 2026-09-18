import { ArrowLeft, Search, X } from "lucide-react";

export function AppHeader({
  user = "guest", summary = "", player = false,
  query = "", onQueryChange, children, searchPlaceholder = "搜索标题、编号或标签，输入即筛选",
}) {
  return (
    <header className={`site-header${player ? " player-header" : ""}`}>
      <div className="header-inner">
        <div className="header-primary">
          {player && (
            <a className="back-link icon-text-button" href="/">
              <ArrowLeft size={17} aria-hidden="true" />
              <span>返回首页</span>
            </a>
          )}
          <a className="brand" href="/" aria-label="返回首页">
            <span className="brand-mark"><i>AV</i>TOOL</span>
            <span className="brand-subtitle">个人视频库</span>
          </a>
          {summary && <span className="catalog-summary">{summary}</span>}
          <span className="header-spacer" />
          {!player && <a className="button" href="/sources.html">导入链接</a>}
          <span className="user-badge" title={`当前用户：${user}`}>
            <span className="user-dot" aria-hidden="true" />
            <span className="user-name">{user}</span>
          </span>
        </div>
        {!player && (
          <div className="header-workspace">
            <label className="search-box">
              <Search size={17} aria-hidden="true" />
              <span className="sr-only">搜索影片</span>
              <input
                type="search"
                value={query}
                onChange={(event) => onQueryChange(event.target.value)}
                placeholder={searchPlaceholder}
                autoComplete="off"
                enterKeyHint="search"
              />
              {query && (
                <button
                  className="bare-icon search-clear"
                  type="button"
                  title="清除搜索"
                  aria-label="清除搜索"
                  onClick={() => onQueryChange("")}
                >
                  <X size={17} />
                </button>
              )}
            </label>
            {children}
          </div>
        )}
      </div>
    </header>
  );
}
