import { Check, Download, Eye, Heart, LoaderCircle, Play, RotateCcw, Sparkles } from "lucide-react";
import { useState } from "react";
import {
  addedLabel, canCache, displayCode, formatDuration, formatSourceViews,
  siteName, thumbnailURL, videoKey, watchURL,
} from "../lib/format";

const ACTIVE = new Set(["queued", "running", "cancelling"]);
const FAILED = new Set(["failed", "stale", "cancelled"]);

export function VideoCard({
  video, favorite = false, watched = false, local = false, job, progress = 0,
  showSite = true, busy = false, onFavorite, onCache,
  popular = false, selectedTags = [], onTag,
}) {
  const [imageStage, setImageStage] = useState(0);
  const key = videoKey(video);
  const title = video.title || key;
  const href = watchURL(video.site, video.code);
  const localThumb = thumbnailURL(video.site, video.code);
  const remoteThumb = String(video.thumb || "");
  const imageURL = imageStage === 0 ? localThumb : imageStage === 1 ? remoteThumb : "";
  const duration = formatDuration(video.duration);
  const newLabel = addedLabel(video._addedAt);
  const code = displayCode(video.code);
  const active = Boolean(job && ACTIVE.has(job.status));
  const failed = Boolean(job && FAILED.has(job.status));
  const percent = Math.round(Number(job?.progress) || 0);
  const ratio = Math.max(0, Math.min(1, Number(progress) || 0));
  const resumeLabel = ratio > 0.01 && ratio < 0.98 ? `看到 ${Math.round(ratio * 100)}%` : "";
  const cacheable = Boolean(onCache && !local && canCache(video));

  const badge = local
    ? { label: "云盘", className: "is-local" }
    : job?.status === "running"
      ? { label: `${job.step || "缓存中"}${percent > 0 ? ` ${percent}%` : ""}`, className: "is-working" }
      : job?.status === "queued"
        ? { label: job.queue_position ? `排队第 ${job.queue_position} 位` : "排队中", className: "is-working" }
        : job?.status === "cancelling"
          ? { label: "正在终止", className: "is-working" }
          : showSite ? { label: siteName(video.site), className: "" } : null;

  const handleImageError = () => {
    if (imageStage === 0 && remoteThumb && remoteThumb !== localThumb) setImageStage(1);
    else setImageStage(2);
  };

  return (
    <article className={`video-card${popular ? " is-popular" : ""}`}>
      <div className="poster">
        <div className="poster-placeholder" aria-hidden="true">
          <FilmPlaceholder label={siteName(video.site)} />
        </div>
        <a className="poster-link" href={href} aria-label={`播放：${title}`}>
          {imageURL && (
            <img
              src={imageURL}
              alt=""
              loading="lazy"
              decoding="async"
              onError={handleImageError}
            />
          )}
          <span className="poster-play" aria-hidden="true"><Play size={22} fill="currentColor" /></span>
        </a>
        {badge && <span className={`poster-badge ${badge.className}`}>{badge.label}</span>}
        <div className="poster-actions">
          {onFavorite && (
            <button
              className={`poster-action favorite-button${favorite ? " is-active" : ""}`}
              type="button"
              title={favorite ? "取消收藏" : "收藏"}
              aria-label={favorite ? "取消收藏" : "收藏"}
              aria-pressed={favorite}
              onClick={() => onFavorite(video)}
            >
              <Heart size={16} fill={favorite ? "currentColor" : "none"} />
            </button>
          )}
          {cacheable && (
            <button
              className={`poster-action cache-button${active ? " is-working" : ""}`}
              type="button"
              title={active ? "正在保存到云盘" : failed ? "重新下载到云盘" : "下载到云盘"}
              aria-label={active ? "正在保存到云盘" : failed ? "重新下载到云盘" : "下载到云盘"}
              disabled={active || busy}
              onClick={() => onCache(video)}
            >
              {active ? <LoaderCircle className="spin" size={16} /> : failed ? <RotateCcw size={16} /> : <Download size={16} />}
            </button>
          )}
        </div>
        {duration && <span className="duration-badge">{duration}</span>}
        {ratio > 0.01 && (
          <span className="watch-progress" aria-hidden="true"><i style={{ width: `${Math.round(ratio * 100)}%` }} /></span>
        )}
      </div>
      <div className="card-copy">
        {popular && video._siteRank && (
          <div className="card-rank" title="该站已收录热门影片按源站播放量排列的名次，非源站完整榜单">
            <strong>#{video._siteRank}</strong><span>{siteName(video.site)} · 收录榜</span>
          </div>
        )}
        <h2 className="card-title"><a href={href} title={title}>{title}</a></h2>
        <div className="card-meta">
          {code && <span>{code}</span>}
          {video.date ? <span>{video.date}</span> : popular && <span>日期未标注</span>}
          {popular && !duration && <span>时长未标注</span>}
          {Number(video.source_views) > 0 && (
            <span className="play-count" title="源站播放量"><Eye size={12} aria-hidden="true" /> {formatSourceViews(video.source_views)}</span>
          )}
        </div>
        <div className="card-flags">
          {resumeLabel
            ? <span className="resume-mark"><Play size={11} aria-hidden="true" /> {resumeLabel}</span>
            : watched
              ? <span className="watched-mark"><Check size={11} aria-hidden="true" /> 看过</span>
              : newLabel
                ? <span className="new-mark"><Sparkles size={11} aria-hidden="true" /> {newLabel}</span>
                : null}
        </div>
        {!!video.tags?.length && (
          <div className="card-tags">
            {(popular ? [...video.tags].sort((a, b) => Number(selectedTags.includes(b)) - Number(selectedTags.includes(a))).slice(0, 3) : video.tags.slice(0, 2)).map((value) => onTag
              ? <button className={selectedTags.includes(value) ? "is-selected" : ""} type="button" key={value} aria-pressed={selectedTags.includes(value)} title={`筛选标签：${value}`} onClick={() => onTag(value)}>{value}</button>
              : <span key={value}>{value}</span>)}
          </div>
        )}
      </div>
    </article>
  );
}

function FilmPlaceholder({ label }) {
  return <><span>AV</span><small>{String(label).slice(0, 8)}</small></>;
}
