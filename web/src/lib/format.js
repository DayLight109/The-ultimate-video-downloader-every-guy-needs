export const SITE_NAMES = { bilibili: "Bilibili", douyin: "抖音" };
export const REMOTE_SITES = new Set(["bilibili", "douyin"]);

export function siteName(site) {
  return SITE_NAMES[site] || site || "未知";
}

export function videoKey(video) {
  return `${video.site}/${video.code}`;
}

export function watchURL(site, code) {
  return `/watch.html?site=${encodeURIComponent(site)}&code=${encodeURIComponent(code)}`;
}

export function thumbnailURL(site, code) {
  const flatCode = String(code || "").replaceAll("/", "_");
  return `/thumbs/${encodeURIComponent(`${site}_${flatCode}`)}.jpg`;
}

export function parseDuration(value) {
  if (!value) return 0;
  if (typeof value === "number") return value;
  const parts = String(value).split(":").map(Number);
  if (parts.some(Number.isNaN)) return 0;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] || 0;
}

export function formatDuration(value) {
  const seconds = Math.max(0, Math.floor(parseDuration(value)));
  if (!seconds) return "";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

export function formatRemaining(seconds) {
  const value = Math.max(0, Math.ceil(Number(seconds) || 0));
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.ceil(value / 60)} 分钟`;
  if (value < 86400) {
    const minutes = Math.ceil(value / 60);
    return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`;
  }
  const hours = Math.ceil(value / 3600);
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`;
}

export function formatDays(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value >= 86400) return `${Math.round(value / 86400)} 天`;
  return formatRemaining(value);
}

export function formatBytes(value) {
  let bytes = Math.max(0, Number(value) || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) {
    bytes /= 1024;
    index += 1;
  }
  return `${bytes >= 100 || index === 0 ? Math.round(bytes) : bytes.toFixed(1)} ${units[index]}`;
}

export function formatSourceViews(value) {
  const count = Math.max(0, Number(value) || 0);
  if (count >= 100_000_000) return `${(count / 100_000_000).toFixed(count >= 1_000_000_000 ? 0 : 1).replace(/\.0$/, "")}亿`;
  if (count >= 10_000) return `${(count / 10_000).toFixed(count >= 100_000 ? 0 : 1).replace(/\.0$/, "")}万`;
  return count.toLocaleString("zh-CN");
}

export function searchText(video) {
  return String(
    video.s || video.search_text ||
    [video.title, video.code, video.site, ...(video.tags || [])].join(" ")
  ).toLowerCase();
}

export function isLocal(video) {
  return video?.local === undefined ? Boolean(video?.has_local) : Boolean(video?.local);
}

// Whether the server can fetch this video from its source (stream or cache it).
export function canCache(video) {
  if (!video) return false;
  return Boolean(video.downloadable || video.video_url || video.remote_playable || REMOTE_SITES.has(video.site));
}

// Long opaque hashes, long numeric ids and path-like codes mean nothing to a
// viewer; hide them. Short studio-style codes such as "012418-590" stay.
export function displayCode(code) {
  const value = String(code || "");
  if (!value || value.startsWith("/") || value.length > 14) return "";
  if (/^[0-9a-f]{9,}$/i.test(value)) return "";
  return value;
}

export function addedLabel(value) {
  const timestamp = Number(value || 0);
  if (!timestamp) return "";
  const date = new Date(timestamp);
  const now = new Date();
  const day = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (day === today) return "今日新增";
  if (day === today - 86400000) return "昨日新增";
  return `${date.getMonth() + 1}月${date.getDate()}日新增`;
}
