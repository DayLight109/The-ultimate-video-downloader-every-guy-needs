import { useEffect, useState } from "react";
import { requestJSON } from "../lib/api";
import { readCatalogCache, writeCatalogCache } from "../lib/catalogCache";
import { searchText, videoKey } from "../lib/format";

const VERSION = "react-2";
const WORKERS = 6;
const COMMIT_EVERY = 8;
const PART_SIZE = 500;
const FULL_REFRESH_AFTER = 24 * 60 * 60 * 1000;
const RECENT_WINDOW = 7 * 24 * 60 * 60 * 1000;
const SYNC_INTERVAL = 3 * 60 * 1000;

function fileFor(entry, index) {
  const parts = Math.max(1, Number(entry.parts || 1));
  return parts > 1
    ? `/data/by_site/${entry.site}.p${index}.json?v=${VERSION}`
    : `/data/by_site/${entry.site}.json?v=${VERSION}`;
}

function runtimeVideo(row, site, order, addedAt = 0) {
  const video = row;
  video.site = row.site || site;
  video.code = String(row.code);
  video.tags = Array.isArray(row.tags) ? row.tags : [];
  video._addedAt = Number(row._addedAt || addedAt || 0);
  Object.defineProperties(video, {
    _order: { value: order, writable: true, configurable: true },
    _search: { value: searchText(video), writable: true, configurable: true },
  });
  return video;
}

function prepareSites(metadata, siteRecords) {
  const records = new Map(siteRecords.map((entry) => [entry.site, entry]));
  const sites = [];
  let order = 0;
  metadata.forEach((entry) => {
    const record = records.get(entry.site);
    if (!record?.videos?.length) return;
    const videos = [];
    record.videos.forEach((row) => {
      if (!row || row.code === null || row.code === undefined) return;
      videos.push(runtimeVideo(row, entry.site, order++));
    });
    sites.push({ site: entry.site, videos });
  });
  return sites;
}

function flattenSites(metadata, sites) {
  const bySite = new Map(sites.map((entry) => [entry.site, entry.videos]));
  const videos = [];
  let order = 0;
  metadata.forEach((entry) => {
    (bySite.get(entry.site) || []).forEach((video) => {
      Object.defineProperty(video, "_order", { value: order++, writable: true, configurable: true });
      videos.push(video);
    });
  });
  return videos;
}

function recentStats(videos) {
  const cutoff = Date.now() - RECENT_WINDOW;
  let count = 0;
  let newestAt = 0;
  videos.forEach((video) => {
    const addedAt = Number(video._addedAt || 0);
    if (addedAt >= cutoff) {
      count += 1;
      newestAt = Math.max(newestAt, addedAt);
    }
  });
  return { newCount: count, newestAt };
}

async function fetchRows(url, controllers) {
  const controller = new AbortController();
  controllers.add(controller);
  try {
    const rows = await requestJSON(url, { signal: controller.signal, timeout: 25000 });
    if (!Array.isArray(rows)) throw new Error("目录分片格式错误");
    return rows;
  } finally {
    controllers.delete(controller);
  }
}

async function fetchAll(metadata, controllers, onBatch, previousAdded = null) {
  const files = metadata.flatMap((entry) => Array.from(
    { length: Math.max(1, Number(entry.parts || 1)) },
    (_, index) => ({ site: entry.site, index, url: fileFor(entry, index) }),
  ));
  const results = new Array(files.length);
  const videos = [];
  const seen = new Set();
  const newKeys = new Set();
  const detectedAt = Date.now();
  let next = 0;
  let committed = 0;
  let sinceRender = 0;
  let failedParts = 0;

  function commit(force = false) {
    while (committed < files.length && results[committed] !== undefined) {
      const rows = results[committed];
      const file = files[committed];
      committed += 1;
      sinceRender += 1;
      if (!rows) continue;
      rows.forEach((row) => {
        if (!row || row.code === null || row.code === undefined) return;
        const key = `${row.site || file.site}/${row.code}`;
        if (seen.has(key)) return;
        seen.add(key);
        const knownAt = previousAdded?.get(key);
        if (previousAdded && knownAt === undefined) newKeys.add(key);
        videos.push(runtimeVideo(
          row,
          file.site,
          videos.length,
          previousAdded && knownAt === undefined ? detectedAt : Number(knownAt || 0),
        ));
      });
    }
    if (onBatch && (force || sinceRender >= COMMIT_EVERY || committed === files.length)) {
      sinceRender = 0;
      onBatch(videos.slice(), committed, files.length, failedParts);
    }
  }

  async function worker() {
    while (next < files.length) {
      const index = next++;
      try {
        results[index] = await fetchRows(files[index].url, controllers);
      } catch (error) {
        failedParts += 1;
        results[index] = null;
        console.warn("目录分片加载失败", files[index].url, error);
      } finally {
        commit();
      }
    }
  }

  await Promise.all(Array.from({ length: Math.min(WORKERS, files.length) }, worker));
  commit(true);
  const grouped = new Map(metadata.map((entry) => [entry.site, []]));
  videos.forEach((video) => grouped.get(video.site)?.push(video));
  return {
    videos,
    sites: metadata.map((entry) => ({ site: entry.site, videos: grouped.get(entry.site) || [] })),
    failedParts,
    totalParts: files.length,
    newItems: newKeys.size,
    changed: true,
  };
}

async function fetchSite(entry, controllers) {
  const parts = Math.max(1, Number(entry.parts || 1));
  const results = new Array(parts);
  let next = 0;
  let failed = 0;
  async function worker() {
    while (next < parts) {
      const index = next++;
      try {
        results[index] = await fetchRows(fileFor(entry, index), controllers);
      } catch (error) {
        failed += 1;
        console.warn("站点目录刷新失败", entry.site, index, error);
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(WORKERS, parts) }, worker));
  if (failed) throw new Error(`${entry.site} 有 ${failed} 个分片更新失败`);
  return results.flat();
}

async function refreshCached(metadata, cachedSites, savedAt, controllers, onProgress) {
  const cachedBySite = new Map(cachedSites.map((entry) => [entry.site, entry.videos]));
  const stale = !savedAt || Date.now() - savedAt >= FULL_REFRESH_AFTER;
  const requiresFull = metadata.some((entry) => {
    const oldCount = cachedBySite.get(entry.site)?.length;
    return oldCount === undefined || Number(entry.count || 0) < oldCount;
  });
  const hasGrowth = metadata.some((entry) => Number(entry.count || 0) > (cachedBySite.get(entry.site)?.length || 0));
  if (!stale && !requiresFull && !hasGrowth) {
    return { sites: cachedSites, failedParts: 0, totalParts: 0, newItems: 0, changed: false };
  }

  const allAdded = new Map();
  cachedSites.forEach((entry) => entry.videos.forEach((video) => allAdded.set(videoKey(video), Number(video._addedAt || 0))));
  if (stale || requiresFull) {
    const full = await fetchAll(metadata, controllers, onProgress, allAdded);
    if (full.failedParts) throw new Error(`${full.failedParts} 个目录分片更新失败`);
    return full;
  }

  const sites = [];
  const newKeys = new Set();
  const detectedAt = Date.now();
  let plannedParts = 0;
  let loadedParts = 0;
  const changes = metadata.map((entry) => {
    const cached = cachedBySite.get(entry.site) || [];
    const delta = Math.max(0, Number(entry.count || 0) - cached.length);
    const parts = delta ? Math.min(Math.max(1, Number(entry.parts || 1)), Math.ceil(delta / PART_SIZE) + 1) : 0;
    plannedParts += parts;
    return { entry, cached, delta, parts };
  });

  for (const { entry, cached, delta, parts } of changes) {
    if (!delta) {
      sites.push({ site: entry.site, videos: cached });
      continue;
    }
    const rows = [];
    for (let index = 0; index < parts; index += WORKERS) {
      const indexes = Array.from({ length: Math.min(WORKERS, parts - index) }, (_, offset) => index + offset);
      const chunks = await Promise.all(indexes.map((part) => fetchRows(fileFor(entry, part), controllers)));
      chunks.forEach((chunk) => rows.push(...chunk));
      loadedParts += indexes.length;
      onProgress?.(null, loadedParts, plannedParts, 0);
    }
    const oldKeys = new Set(cached.map(videoKey));
    const headKeys = new Set();
    const merged = [];
    rows.forEach((row) => {
      if (!row || row.code === null || row.code === undefined) return;
      const key = `${row.site || entry.site}/${row.code}`;
      if (headKeys.has(key)) return;
      headKeys.add(key);
      const known = oldKeys.has(key);
      if (!known) newKeys.add(key);
      merged.push(runtimeVideo(row, entry.site, 0, known ? allAdded.get(key) : detectedAt));
    });
    cached.forEach((video) => {
      if (!headKeys.has(videoKey(video))) merged.push(video);
    });

    // New crawler records are published at the head of each site. The overlap
    // page lets us preserve the cached tail without downloading it again.
    sites.push({ site: entry.site, videos: merged });
  }
  return {
    sites,
    failedParts: 0,
    totalParts: plannedParts,
    newItems: newKeys.size,
    changed: newKeys.size > 0,
  };
}

export function useCatalog({ enabled = true } = {}) {
  const [state, setState] = useState({
    metadata: [], videos: [], complete: false, syncing: false, fromCache: false,
    loadedParts: 0, totalParts: 0, failedParts: 0, newCount: 0,
    sessionNewCount: 0, newestAt: 0, lastUpdated: 0, error: "", cacheError: "",
  });

  useEffect(() => {
    if (!enabled) return undefined;
    let cancelled = false;
    let cacheChecked = false;
    let memoryCache = null;
    let memoryVideos = [];
    let syncTimer = null;
    const controllers = new Set();

    async function load() {
      const metadataRequest = requestJSON(`/data/sites.json?v=${VERSION}`)
        .then((value) => ({ value }), (error) => ({ error }));
      const usingMemory = Boolean(memoryCache);
      let cached = memoryCache;
      if (!cacheChecked) {
        cacheChecked = true;
        try {
          cached = await readCatalogCache();
          memoryCache = cached;
        } catch (error) {
          console.warn("目录缓存读取失败", error);
        }
      }

      if (cached && !usingMemory && !cancelled) {
        const sites = prepareSites(cached.metadata, cached.sites);
        const videos = flattenSites(cached.metadata, sites);
        cached = { ...cached, sites };
        memoryCache = cached;
        memoryVideos = videos;
        setState((current) => ({
          ...current,
          metadata: cached.metadata,
          videos,
          complete: true,
          syncing: true,
          fromCache: true,
          lastUpdated: cached.savedAt,
          ...recentStats(videos),
        }));
      }

      const metadataResult = await metadataRequest;
      const metadata = metadataResult.value;
      if (metadataResult.error || !Array.isArray(metadata)) {
        const error = metadataResult.error || new Error("目录元数据格式错误");
        if (!cancelled) setState((current) => ({
          ...current,
          complete: true,
          syncing: false,
          error: cached ? "已使用本地目录，在线更新检查失败" : error.message || "目录加载失败",
        }));
        return;
      }
      if (cancelled) return;

      try {
        let result;
        if (cached) {
          const prepared = usingMemory || memoryCache === cached
            ? cached.sites
            : prepareSites(cached.metadata, cached.sites);
          result = await refreshCached(
            metadata,
            prepared,
            cached.savedAt,
            controllers,
            (_videos, loadedParts, totalParts, failedParts) => {
              if (!cancelled) setState((current) => ({
                ...current, metadata, syncing: true, loadedParts, totalParts, failedParts,
              }));
            },
          );
          result.videos = !result.changed && usingMemory
            ? memoryVideos
            : flattenSites(metadata, result.sites);
        } else {
          setState((current) => ({
            ...current,
            metadata,
            syncing: true,
            totalParts: metadata.reduce((sum, entry) => sum + Math.max(1, Number(entry.parts || 1)), 0),
          }));
          result = await fetchAll(metadata, controllers, (videos, loadedParts, totalParts, failedParts) => {
            if (!cancelled) setState((current) => ({
              ...current, metadata, videos, loadedParts, totalParts, failedParts,
            }));
          });
        }
        if (cancelled) return;
        const savedAt = result.changed || !cached ? Date.now() : cached.savedAt;
        memoryCache = { metadata, sites: result.sites, savedAt };
        memoryVideos = result.videos;
        setState((current) => ({
          ...current,
          metadata,
          videos: result.videos,
          complete: true,
          syncing: false,
          fromCache: Boolean(cached),
          loadedParts: result.totalParts,
          totalParts: result.totalParts,
          failedParts: result.failedParts,
          sessionNewCount: result.newItems || current.sessionNewCount || 0,
          lastUpdated: savedAt,
          error: "",
          ...recentStats(result.videos),
        }));
        if (!result.failedParts && (result.changed || !cached)) {
          writeCatalogCache(metadata, result.sites).catch((error) => {
            console.warn("目录缓存写入失败", error);
            if (!cancelled) setState((current) => ({ ...current, cacheError: "目录缓存写入失败，下次仍需重新读取" }));
          });
        }
      } catch (error) {
        if (!cancelled) setState((current) => ({
          ...current,
          complete: true,
          syncing: false,
          error: cached ? "增量目录更新失败，当前显示上次缓存" : error.message || "目录加载失败",
        }));
      }
    }

    async function cycle() {
      await load();
      if (!cancelled) syncTimer = window.setTimeout(cycle, SYNC_INTERVAL);
    }

    cycle();
    return () => {
      cancelled = true;
      window.clearTimeout(syncTimer);
      controllers.forEach((controller) => controller.abort());
    };
  }, [enabled]);

  return state;
}
