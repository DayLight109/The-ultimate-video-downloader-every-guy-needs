import { useFavoriteCache } from "../hooks/useFavoriteCache";
import {
  ArrowUp, Clock3, Download, Heart, Home, LayoutGrid, RefreshCw, Sparkles, Trash2, TrendingUp,
} from "lucide-react";
import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { AppHeader } from "../components/AppHeader";
import { CacheView } from "../components/CacheView";
import { EmptyState } from "../components/EmptyState";
import { FilterBar } from "../components/FilterBar";
import { PopularFilters } from "../components/PopularFilters";
import { VideoCard } from "../components/VideoCard";
import { VideoRow } from "../components/VideoRow";
import { useCache } from "../hooks/useCache";
import { useCatalog } from "../hooks/useCatalog";
import { useStoredList } from "../hooks/useStoredList";
import { useSourcePopularity } from "../hooks/useSourcePopularity";
import { parseDuration, searchText, siteName, videoKey } from "../lib/format";
import { progressRatio } from "../lib/storage";
import { POPULAR_DEFAULTS, readPopularFilters } from "../lib/popularity";

const PAGE_SIZE = 48;
const ROW_SIZE = 12;
const RECENT_WINDOW = 7 * 24 * 60 * 60 * 1000;
const VIEWS = [
  { id: "home", label: "首页", Icon: Home },
  { id: "library", label: "片库", Icon: LayoutGrid },
  { id: "popular", label: "热门", Icon: TrendingUp },
  { id: "favorites", label: "收藏", Icon: Heart },
  { id: "history", label: "历史", Icon: Clock3 },
  { id: "cache", label: "缓存", Icon: Download },
];
const VIEW_IDS = new Set(VIEWS.map((view) => view.id));
const SORT_OPTIONS = [
  { id: "default", label: "目录顺序" },
  { id: "recent", label: "日期较新" },
  { id: "views", label: "播放量较高" },
  { id: "duration", label: "时长较长" },
  { id: "local", label: "云盘优先" },
];

function readURLState() {
  const params = new URLSearchParams(window.location.search);
  const view = params.get("view") || "";
  const site = params.get("site") || "";
  return {
    view: VIEW_IDS.has(view) ? view : (site || params.get("q") || params.get("tag") || params.get("recent") === "1") ? "library" : "home",
    site,
    tag: view === "popular" ? "" : params.get("tag") || "",
    query: params.get("q") || "",
    recentOnly: view !== "popular" && params.get("recent") === "1",
    sort: SORT_OPTIONS.some((option) => option.id === params.get("sort")) ? params.get("sort") : "default",
    ...(view === "popular" ? readPopularFilters(params) : POPULAR_DEFAULTS),
    page: view === "popular" && /^\d{1,6}$/.test(params.get("page") || "") ? Math.max(1, Math.min(100000, Number(params.get("page")))) : 1,
  };
}

function writeURLState(state, push) {
  const params = new URLSearchParams();
  if (state.view !== "home") params.set("view", state.view);
  if (state.site) params.set("site", state.site);
  if (state.tag) params.set("tag", state.tag);
  if (state.query) params.set("q", state.query);
  if (state.recentOnly) params.set("recent", "1");
  if (state.sort && state.sort !== "default") params.set("sort", state.sort);
  if (state.view === "popular") {
    if (state.page > 1) params.set("page", String(state.page));
    state.topics.forEach((tag) => params.append("topic", tag));
    if (state.duration) params.set("duration", state.duration);
    if (state.age) params.set("age", state.age);
    if (state.unwatched) params.set("unwatched", "1");
    if (state.cloudOnly) params.set("cloud", "1");
    if (state.exclude) params.set("exclude", state.exclude);
  }
  const search = params.toString();
  const url = `${window.location.pathname}${search ? `?${search}` : ""}`;
  if (url === `${window.location.pathname}${window.location.search}`) return;
  try {
    window.history[push ? "pushState" : "replaceState"](null, "", url);
  } catch {
    // Safari rate-limits history updates while typing; the in-memory state still wins.
  }
}

function byViewOrder(a, b) {
  return (a._viewOrder ?? a._order ?? 0) - (b._viewOrder ?? b._order ?? 0);
}

export function LibraryPage({ user, notify }) {
  const [nav, setNav] = useState(readURLState);
  // Popular can load independently. Start the full catalog only once a view
  // needs it, then keep that catalog session alive across navigation.
  const [catalogEnabled, setCatalogEnabled] = useState(() => nav.view !== "popular");
  useEffect(() => { if (nav.view !== "popular") setCatalogEnabled(true); }, [nav.view]);
  const catalog = useCatalog({ enabled: catalogEnabled });
  const cache = useCache();
  const [favorites] = useStoredList("favorites", user);
  const [history, setHistory] = useStoredList("history", user);
  const navRef = useRef(nav);
  navRef.current = nav;
  const { sort } = nav;
  const [limit, setLimit] = useState(PAGE_SIZE);
  const [busyKey, setBusyKey] = useState("");
  const actionPending = useRef(false);
  const [showTop, setShowTop] = useState(false);
  const [compact, setCompact] = useState(false);
  const sentinelRef = useRef(null);

  const { site, tag, recentOnly } = nav;
  const query = nav.query.trim();
  const deferredQuery = useDeferredValue(query.toLowerCase());
  const deferredExclude = useDeferredValue(nav.exclude.trim().toLowerCase());
  // Typing on the home page turns it into a search result grid.
  const gridView = nav.view === "home" && deferredQuery ? "library" : nav.view;
  const isGrid = gridView !== "home" && gridView !== "cache";

  const navigate = useCallback((patch, { push = false } = {}) => {
    const next = { ...navRef.current, page: 1, ...patch };
    navRef.current = next;
    writeURLState(next, push);
    setNav(next);
  }, []);

  useEffect(() => {
    const onPop = () => { const next = readURLState(); navRef.current = next; setNav(next); };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    const onKey = (event) => {
      if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      if (target && ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      event.preventDefault();
      document.querySelector(".search-box input")?.focus();
    };
    const onScroll = () => {
      const y = window.scrollY;
      setShowTop(y > 900);
      // Hysteresis keeps the brand row from flickering around the threshold.
      setCompact((current) => (current ? y > 80 : y > 240));
    };
    onScroll();
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll);
    };
  }, []);

  useEffect(() => { window.scrollTo(0, 0); }, [nav.view, site, tag, recentOnly]);

  const lookup = useMemo(
    () => new Map(catalog.videos.map((video) => [videoKey(video), video])),
    [catalog.videos],
  );
  const favoriteKeys = useMemo(() => new Set(favorites.map(videoKey)), [favorites]);
  const historyKeys = useMemo(() => new Set(history.map(videoKey)), [history]);
  const popularPage = gridView === "popular" ? nav.page : 1;
  const changePopularPage = useCallback((page, push = true) => {
    navigate({ page }, { push });
    window.scrollTo(0, 0);
  }, [navigate]);
  const popularRequest = gridView === "popular" ? {
    site, query: deferredQuery, exclude: deferredExclude, topics: nav.topics,
    duration: nav.duration, age: nav.age, unwatched: nav.unwatched, cloudOnly: nav.cloudOnly, sort,
  } : { sort: "default" };
  const popularity = useSourcePopularity(popularRequest, popularPage, historyKeys, cache.localSignature, changePopularPage);
  const progressByKey = useMemo(
    () => new Map(history.map((entry) => [videoKey(entry), progressRatio(entry)])),
    [history],
  );

  const storedVideos = useCallback((records) => records.map((record, index) => {
    const found = lookup.get(videoKey(record));
    return found
      ? { ...record, ...found, _addedAt: found._addedAt, _viewOrder: index }
      : { ...record, title: record.title || videoKey(record), tags: [], _search: searchText(record), _viewOrder: index };
  }), [lookup]);

  const localVideos = useMemo(() => cache.records
    .slice()
    .sort((a, b) => Number(b.cached_at || 0) - Number(a.cached_at || 0))
    .map((record) => {
      const found = lookup.get(videoKey(record));
      return found || { ...record, title: record.title || record.code, tags: [], has_local: true };
    }), [cache.records, lookup]);

  const popularMetadata = popularity.sites;

  const listMetadata = useMemo(() => {
    const source = gridView === "favorites" ? favorites : gridView === "history" ? history : null;
    if (!source) return null;
    const counts = new Map();
    source.forEach((entry) => counts.set(entry.site, (counts.get(entry.site) || 0) + 1));
    const order = new Map(catalog.metadata.map((entry, index) => [entry.site, index]));
    return [...counts.entries()]
      .map(([key, count]) => ({ site: key, count }))
      .sort((a, b) => (order.get(a.site) ?? 99) - (order.get(b.site) ?? 99) || b.count - a.count);
  }, [catalog.metadata, favorites, gridView, history]);

  const sortOptions = useMemo(() => [
    {
      id: "default",
      label: gridView === "popular" ? (site ? "站内热度" : "各站热门优先") : gridView === "favorites" ? "收藏先后" : gridView === "history" ? "观看先后" : "默认（最新入库）",
    },
    ...SORT_OPTIONS.slice(1),
  ], [gridView, site]);

  const filtered = useMemo(() => {
    if (!isGrid) return [];
    if (gridView === "popular") return popularity.all;
    const source = gridView === "favorites"
      ? storedVideos(favorites)
      : gridView === "history"
        ? storedVideos(history)
        : catalog.videos;
    const cutoff = Date.now() - RECENT_WINDOW;
    let result = source.filter((video) => {
      if (site && video.site !== site) return false;
      if (tag && !video.tags?.includes(tag)) return false;
      if (recentOnly && Number(video._addedAt || 0) < cutoff) return false;
      return !deferredQuery || (video._search || searchText(video)).includes(deferredQuery);
    });
    if (sort === "default" && recentOnly) {
      result = result.slice().sort((a, b) => Number(b._addedAt || 0) - Number(a._addedAt || 0) || byViewOrder(a, b));
    } else if (sort === "local") {
      result = result.slice().sort((a, b) =>
        Number(cache.localKeys.has(videoKey(b))) - Number(cache.localKeys.has(videoKey(a)))
        || byViewOrder(a, b));
    } else if (sort === "recent") {
      result = result.slice().sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")) || byViewOrder(a, b));
    } else if (sort === "views") {
      result = result.slice().sort((a, b) => Number(b.source_views || 0) - Number(a.source_views || 0) || byViewOrder(a, b));
    } else if (sort === "duration") {
      result = result.slice().sort((a, b) => parseDuration(b.duration) - parseDuration(a.duration) || byViewOrder(a, b));
    } else if (gridView !== "library") {
      result = result.slice().sort(byViewOrder);
    }
    return result;
  }, [cache.localKeys, catalog.videos, deferredQuery, favorites, gridView, history, isGrid, popularity.all, recentOnly, site, sort, storedVideos, tag]);

  const tagStats = useMemo(() => {
    if (!catalog.complete) return null;
    const perSite = new Map();
    const global = new Map();
    catalog.videos.forEach((video) => {
      if (!video.tags?.length) return;
      let counts = perSite.get(video.site);
      if (!counts) { counts = new Map(); perSite.set(video.site, counts); }
      video.tags.forEach((value) => {
        if (!value) return;
        counts.set(value, (counts.get(value) || 0) + 1);
        global.set(value, (global.get(value) || 0) + 1);
      });
    });
    const sortEntries = (map) => [...map.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    const result = new Map([["", sortEntries(global)]]);
    perSite.forEach((counts, key) => result.set(key, sortEntries(counts)));
    return result;
  }, [catalog.complete, catalog.videos]);

  const home = useMemo(() => {
    const cutoff = Date.now() - RECENT_WINDOW;
    const perSite = new Map(catalog.metadata.map((entry) => [entry.site, []]));
    const recent = [];
    catalog.videos.forEach((video) => {
      const list = perSite.get(video.site);
      if (list && list.length < ROW_SIZE) list.push(video);
      if (Number(video._addedAt || 0) >= cutoff) recent.push(video);
    });
    recent.sort((a, b) => Number(b._addedAt || 0) - Number(a._addedAt || 0));
    return { perSite, recent: recent.slice(0, ROW_SIZE) };
  }, [catalog.metadata, catalog.videos]);
  const continueWatching = useMemo(() => storedVideos(
    history.filter((entry) => { const ratio = progressRatio(entry); return ratio > 0.01 && ratio < 0.98; }).slice(0, ROW_SIZE),
  ), [history, storedVideos]);
  const recentHistory = useMemo(() => storedVideos(history.slice(0, ROW_SIZE)), [history, storedVideos]);
  const favoriteRow = useMemo(() => storedVideos(favorites.slice(0, ROW_SIZE)), [favorites, storedVideos]);

  useEffect(() => {
    if (catalog.sessionNewCount > 0) notify(`发现 ${catalog.sessionNewCount.toLocaleString()} 部新增影片`, "success");
  }, [catalog.sessionNewCount, notify]);

  useEffect(() => setLimit(PAGE_SIZE), [deferredQuery, site, sort, tag, gridView, recentOnly, nav.topics, nav.duration, nav.age, nav.unwatched, nav.cloudOnly, deferredExclude]);
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel || !isGrid || gridView === "popular" || limit >= filtered.length) return undefined;
    const observer = new IntersectionObserver((entries) => {
      if (entries[0]?.isIntersecting) setLimit((current) => current + PAGE_SIZE);
    }, { rootMargin: "800px 0px" });
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [filtered.length, isGrid, limit, gridView]);

  const toggleFavorite = useFavoriteCache(user, notify, () => cache.refresh(true));

  const runAction = async (record, action, success) => {
    if (actionPending.current) return;
    actionPending.current = true;
    const key = videoKey(record);
    setBusyKey(key);
    try {
      const result = await action();
      notify(result?.msg || success, "success");
    } catch (caught) {
      notify(caught.message || "操作失败", "error");
    } finally {
      actionPending.current = false;
      setBusyKey("");
    }
  };

  const startCache = (video) => runAction(video, () => cache.start(video), "已加入缓存队列，可在「缓存」中查看进度");
  const startBatch = async (items) => {
    if (actionPending.current) throw new Error("其他操作正在提交，请稍后再试");
    actionPending.current = true;
    setBusyKey("batch");
    try {
      const result = await cache.startBatch(items);
      notify(result.msg, result.failed?.length ? "info" : "success");
      return result;
    } finally {
      actionPending.current = false;
      setBusyKey("");
    }
  };
  const clearRecord = (record) => {
    if (!window.confirm("删除云盘上的这部影片？")) return;
    runAction(record, () => cache.clear(record.site, record.code), "缓存已删除");
  };
  const cancelJob = (job) => {
    if (!window.confirm("终止这个缓存任务？")) return;
    runAction(job, () => cache.cancel(job.site, job.code), "终止请求已提交");
  };
  const clearAll = async () => {
    if (actionPending.current) return;
    if (!window.confirm(`删除云盘上的全部 ${cache.records.length} 部影片？`)) return;
    actionPending.current = true;
    setBusyKey("all");
    try {
      const result = await cache.clearAll();
      const skipped = Array.isArray(result.skipped) ? result.skipped.length : 0;
      notify(
        skipped ? `已删除 ${result.cleared} 部，${skipped} 部运行中未删除` : `已删除 ${result.cleared} 部缓存`,
        skipped ? "info" : "success",
      );
    } catch (caught) {
      notify(caught.message || "部分缓存删除失败，请刷新后重试", "error");
    } finally {
      actionPending.current = false;
      setBusyKey("");
    }
  };
  const clearHistory = () => {
    if (!window.confirm("清空全部观看历史？")) return;
    setHistory([]);
    notify("观看历史已清空");
  };

  const goto = (view, patch = {}) => navigate({ view, site: "", tag: "", query: "", recentOnly: false, sort: "default", ...POPULAR_DEFAULTS, ...patch }, { push: true });
  const gotoSite = (nextSite) => goto("library", { site: nextSite, tag: "", recentOnly: false, query: "" });
  const clearFilters = () => navigate({ site: "", tag: "", query: "", recentOnly: false, sort: "default", ...POPULAR_DEFAULTS }, { push: true });
  const toggleTopic = (value) => navigate({ topics: nav.topics.includes(value) ? nav.topics.filter((tag) => tag !== value) : [...nav.topics, value] }, { push: true });
  const hasFilters = Boolean(site || tag || query || recentOnly || sort !== "default"
    || (gridView === "popular" && (nav.topics.length || nav.duration || nav.age || nav.unwatched || nav.cloudOnly || nav.exclude)));
  const countFor = (id) => {
    if (id === "library") return catalogEnabled ? catalog.videos.length : null;
    if (id === "popular") return popularity.total;
    if (id === "favorites") return favorites.length;
    if (id === "history") return history.length;
    if (id === "cache") return cache.activeCount ? `${cache.records.length}+${cache.activeCount}` : cache.records.length;
    return null;
  };

  const loadingNote = !catalog.complete
    ? `正在载入目录 ${catalog.loadedParts}/${catalog.totalParts || "-"}`
    : catalog.syncing
      ? "正在检查目录更新"
      : "";
  const summary = gridView === "popular"
    ? `热度榜 ${popularity.total.toLocaleString()} 部 · ${popularMetadata.length} 个站点 · 已存云盘 ${cache.records.length} 部`
    : catalog.error && !catalog.videos.length
    ? "目录加载失败"
    : `共 ${catalog.videos.length.toLocaleString()} 部 · 已缓存 ${cache.records.length} 部${loadingNote ? ` · ${loadingNote}` : ""}`;

  const renderCard = (video, options = {}) => {
    const key = videoKey(video);
    return (
      <VideoCard
        key={key}
        video={video}
        favorite={favoriteKeys.has(key)}
        watched={historyKeys.has(key)}
        local={cache.localKeys.has(key)}
        job={cache.jobsByKey.get(key)}
        progress={progressByKey.get(key) || 0}
        showSite={options.showSite ?? !site}
        busy={Boolean(busyKey)}
        onFavorite={toggleFavorite}
        onCache={startCache}
        popular={gridView === "popular"}
        selectedTags={gridView === "popular" ? nav.topics : undefined}
        onTag={gridView === "popular" ? toggleTopic : undefined}
      />
    );
  };

  const heading = (() => {
    if (gridView === "popular") {
      return {
        kicker: "热门榜",
        title: site ? `${siteName(site)} 热门` : "全站热门",
        copy: `从 ${Number(site ? popularMetadata.find((entry) => entry.site === site)?.count || 0 : popularity.total).toLocaleString()} 部有播放量的影片中筛选${popularity.updatedAt ? ` · 榜单更新于 ${new Date(popularity.updatedAt * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" })}` : ""}`,
      };
    }
    if (gridView === "favorites") return { kicker: "我的收藏", title: site ? `收藏 · ${siteName(site)}` : "我的收藏", copy: "" };
    if (gridView === "history") return { kicker: "观看历史", title: site ? `历史 · ${siteName(site)}` : "观看历史", copy: "记录播放进度，点击卡片可从上次位置继续" };
    const parts = [];
    if (recentOnly) parts.push("最近新增");
    parts.push(site ? siteName(site) : "全部影片");
    if (tag) parts.push(tag);
    const scope = [site ? siteName(site) : "", tag].filter(Boolean).join(" · ");
    return {
      kicker: query ? "搜索结果" : "片库",
      title: query ? `搜索“${query}”` : parts.join(" · "),
      copy: query
        ? (scope ? `在 ${scope} 范围内搜索标题、编号和标签` : "在全部站点中搜索标题、编号和标签")
        : recentOnly ? "最近 7 天新抓取的影片，最新的在前" : "按站点或标签筛选，输入关键字即时搜索",
    };
  })();

  const emptyState = (() => {
    if (gridView === "library" && !catalog.complete && !catalog.error) {
      return { loading: true, title: "正在读取目录", copy: "已读取的内容会立即显示" };
    }
    if (gridView === "popular" && popularity.loading) return { loading: true, title: "正在读取热门榜", copy: "播放量数据载入后会显示在这里" };
    if (gridView === "popular" && popularity.error && !popularity.all.length) return { title: "热门榜加载失败", copy: "请点击刷新榜单重试" };
    if (gridView === "popular" && hasFilters) return { title: "没有匹配的热门影片", copy: "试试清除筛选条件", action: "清除筛选", onAction: clearFilters };
    if (gridView === "popular") return { title: site ? "该站点暂无播放量数据" : "热门榜暂无数据", copy: "只有源站公开播放量的影片会进入热门榜", action: site ? "查看全站热门" : undefined, onAction: () => navigate({ site: "" }) };
    if (gridView === "favorites") return { title: hasFilters ? "没有匹配的收藏" : "还没有收藏", copy: hasFilters ? "试试清除筛选条件" : "在影片卡片或播放页点击心形按钮即可收藏", action: hasFilters ? "清除筛选" : "去片库挑选", onAction: hasFilters ? clearFilters : () => gotoSite("") };
    if (gridView === "history") return { title: hasFilters ? "没有匹配的记录" : "还没有观看记录", copy: hasFilters ? "试试清除筛选条件" : "播放过的影片会出现在这里，并记住播放进度", action: hasFilters ? "清除筛选" : "去片库挑选", onAction: hasFilters ? clearFilters : () => gotoSite("") };
    if (recentOnly) return { title: "最近没有新增影片", copy: "首次载入目录后，之后抓取到的新影片会出现在这里", action: "查看全部影片", onAction: () => navigate({ recentOnly: false }) };
    return { title: "没有匹配结果", copy: "换个关键字，或清除筛选条件后再试", action: hasFilters ? "清除筛选" : undefined, onAction: clearFilters };
  })();

  return (
    <>
      <div className={`top-chrome${compact ? " is-compact" : ""}`}>
      <AppHeader
        user={user}
        summary={summary}
        query={nav.query}
        searchPlaceholder={gridView === "popular" ? "搜索热门：标题、编号或标签，多个词用空格分隔" : undefined}
        onQueryChange={(value) => {
          const changeView = ["home", "cache"].includes(nav.view) && Boolean(value.trim());
          navigate({ query: value, ...(changeView ? { view: "library", site: "", tag: "", recentOnly: false, sort: "default" } : {}) }, { push: changeView });
          window.scrollTo(0, 0);
        }}
      />

      <nav className="view-bar" aria-label="主导航">
        <div className="view-bar-inner">
          {VIEWS.map(({ id, label, Icon }) => {
            const count = countFor(id);
            const current = nav.view === id;
            return (
              <button className={`view-tab${current ? " is-active" : ""}`} type="button" key={id} aria-current={current ? "page" : undefined} onClick={() => goto(id, { site: "", tag: "", query: "", recentOnly: false })}>
                <Icon size={16} aria-hidden="true" /><span>{label}</span>
                {count !== null && <small>{typeof count === "number" ? count.toLocaleString() : count}</small>}
              </button>
            );
          })}
        </div>
      </nav>
      </div>

      <main className="app-shell">
        {gridView !== "popular" && catalog.error && <div className="inline-error">{catalog.error}</div>}
        {gridView !== "popular" && catalog.cacheError && <div className="inline-warning">{catalog.cacheError}</div>}

        {gridView === "cache" && (
          <CacheView
            cache={cache}
            catalog={catalog}
            onStartBatch={startBatch}
            busyKey={busyKey}
            onRefresh={() => cache.refresh().catch((caught) => notify(caught.message, "error"))}
            onCancel={cancelJob}
            onRemove={(job) => runAction(job, () => cache.removeJob(job.site, job.code), "任务记录已移除")}
            onRetry={(job) => runAction(job, () => cache.start(job), "任务已重新提交")}
            onClear={clearRecord}
            onClearAll={clearAll}
            onBrowse={() => gotoSite("")}
          />
        )}

        {gridView === "home" && (
          <div className="home-view">
            {catalog.sessionNewCount > 0 && (
              <section className="update-notice" aria-live="polite">
                <Sparkles size={17} aria-hidden="true" />
                <span>目录刚刚新增 <strong>{catalog.sessionNewCount.toLocaleString()}</strong> 部影片</span>
                <button type="button" onClick={() => goto("library", { recentOnly: true, site: "", tag: "" })}>查看新增</button>
              </section>
            )}
            {loadingNote && !catalog.videos.length && (
              <EmptyState loading title="正在读取目录" copy="首次打开需要下载完整目录，之后会缓存在浏览器中" />
            )}
            {continueWatching.length > 0 && (
              <VideoRow kicker="继续观看" title="上次看到一半" items={continueWatching} onAction={() => goto("history")} actionLabel="全部历史" renderItem={(video) => renderCard(video, { showSite: true })} />
            )}
            {localVideos.length > 0 && (
              <VideoRow kicker="云盘" title="已保存到云盘" count={cache.records.length} items={localVideos.slice(0, ROW_SIZE)} onAction={() => goto("cache")} actionLabel="下载管理" renderItem={(video) => renderCard(video, { showSite: true })} />
            )}
            {favoriteRow.length > 0 && (
              <VideoRow kicker="收藏" title="我的收藏" count={favorites.length} items={favoriteRow} onAction={() => goto("favorites")} renderItem={(video) => renderCard(video, { showSite: true })} />
            )}
            {!continueWatching.length && recentHistory.length > 0 && (
              <VideoRow kicker="历史" title="最近观看" count={history.length} items={recentHistory} onAction={() => goto("history")} renderItem={(video) => renderCard(video, { showSite: true })} />
            )}
            {home.recent.length > 0 && (
              <VideoRow kicker="新增" title="最近入库" description="最近 7 天新抓取的影片" count={catalog.newCount} items={home.recent} onAction={() => goto("library", { recentOnly: true, site: "", tag: "" })} renderItem={(video) => renderCard(video, { showSite: true })} />
            )}
            {popularity.all.length > 0 && (
              <VideoRow kicker="热门" title="全站热门" description="各站热门交错展示，按偏好挑选" items={popularity.all.slice(0, ROW_SIZE)} onAction={() => goto("popular", { site: "" })} actionLabel="完整榜单" renderItem={(video) => renderCard(video, { showSite: true })} />
            )}
            {catalog.metadata.map((entry) => {
              const items = home.perSite.get(entry.site) || [];
              if (!items.length) return null;
              return (
                <VideoRow
                  key={entry.site}
                  kicker="站点"
                  title={siteName(entry.site)}
                  description="最新入库在前"
                  count={entry.count}
                  items={items}
                  actionLabel={`全部 ${Number(entry.count || 0).toLocaleString()} 部`}
                  onAction={() => gotoSite(entry.site)}
                  renderItem={(video) => renderCard(video, { showSite: false })}
                />
              );
            })}
            {loadingNote && catalog.videos.length > 0 && (
              <p className="home-status">{loadingNote}，其余站点稍后显示</p>
            )}
          </div>
        )}

        {isGrid && (
          <>
            <FilterBar
              metadata={gridView === "popular" ? popularMetadata : listMetadata || catalog.metadata}
              site={site}
              tag={tag}
              tags={tagStats?.get(site) || []}
              tagsReady={Boolean(tagStats)}
              showTags={gridView === "library"}
              sort={sort}
              sortOptions={sortOptions}
              recentCount={gridView === "library" ? catalog.newCount : 0}
              recentOnly={recentOnly}
              hasFilters={hasFilters}
              onSite={(next) => navigate({ view: gridView, site: next, tag: "" }, { push: true })}
              onTag={(next) => navigate({ view: gridView, tag: next }, { push: true })}
              onSort={(next) => navigate({ sort: next }, { push: true })}
              onRecentToggle={gridView === "library" ? () => navigate({ view: gridView, recentOnly: !recentOnly }, { push: true }) : undefined}
              onClear={clearFilters}
            />
            {gridView === "popular" && (
              <PopularFilters
                filters={nav}
                tags={popularity.tags}
                loading={popularity.loading}
                onChange={(patch, push = true) => navigate(patch, { push })}
                onTopic={toggleTopic}
              />
            )}

            <div className="content-heading library-heading">
              <div>
                <span className="heading-kicker">{heading.kicker}</span>
                <h1>{heading.title}</h1>
                <p>
                  {gridView === "popular" && popularity.loading ? "正在查询" : (gridView === "popular" ? popularity.matched : filtered.length).toLocaleString() + " 项"} · {heading.copy}
                  {gridView === "library" && loadingNote ? ` · ${loadingNote}` : ""}
                  {gridView !== "popular" && catalog.failedParts ? ` · ${catalog.failedParts} 个分片加载失败` : ""}
                </p>
              </div>
              {gridView === "popular" && (
                <button className="button is-quiet" type="button" aria-label="刷新榜单" disabled={popularity.loading} onClick={() => popularPage > 1 ? changePopularPage(1, false) : popularity.refresh()}>
                  <RefreshCw size={14} className={popularity.loading ? "spin" : ""} aria-hidden="true" /><span>刷新榜单</span>
                </button>
              )}
              {gridView === "history" && history.length > 0 && (
                <div className="heading-actions">
                  <button className="button is-quiet" type="button" onClick={clearHistory}><Trash2 size={14} aria-hidden="true" /><span>清空历史</span></button>
                </div>
              )}
            </div>

            {gridView === "popular" && (
              <p className="popular-ranking-note">
                {sort === "default" ? (site ? "当前按该站已收录影片的播放量排序。" : "各站按收录榜名次交错展示，减少单一站点霸榜。") : sort === "views" ? "当前按源站累计播放量排序，不同站点的统计口径可能不同。" : "按你选择的方式排列，卡片保留该站收录榜名次。"}
                热度供选片参考，不代表评分；可点击卡片标签继续筛选。
              </p>
            )}
            {gridView === "popular" && nav.cloudOnly && cache.error && <div className="inline-warning">云盘状态获取失败，结果可能不完整：{cache.error}</div>}
            {gridView === "popular" && popularity.error && <div className="inline-error">{popularity.error}</div>}
            {filtered.length ? (
              <div className="video-grid">
                {filtered.slice(0, limit).map((video) => renderCard(video))}
              </div>
            ) : (
              <EmptyState {...emptyState} />
            )}
            {gridView === "popular" && !popularity.loading && !popularity.error && popularity.matched > 0 && (
              <nav className="popular-pagination" aria-label="热门榜分页">
                <button className="button" type="button" disabled={popularPage <= 1} onClick={() => changePopularPage(popularPage - 1)}>上一页</button>
                <span>第 {popularPage.toLocaleString()} / {popularity.pages.toLocaleString()} 页 · 每页 48 部</span>
                <button className="button" type="button" disabled={popularPage >= popularity.pages} onClick={() => changePopularPage(popularPage + 1)}>下一页</button>
                <form onSubmit={(event) => { event.preventDefault(); const value = Number(new FormData(event.currentTarget).get("page")); if (Number.isInteger(value) && value >= 1 && value <= popularity.pages) changePopularPage(value); }}>
                  <input key={`${popularPage}-${popularity.pages}`} type="number" name="page" min="1" max={popularity.pages} defaultValue={popularPage} aria-label="跳转页码" />
                  <button className="button is-quiet" type="submit">跳转</button>
                </form>
              </nav>
            )}
            <div className="load-sentinel" ref={sentinelRef}>
              {limit < filtered.length && <span>下滑继续载入 · {Math.min(limit, filtered.length).toLocaleString()} / {filtered.length.toLocaleString()}</span>}
            </div>
          </>
        )}
      </main>

      {showTop && (
        <button className="back-to-top" type="button" aria-label="回到顶部" title="回到顶部" onClick={() => window.scrollTo({ top: 0, behavior: "smooth" })}>
          <ArrowUp size={18} />
        </button>
      )}
    </>
  );
}
