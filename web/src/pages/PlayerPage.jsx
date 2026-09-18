import { useFavoriteCache } from "../hooks/useFavoriteCache";
import {
  AlertTriangle, Check, Download, Heart, LoaderCircle, PlayCircle,
  Radio, RotateCcw, XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { AppHeader } from "../components/AppHeader";
import { ProgressBar } from "../components/ProgressBar";
import { VideoCard } from "../components/VideoCard";
import { VideoRow } from "../components/VideoRow";
import { QUALITY_LABELS, usePlayer } from "../hooks/usePlayer";
import { useRecommendations } from "../hooks/useRecommendations";
import { useStoredList } from "../hooks/useStoredList";
import { formatDuration, siteName, thumbnailURL, videoKey } from "../lib/format";
import { readStoredProgress, recordStoredHistory, updateStoredProgress } from "../lib/storage";

const ACTIVE = new Set(["queued", "running", "cancelling"]);
const RELATED_LIMIT = 18;

function cacheLabel(item, cache, submitting) {
  if (submitting) return "正在提交";
  const job = cache?.job;
  if (cache?.ready) return "已保存到云盘";
  if (job?.status === "queued") return "排队等待缓存";
  if (job && ACTIVE.has(job.status)) return "正在缓存";
  if (["failed", "stale", "cancelled"].includes(job?.status)) return "重新缓存";
  if (item?.local || item?.has_local) return "保存原片到云盘";
  return "下载到云盘";
}

function localLabel(item, cache) {
  if (cache?.has_raw || item?.has_raw) return "原片已保存";
  if (cache?.ready) return "已缓存（流畅版 + 原画）";
  if (item?.local && item?.has_low) return "已缓存（流畅版 + 原画）";
  if (item?.local || item?.has_local) return "已缓存（仅原画）";
  if (cache?.job && ACTIVE.has(cache.job.status)) return "缓存中";
  return "未缓存";
}

export function PlayerPage({ user, notify }) {
  const params = useMemo(() => new URLSearchParams(window.location.search), []);
  const site = params.get("site") || "";
  const code = params.get("code") || "";
  const saved = useMemo(() => readStoredProgress(user, { site, code }), [code, site, user]);
  const resumeAt = saved.position > 15 && (!saved.total || saved.position < saved.total - 20) ? saved.position : 0;
  const player = usePlayer(site, code, { resumeFrom: () => resumeAt });
  const [favorites] = useStoredList("favorites", user);
  const related = useRecommendations(site);
  const historyRecorded = useRef(false);
  const resumeNotified = useRef(false);
  const item = player.item;
  const itemRef = useRef(item);
  itemRef.current = item;
  const key = item ? videoKey(item) : `${site}/${code}`;
  const favorite = favorites.some((entry) => videoKey(entry) === key);
  const favoriteKeys = useMemo(() => new Set(favorites.map(videoKey)), [favorites]);
  const job = player.cache?.job;
  const active = Boolean(job && ACTIVE.has(job.status));
  const failed = Boolean(job && ["failed", "stale", "cancelled"].includes(job.status));
  const showProgress = active || failed || player.submitting;
  const relatedVideos = useMemo(
    () => related.videos.filter((video) => videoKey(video) !== key).slice(0, RELATED_LIMIT),
    [key, related.videos],
  );

  useEffect(() => {
    historyRecorded.current = false;
    resumeNotified.current = false;
  }, [key]);
  useEffect(() => {
    document.title = item?.title ? `${item.title} · AVWEB` : "播放 · AVWEB";
  }, [item?.title]);

  // Persist playback position so the library can offer "continue watching".
  const hasItem = Boolean(item);
  useEffect(() => {
    const video = player.videoRef.current;
    if (!video || !hasItem) return undefined;
    let lastSave = 0;
    const save = (force = false) => {
      const current = itemRef.current;
      if (!current || !historyRecorded.current) return;
      const now = Date.now();
      if (!force && now - lastSave < 5000) return;
      const position = Number(video.currentTime) || 0;
      if (position < 3) return;
      lastSave = now;
      updateStoredProgress(user, current, position, video.duration);
    };
    const onTime = () => save(false);
    const onPause = () => save(true);
    const onEnded = () => {
      if (itemRef.current && historyRecorded.current) updateStoredProgress(user, itemRef.current, video.duration, video.duration);
    };
    const onHide = () => { if (document.visibilityState === "hidden") save(true); };
    video.addEventListener("timeupdate", onTime);
    video.addEventListener("pause", onPause);
    video.addEventListener("ended", onEnded);
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("pagehide", onPause);
    return () => {
      save(true);
      video.removeEventListener("timeupdate", onTime);
      video.removeEventListener("pause", onPause);
      video.removeEventListener("ended", onEnded);
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("pagehide", onPause);
    };
  }, [hasItem, key, player.videoRef, user]);

  useEffect(() => {
    if (!resumeAt || resumeNotified.current || !player.quality || !player.overlay.hidden) return;
    resumeNotified.current = true;
    notify(`已定位到上次看到的 ${formatDuration(resumeAt)}，拖动进度条可从头看`);
  }, [notify, player.overlay.hidden, player.quality, resumeAt]);

  const toggleFavorite = useFavoriteCache(user, notify, (target) =>
    videoKey(target) === key ? player.refreshCache() : Promise.resolve());
  const recordHistory = () => {
    if (!item || historyRecorded.current) return;
    historyRecorded.current = true;
    recordStoredHistory(user, item);
  };
  const startCache = async () => {
    try {
      const result = await player.startCache();
      notify(result?.msg || "已加入缓存队列", "success");
    } catch (caught) {
      notify(caught.message || "缓存任务提交失败", "error");
    }
  };
  const cancelCache = async () => {
    if (!window.confirm("终止这个缓存任务？")) return;
    try {
      const result = await player.cancelCache();
      notify(result?.msg || "终止请求已提交");
    } catch (caught) {
      notify(caught.message || "终止任务失败", "error");
    }
  };

  const metadata = [formatDuration(item?.duration), item?.date, item?.heat].filter(Boolean);
  const progress = player.submitting ? 0 : job?.progress || (player.cache?.ready ? 100 : 0);
  const downloaded = Number(job?.downloaded_bytes || 0);
  const total = Number(job?.total_bytes || job?.source_bytes || 0);
  const cacheDone = Boolean(player.cache?.ready);
  const lines = [
    { id: "remote", label: QUALITY_LABELS.remote, hint: "从源站实时拉取，经服务器中转", available: player.capabilities.remote, hidden: !player.capabilities.remote },
    { id: "cloud", label: QUALITY_LABELS.cloud, hint: "播放云盘原片", available: player.capabilities.cloud },
    { id: "low", label: QUALITY_LABELS.low, hint: player.capabilities.low ? "云盘上的流畅转码版本" : "下载到云盘后可用", available: player.capabilities.low },
    { id: "high", label: QUALITY_LABELS.high, hint: player.capabilities.high ? "云盘上的原画版本" : "下载到云盘后可用", available: player.capabilities.high },
  ].filter((line) => !line.hidden && (!["low", "high"].includes(line.id) || line.available));

  return (
    <>
      <AppHeader user={user} player />
      <main className="app-shell player-shell">
        <nav className="player-breadcrumb" aria-label="当前位置">
          <a href="/">首页</a>
          <span>/</span>
          <a href={`/?view=library&site=${encodeURIComponent(site)}`}>{siteName(site)}</a>
          <span>/</span><span>{item?.title || code || "播放"}</span>
        </nav>

        {player.error && !item ? (
          <div className="fatal-state">
            <AlertTriangle size={30} />
            <h1>无法打开影片</h1>
            <p>{player.error}</p>
            <a className="button" href="/">返回首页</a>
          </div>
        ) : (
          <>
            <div className="player-layout">
              <section className="player-stage" aria-label="视频播放">
                <div className="video-frame">
                  <video
                    ref={player.videoRef}
                    controls
                    playsInline
                    preload="metadata"
                    poster={item ? thumbnailURL(item.site, item.code) : undefined}
                    onPlaying={recordHistory}
                  />
                  {!player.overlay.hidden && (
                    <div className={`player-overlay${player.overlay.error ? " is-error" : ""}`}>
                      {player.overlay.loading ? <LoaderCircle className="spin" size={28} /> : player.overlay.error ? <AlertTriangle size={28} /> : <PlayCircle size={28} />}
                      <span>{player.overlay.message}</span>
                    </div>
                  )}
                </div>
                <div className="player-toolbar">
                  <span className={`stream-status is-${player.streamStatus.type || "idle"}`}>
                    <Radio size={15} aria-hidden="true" />
                    <span>{player.streamStatus.text}</span>
                  </span>
                  <div className="line-picker">
                    <span className="line-label">播放线路</span>
                    <div className="quality-control" role="group" aria-label="播放线路">
                      {lines.map((line) => (
                        <button
                          type="button"
                          key={line.id}
                          className={player.quality === line.id ? "is-active" : ""}
                          title={line.hint}
                          aria-pressed={player.quality === line.id}
                          disabled={!line.available}
                          onClick={() => player.loadQuality(line.id)}
                        >
                          {line.label}
                        </button>
                      ))}
                    </div>
                  </div>
                </div>
              </section>

              <aside className="video-details">
                <span className="detail-kicker">{siteName(item?.site || site)}</span>
                <h1 className="video-title">{item?.title || (player.loading ? "正在读取影片" : key)}</h1>
                <div className="video-meta">
                  {metadata.map((value) => <span key={value}>{value}</span>)}
                </div>
                {!!item?.tags?.length && (
                  <div className="detail-tags">
                    {item.tags.map((tag) => (
                      <a key={tag} href={`/?view=library&site=${encodeURIComponent(item.site || site)}&tag=${encodeURIComponent(tag)}`}>{tag}</a>
                    ))}
                  </div>
                )}

                <div className="detail-actions">
                  <button className={`button action-button${favorite ? " is-favorite" : ""}`} type="button" onClick={() => toggleFavorite(item)} disabled={!item} aria-pressed={favorite}>
                    <Heart size={17} fill={favorite ? "currentColor" : "none"} />
                    {favorite ? "已收藏" : "收藏"}
                  </button>
                  <button className={`button action-button${cacheDone ? " is-done" : " is-primary"}`} type="button" onClick={startCache} disabled={!item || active || cacheDone || player.submitting}>
                    {cacheDone ? <Check size={17} /> : failed ? <RotateCcw size={17} /> : <Download size={17} />}
                    {cacheLabel(item, player.cache, player.submitting)}
                  </button>
                </div>

                {showProgress && (
                  <section className={`cache-progress${failed ? " is-failed" : ""}`} aria-live="polite">
                    <div className="cache-progress-head">
                      <div>
                        <span className="section-eyebrow">缓存任务</span>
                        <h2>{player.submitting ? "正在提交" : job?.status === "queued" ? (job.queue_position ? `排队第 ${job.queue_position} 位` : "排队中") : job?.step || (failed ? "任务未完成" : "处理中")}</h2>
                      </div>
                      {active && (
                        <button className="row-button is-danger" type="button" onClick={cancelCache} disabled={job?.status === "cancelling" || player.submitting}>
                          <XCircle size={15} /> 终止
                        </button>
                      )}
                    </div>
                    <ProgressBar
                      progress={progress}
                      eta={job?.eta_seconds}
                      status={player.submitting ? "running" : job?.status}
                      step={job?.step}
                      phase={job?.phase}
                      stalled={job?.stalled}
                      retryInSeconds={job?.retry_in_seconds}
                      queuePosition={job?.queue_position}
                      downloadedBytes={downloaded}
                      totalBytes={total}
                      speedBps={job?.speed_bps}
                    />
                    {(job?.error || failed) && <p className="error-text">{job?.error || "任务已终止，可重新提交"}</p>}
                  </section>
                )}

                <dl className="detail-specs">
                  <dt>来源</dt><dd>{siteName(item?.site || site)}</dd>
                  <dt>编号</dt><dd>{code || "-"}</dd>
                  <dt>云盘存储</dt><dd>{localLabel(item, player.cache)}</dd>
                  <dt>在线播放</dt><dd>{player.capabilities.remote ? "可用（源站中转）" : "不可用"}</dd>
                  {saved.position > 0 && <><dt>上次看到</dt><dd>{formatDuration(saved.position)}</dd></>}
                </dl>
              </aside>
            </div>

            {relatedVideos.length > 0 && (
              <div className="player-related">
                <VideoRow
                  kicker="推荐"
                  title={`${siteName(site)} 热门`}
                  description="同一站点播放量最高的影片"
                  actionHref={`/?view=popular&site=${encodeURIComponent(site)}`}
                  actionLabel="完整榜单"
                  items={relatedVideos}
                  renderItem={(video) => (
                    <VideoCard
                      video={video}
                      favorite={favoriteKeys.has(videoKey(video))}
                      showSite={false}
                      onFavorite={toggleFavorite}
                    />
                  )}
                />
              </div>
            )}
          </>
        )}
      </main>
    </>
  );
}
