import { formatBytes, formatRemaining } from "../lib/format";

function captionFor({ status, step, phase, stalled, retryInSeconds, progress, eta, queuePosition, downloadedBytes, totalBytes }) {
  if (status === "queued") {
    if (phase === "retry_wait") return `等待重试${retryInSeconds > 0 ? ` · ${formatRemaining(retryInSeconds)}` : ""}`;
    return queuePosition > 1 ? `前面还有 ${queuePosition - 1} 个排队任务` : "等待空闲下载位";
  }
  if (status === "cancelling") return "正在终止";
  if (status === "cancelled") return "已终止";
  if (status === "failed" || status === "stale") return "失败，可重试";
  if (step && (step.includes("确认") || step.includes("校验"))) return "等待云盘确认上传";
  if (status === "done" || progress >= 100) return "已完成";
  if (phase === "resolving") return step || "连接源站";
  if (stalled) return "等待源站或云盘响应";
  const remaining = Number(eta);
  if (Number.isFinite(remaining) && remaining > 0) return `约 ${formatRemaining(remaining)}`;
  if (downloadedBytes > 0 && totalBytes <= 0) return "下载中（总大小未知）";
  return "正在计算速度";
}

export function ProgressBar({
  progress = 0,
  eta,
  status = "",
  step = "",
  phase = "",
  stalled = false,
  retryInSeconds = 0,
  queuePosition,
  downloadedBytes = 0,
  totalBytes = 0,
  speedBps = 0,
  compact = false,
}) {
  const value = Math.max(0, Math.min(100, Number(progress) || 0));
  const downloaded = Math.max(0, Number(downloadedBytes) || 0);
  const total = Math.max(0, Number(totalBytes) || 0);
  const speed = status === "running" && phase !== "finalizing" ? Math.max(0, Number(speedBps) || 0) : 0;
  const indeterminate = status === "running" && !total && value <= 0;
  const details = downloaded > 0
    ? `${formatBytes(downloaded)}${total > 0 ? ` / ${formatBytes(total)}` : ""}${speed > 0 ? ` · ${formatBytes(speed)}/s` : ""}`
    : "";
  return (
    <div className={`job-progress${compact ? " is-compact" : ""}${indeterminate ? " is-indeterminate" : ""}`}>
      <div
        className="progress-track"
        role="progressbar"
        aria-label="缓存进度"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow={indeterminate ? undefined : Math.round(value)}
      >
        <span className="progress-fill" style={{ width: indeterminate ? "35%" : `${value}%` }} />
      </div>
      <div className="progress-caption">
        <span>{indeterminate ? "下载中" : `${Math.round(value)}%`}</span>
        <span>{captionFor({ status, step, phase, stalled, retryInSeconds, progress: value, eta, queuePosition, downloadedBytes: downloaded, totalBytes: total })}</span>
      </div>
      {details && <div className="progress-details">{details}</div>}
    </div>
  );
}
