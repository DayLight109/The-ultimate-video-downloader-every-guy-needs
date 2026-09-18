import { parseDuration, searchText, videoKey } from "./format.js";

export const DURATION_OPTIONS = [
  ["", "不限时长"], ["short", "10 分钟以内"], ["medium", "10–30 分钟"],
  ["long", "30 分钟及以上"], ["unknown", "时长未标注"],
];
export const AGE_OPTIONS = [["", "不限日期"], ["30", "近 30 天"], ["90", "近 90 天"], ["365", "近一年"]];
export const POPULAR_DEFAULTS = { topics: [], duration: "", age: "", unwatched: false, cloudOnly: false, exclude: "" };

export function readPopularFilters(params) {
  return {
    topics: [...new Set([...params.getAll("topic"), params.get("tag") || ""].map((s) => s.trim()).filter(Boolean))],
    duration: DURATION_OPTIONS.some(([id]) => id === params.get("duration")) ? params.get("duration") : "",
    age: AGE_OPTIONS.some(([id]) => id === params.get("age")) ? params.get("age") : "",
    unwatched: params.get("unwatched") === "1",
    cloudOnly: params.get("cloud") === "1",
    exclude: params.get("exclude") || "",
  };
}

function dateValue(value) {
  const text = String(value || "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return 0;
  const timestamp = Date.parse(`${text}T00:00:00Z`);
  return Number.isFinite(timestamp) && new Date(timestamp).toISOString().slice(0, 10) === text ? timestamp : 0;
}

export function preparePopularity(payload) {
  if (!payload || !Array.isArray(payload.all) || !payload.sites || typeof payload.sites !== "object" || Array.isArray(payload.sites)) {
    throw new Error("热门榜数据格式错误，请重试");
  }
  // Merge the small global and site lists; never load the full film catalog.
  const unique = new Map();
  const add = (rows, fallbackSite = "") => {
    if (!Array.isArray(rows)) return;
    for (const row of rows) {
      if (!row || typeof row !== "object") continue;
      const site = String(row.site || fallbackSite);
      const code = String(row.code ?? "");
      const views = Number(row.source_views);
      if (!site || !code || !Number.isFinite(views) || views <= 0) continue;
      const video = {
        ...row, site, code, source_views: views,
        tags: [...new Set((Array.isArray(row.tags) ? row.tags : []).filter((tag) => typeof tag === "string" && tag.trim()))],
        _date: dateValue(row.date),
      };
      const duration = parseDuration(video.duration);
      video._duration = Number.isFinite(duration) && duration > 0 ? duration : 0;
      video._search = `${searchText(video)} ${video.title || ""} ${video.code} ${video.tags.join(" ")}`.toLowerCase();
      unique.set(videoKey(video), video);
    }
  };
  add(payload.all);
  Object.entries(payload.sites).forEach(([site, rows]) => add(rows, site));
  const grouped = new Map();
  for (const video of unique.values()) {
    if (!grouped.has(video.site)) grouped.set(video.site, []);
    grouped.get(video.site).push(video);
  }
  const bySite = Object.fromEntries([...grouped.entries()].map(([site, rows]) => {
    rows.sort((a, b) => b.source_views - a.source_views || a.code.localeCompare(b.code));
    rows.forEach((video, index) => { video._siteRank = index + 1; });
    return [site, rows];
  }));
  const all = [...unique.values()].sort((a, b) => a._siteRank - b._siteRank
    || b.source_views - a.source_views || videoKey(a).localeCompare(videoKey(b)));
  all.forEach((video, index) => { video._viewOrder = index; });
  return { all, bySite };
}

export function filterPopular(rows, filters, { localKeys = new Set(), historyKeys = new Set(), now = Date.now() } = {}) {
  const words = String(filters.query || "").toLowerCase().trim().split(/\s+/).filter(Boolean);
  const excluded = String(filters.exclude || "").toLowerCase().split(/[\s,，]+/).filter(Boolean);
  const todayEnd = Math.floor(now / 86400000) * 86400000 + 86400000;
  return rows.filter((video) => {
    if (filters.site && video.site !== filters.site) return false;
    if (filters.topics?.some((tag) => !video.tags.includes(tag))) return false;
    const key = videoKey(video);
    if (filters.unwatched && historyKeys.has(key)) return false;
    if (filters.cloudOnly && !localKeys.has(key)) return false;
    const seconds = video._duration;
    if (filters.duration === "short" && !(seconds > 0 && seconds < 600)) return false;
    if (filters.duration === "medium" && !(seconds >= 600 && seconds < 1800)) return false;
    if (filters.duration === "long" && !(seconds >= 1800)) return false;
    if (filters.duration === "unknown" && seconds > 0) return false;
    if (filters.age && !(video._date > 0 && video._date >= todayEnd - Number(filters.age) * 86400000 && video._date < todayEnd)) return false;
    return words.every((word) => video._search.includes(word)) && !excluded.some((word) => video._search.includes(word));
  });
}

export function sortPopular(rows, sort, localKeys) {
  const order = (a, b) => a._viewOrder - b._viewOrder;
  const compare = sort === "views" ? (a, b) => b.source_views - a.source_views
    : sort === "recent" ? (a, b) => b._date - a._date
      : sort === "duration" ? (a, b) => b._duration - a._duration
        : sort === "local" ? (a, b) => Number(localKeys.has(videoKey(b))) - Number(localKeys.has(videoKey(a)))
          : order;
  return [...rows].sort((a, b) => compare(a, b) || order(a, b));
}

export function popularTags(rows) {
  const counts = new Map();
  rows.forEach((video) => video.tags.forEach((tag) => counts.set(tag, (counts.get(tag) || 0) + 1)));
  return [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}
