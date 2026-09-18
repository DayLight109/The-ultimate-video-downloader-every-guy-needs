import { useMemo, useRef, useState } from "react";
import { Download, Search } from "lucide-react";
import { ACTIVE_STATUSES } from "../hooks/useCache";
import { canCache, searchText, siteName, videoKey, watchURL } from "../lib/format";

export function KeywordCachePanel({ catalog, cache, busy, onStartBatch }) {
  const [keyword, setKeyword] = useState("");
  const [site, setSite] = useState("");
  const [quantity, setQuantity] = useState("20");
  const [search, setSearch] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const pending = useRef(false);
  const normalized = keyword.trim().toLowerCase().replace(/\s+/g, " ");
  const dirty = search && (search.keyword !== normalized || search.site !== site);
  const batchLimit = cache.batchLimit;
  const count = Number(quantity);
  const validCount = Number.isInteger(count) && count >= 1 && count <= batchLimit;

  // Scan the catalog only when a search is submitted or catalog data changes.
  // Cache polling below checks a small set of active/cached keys instead.
  const matches = useMemo(() => {
    const items = [];
    const keys = new Set();
    const seen = new Set();
    let unsupported = 0;
    if (!search) return { items, keys, total: 0, unsupported };
    const terms = search.keyword.split(" ");
    for (const video of catalog.videos) {
      if (search.site && video.site !== search.site) continue;
      const text = video._search || searchText(video);
      if (!terms.every((term) => text.includes(term))) continue;
      const key = videoKey(video);
      if (seen.has(key)) continue;
      seen.add(key);
      if (!canCache(video)) { unsupported += 1; continue; }
      keys.add(key);
      items.push(video);
    }
    return { items, keys, total: seen.size, unsupported };
  }, [search, catalog.videos]);

  const selection = useMemo(() => {
    const excluded = new Set();
    let cached = 0;
    let active = 0;
    for (const key of cache.localKeys) {
      if (matches.keys.has(key)) { excluded.add(key); cached += 1; }
    }
    for (const [key, job] of cache.jobsByKey) {
      if (matches.keys.has(key) && !excluded.has(key) && ACTIVE_STATUSES.has(job.status)) {
        excluded.add(key);
        active += 1;
      }
    }
    const available = matches.items.length - excluded.size;
    const limit = validCount ? Math.min(count, cache.queueRemaining ?? 0) : 0;
    const items = [];
    if (limit > 0) {
      for (const video of matches.items) {
        if (!excluded.has(videoKey(video))) items.push(video);
        if (items.length >= limit) break;
      }
    }
    return { items, available, cached, active };
  }, [matches, cache.localKeys, cache.jobsByKey, cache.queueRemaining, count, validCount]);

  const findMatches = (event) => {
    event.preventDefault();
    if (!normalized || submitting) return;
    setSearch({ keyword: normalized, site });
    setResult(null);
    setError("");
  };
  const submit = async () => {
    if (pending.current || busy || dirty || !selection.items.length || cache.error || cache.loading) return;
    pending.current = true;
    setSubmitting(true);
    setResult(null);
    setError("");
    try {
      setResult(await onStartBatch(selection.items));
    } catch (caught) {
      setError(`${caught.message || "提交失败"}。请刷新队列确认后重试，已入队影片会自动跳过。`);
    } finally {
      pending.current = false;
      setSubmitting(false);
    }
  };

  return (
    <section className="keyword-cache" aria-labelledby="keyword-cache-title">
      <h2 id="keyword-cache-title">按关键词批量缓存</h2>
      <p className="keyword-hint">匹配片库中的标题、编号和标签；多个词以空格分隔，需同时匹配。按目录顺序选取，保存到云盘。</p>
      <form className="keyword-form" onSubmit={findMatches}>
        <label className="keyword-field">
          <span>关键词</span>
          <input type="search" value={keyword} maxLength={200} placeholder="输入标题、编号或标签" onChange={(event) => setKeyword(event.target.value)} disabled={submitting} />
        </label>
        <label>
          <span>站点范围</span>
          <select value={site} onChange={(event) => setSite(event.target.value)} disabled={submitting}>
            <option value="">全部站点</option>
            {catalog.metadata.map((entry) => <option key={entry.site} value={entry.site}>{siteName(entry.site)}</option>)}
          </select>
        </label>
        <button className="button" type="submit" disabled={!normalized || submitting || !catalog.videos.length}>
          <Search size={16} /> 查找影片
        </button>
      </form>
      <div className="keyword-controls">
        <label className="keyword-quantity">
          <span>本次缓存数量</span>
          <input type="number" min="1" max={batchLimit} step="1" inputMode="numeric" value={quantity} onChange={(event) => setQuantity(event.target.value)} disabled={submitting} aria-describedby="keyword-quantity-hint" />
        </label>
        <div className="keyword-presets" role="group" aria-label="快捷选择数量">
          {[10, 20, 50, 100].filter((value) => value <= batchLimit).map((value) => (
            <button className={`row-button${count === value ? " is-selected" : ""}`} type="button" aria-pressed={count === value} key={value} onClick={() => setQuantity(String(value))} disabled={submitting}>{value} 部</button>
          ))}
        </div>
        <span id="keyword-quantity-hint" className={validCount ? "keyword-hint" : "error-text"}>
          {validCount ? `每批最多 ${batchLimit} 部` : `请输入 1–${batchLimit} 之间的整数`}
          {cache.queueRemaining !== null && ` · 队列还可加入 ${cache.queueRemaining} 部`}
        </span>
      </div>
      {(!catalog.complete || catalog.syncing || catalog.failedParts > 0 || catalog.error) && (
        <p className="inline-warning">{!catalog.complete || catalog.syncing ? "目录正在加载或更新，当前仅匹配已载入的影片。" : "部分目录未能更新，当前仅匹配已载入的影片。"}</p>
      )}
      {!search ? <p className="keyword-hint">先查找影片，再按所选数量加入缓存队列。</p> : dirty ? (
        <p className="inline-warning">关键词或站点已修改，请重新查找影片。</p>
      ) : (
        <div className="keyword-preview">
          <p className="keyword-summary">匹配 {matches.total.toLocaleString()} 部 · 可缓存 {selection.available.toLocaleString()} 部</p>
          <p className="keyword-hint">已跳过：已缓存 {selection.cached} 部、进行中 {selection.active} 部、不支持下载 {matches.unsupported} 部。</p>
          {!matches.total && <p className="keyword-hint">没有匹配的影片，请换一个关键词或站点。</p>}
          {selection.items.length > 0 && (
            <>
              <p className="keyword-hint">本次将加入 {selection.items.length} 部{selection.items.length > 5 ? "，预览前 5 部" : ""}：</p>
              <ol className="keyword-preview-list">
                {selection.items.slice(0, 5).map((video) => <li key={videoKey(video)}><a href={watchURL(video.site, video.code)} target="_blank" rel="noreferrer">{video.title || video.code}</a><span>{siteName(video.site)}</span></li>)}
              </ol>
            </>
          )}
          {cache.queueRemaining === 0 && <p className="inline-warning">队列已满，等待任务出队后可继续提交。</p>}
          {cache.queueRemaining === null && <p className="keyword-hint">等待队列容量信息，请刷新缓存状态。</p>}
          {validCount && selection.items.length > 0 && selection.items.length < count && <p className="keyword-hint">受可缓存影片数量或队列剩余容量限制，本次实际加入 {selection.items.length} 部。</p>}
          <button className="button is-primary" type="button" onClick={submit} disabled={submitting || busy || !selection.items.length || cache.loading || Boolean(cache.error)}>
            <Download size={16} /> {submitting ? "正在加入队列…" : `加入 ${selection.items.length} 部到缓存队列`}
          </button>
        </div>
      )}
      {result && <div className="keyword-result" role="status">
        <p>{result.msg}</p>
        {result.failed?.length > 0 && <ul>{result.failed.slice(0, 3).map((item) => <li key={videoKey(item)}>{item.title || item.code}：{item.msg}</li>)}</ul>}
        {result.failed?.length > 3 && <p>另有 {result.failed.length - 3} 部未加入，可刷新后重新提交。</p>}
      </div>}
      {error && <div className="inline-error" role="alert">{error}</div>}
    </section>
  );
}
