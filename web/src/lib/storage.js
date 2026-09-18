import { videoKey } from "./format";

const LIST_LIMIT = 300;

function key(type, user) {
  return `avweb:${type}:${user || "guest"}`;
}

export function readStoredList(type, user) {
  try {
    let raw = localStorage.getItem(key(type, user));
    if (raw === null) {
      const prefix = type === "favorites" ? "fav_" : type === "history" ? "hist_" : "";
      const legacy = prefix ? localStorage.getItem(`${prefix}${user}`) : null;
      if (legacy !== null) {
        raw = legacy;
        localStorage.setItem(key(type, user), legacy);
      }
    }
    const value = JSON.parse(raw || "[]");
    return Array.isArray(value) ? value : [];
  } catch {
    return [];
  }
}

export function writeStoredList(type, user, value) {
  localStorage.setItem(key(type, user), JSON.stringify(value));
  window.dispatchEvent(new CustomEvent("avweb-storage", { detail: { type, user } }));
}

export function toggleStoredFavorite(user, item) {
  const list = readStoredList("favorites", user);
  const itemKey = videoKey(item);
  const index = list.findIndex((entry) => videoKey(entry) === itemKey);
  let active = false;
  if (index >= 0) {
    list.splice(index, 1);
  } else {
    list.unshift({
      site: item.site,
      code: item.code,
      title: item.title || itemKey,
      date: item.date || "",
    });
    active = true;
  }
  writeStoredList("favorites", user, list.slice(0, LIST_LIMIT));
  return active;
}

// History entries keep playback progress in seconds: `position` (where the
// viewer stopped) and `total` (media length). `duration` is left to the
// catalog record, which stores it as a "hh:mm:ss" string.
function historyEntry(item, previous = {}) {
  const itemKey = videoKey(item);
  return {
    site: item.site,
    code: item.code,
    title: item.title || previous.title || itemKey,
    date: item.date || previous.date || "",
    position: Math.max(0, Math.floor(Number(previous.position) || 0)),
    total: Math.max(0, Math.floor(Number(previous.total) || 0)),
    watched_at: Number(previous.watched_at) || Date.now(),
  };
}

// Move (or add) the item to the top of the history list; keeps its saved position.
export function recordStoredHistory(user, item) {
  const itemKey = videoKey(item);
  const list = readStoredList("history", user);
  const previous = list.find((entry) => videoKey(entry) === itemKey) || {};
  const rest = list.filter((entry) => videoKey(entry) !== itemKey);
  rest.unshift({ ...historyEntry(item, previous), watched_at: Date.now() });
  writeStoredList("history", user, rest.slice(0, LIST_LIMIT));
}

// Save playback position without reordering the list.
export function updateStoredProgress(user, item, position, total) {
  const itemKey = videoKey(item);
  const list = readStoredList("history", user);
  const index = list.findIndex((entry) => videoKey(entry) === itemKey);
  const entry = historyEntry(item, index >= 0 ? list[index] : {});
  entry.position = Math.max(0, Math.floor(Number(position) || 0));
  entry.total = Math.max(0, Math.floor(Number(total) || entry.total || 0));
  if (index >= 0) list[index] = entry;
  else list.unshift(entry);
  writeStoredList("history", user, list.slice(0, LIST_LIMIT));
}

export function readStoredProgress(user, item) {
  const itemKey = videoKey(item);
  const entry = readStoredList("history", user).find((candidate) => videoKey(candidate) === itemKey);
  return {
    position: Math.max(0, Number(entry?.position) || 0),
    total: Math.max(0, Number(entry?.total) || 0),
  };
}

export function progressRatio(entry) {
  const position = Number(entry?.position) || 0;
  const total = Number(entry?.total) || 0;
  if (!position || !total) return 0;
  return Math.max(0, Math.min(1, position / total));
}
