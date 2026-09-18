import { Clock3, HardDrive, Play, RefreshCw, RotateCcw, Trash2, XCircle } from "lucide-react";
import { ACTIVE_STATUSES } from "../hooks/useCache";
import { formatDays, formatRemaining, siteName, watchURL } from "../lib/format";
import { EmptyState } from "./EmptyState";
import { ProgressBar } from "./ProgressBar";
import { KeywordCachePanel } from "./KeywordCachePanel";

function jobLabel(job) {
  if (job.status === "cancelling") return "正在终止";
  if (job.status === "queued") return job.queue_position ? `排队第 ${job.queue_position} 位` : "排队中";
  if (job.status === "running") return job.step || "处理中";
  if (job.status === "failed" || job.status === "stale") return "失败";
  if (job.status === "cancelled") return "已终止";
  return "完成";
}

export function CacheView({ cache, catalog, busyKey, onStartBatch, onRefresh, onCancel, onRemove, onRetry, onClear, onClearAll, onBrowse }) {
  const activeJobs = cache.jobs
    .filter((job) => ACTIVE_STATUSES.has(job.status))
    .sort((a, b) => Number(a.started_at || 0) - Number(b.started_at || 0));
  const recentJobs = cache.jobs
    .filter((job) => ["failed", "stale", "cancelled"].includes(job.status))
    .sort((a, b) => Number(b.updated_at || 0) - Number(a.updated_at || 0));
  const jobs = [...activeJobs, ...recentJobs];
  const records = cache.records.slice().sort((a, b) => Number(b.cached_at || 0) - Number(a.cached_at || 0));

  return (
    <section className="cache-view">
      <div className="content-heading">
        <div>
          <span className="heading-kicker">缓存管理</span>
          <h1>云盘下载</h1>
          <p>
            {activeJobs.length ? `${activeJobs.length} 个任务进行中 · ` : ""}
            {records.length} 部已保存 · {cache.ttl > 0 ? `保存 ${formatDays(cache.ttl)}后自动清理` : "长期保留，仅手动删除"}
          </p>
        </div>
        <div className="heading-actions">
          <button className="icon-button" type="button" title="刷新缓存状态" aria-label="刷新缓存状态" onClick={onRefresh} disabled={cache.loading}>
            <RefreshCw className={cache.loading ? "spin" : ""} size={17} />
          </button>
          <button className="button is-danger" type="button" onClick={onClearAll} disabled={!records.length || Boolean(busyKey)}>
            <Trash2 size={16} /> <span>清除全部</span>
          </button>
        </div>
      </div>


      {cache.error && <div className="inline-error">{cache.error}</div>}
      <KeywordCachePanel catalog={catalog} cache={cache} busy={Boolean(busyKey)} onStartBatch={onStartBatch} />
      <div className="cache-dashboard">
        <section className="cache-column">
          <div className="section-label"><h2>任务队列</h2><span>{activeJobs.length ? `${activeJobs.length} 个进行中` : "当前空闲"}</span></div>
          <div className="status-list">
            {!jobs.length ? (
              <EmptyState title="暂无缓存任务" copy="提交的缓存任务会显示在这里" action={onBrowse ? "去片库挑选" : undefined} onAction={onBrowse} />
            ) : jobs.map((job) => {
              const key = `${job.site}/${job.code}`;
              const active = ACTIVE_STATUSES.has(job.status);
              const failed = ["failed", "stale", "cancelled"].includes(job.status);
              return (
                <article className="status-row" key={key}>
                  <div className="status-copy">
                    <div className="status-title"><a href={watchURL(job.site, job.code)}>{job.title || job.code}</a></div>
                    <div className="status-meta">
                      <span>{siteName(job.site)}</span><span>{job.code}</span>
                      {job.error && <span className="error-text">{job.error}</span>}
                    </div>
                    <ProgressBar
                      progress={job.progress}
                      eta={job.eta_seconds}
                      status={job.status}
                      step={job.step}
                      phase={job.phase}
                      stalled={job.stalled}
                      retryInSeconds={job.retry_in_seconds}
                      queuePosition={job.queue_position}
                      downloadedBytes={job.downloaded_bytes}
                      totalBytes={job.total_bytes || job.source_bytes}
                      speedBps={job.speed_bps}
                      compact
                    />
                  </div>
                  <div className="row-actions">
                    <span className={`status-value${active ? " is-running" : failed ? " is-failed" : ""}`}>{jobLabel(job)}</span>
                    {failed && (
                      <button className="row-button is-danger" type="button" onClick={() => onRemove(job)} disabled={Boolean(busyKey)} title="移除任务记录" aria-label="移除任务记录">
                        <Trash2 size={15} /> <span>移除</span>
                      </button>
                    )}
                    {failed && (
                      <button className="row-button" type="button" onClick={() => onRetry(job)} disabled={Boolean(busyKey)} title="重新提交">
                        <RotateCcw size={15} /> <span>重试</span>
                      </button>
                    )}
                    {active && (
                      <button className="row-button is-danger" type="button" onClick={() => onCancel(job)} disabled={job.status === "cancelling" || Boolean(busyKey)} title="终止任务" aria-label="终止任务">
                        <XCircle size={15} /> <span>终止</span>
                      </button>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        </section>

        <section className="cache-column">
          <div className="section-label"><h2>已缓存影片</h2><span>{records.length} 部</span></div>
          <div className="status-list">
            {!records.length ? <EmptyState title="云盘暂无影片" /> : records.map((record) => {
              const key = `${record.site}/${record.code}`;
              const permanent = cache.ttl <= 0 || record.expires_at == null;
              const remaining = Number(record.expires_at || 0) - Number(cache.now || Date.now() / 1000);
              return (
                <article className="status-row" key={key}>
                  <div className="status-icon"><HardDrive size={18} /></div>
                  <div className="status-copy">
                    <div className="status-title"><a href={watchURL(record.site, record.code)}>{record.title || record.code}</a></div>
                    <div className="status-meta">
                      <span>{siteName(record.site)}</span>
                      <span><Clock3 size={13} /> {permanent ? "长期保留" : `${formatRemaining(remaining)}后清理`}</span>
                    </div>
                  </div>
                  <div className="row-actions">
                    <a className="row-button" href={watchURL(record.site, record.code)}><Play size={15} /> <span>播放</span></a>
                    <button className="row-button is-danger" type="button" onClick={() => onClear(record)} disabled={Boolean(busyKey)} title="删除缓存">
                      <Trash2 size={15} /> <span>删除</span>
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        </section>
      </div>
    </section>
  );
}
