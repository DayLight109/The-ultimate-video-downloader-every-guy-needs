import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { postJSON, requestJSON } from "../lib/api";
import { REMOTE_SITES } from "../lib/format";

const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);
const RATE_KEY = "avweb:player:rate";
export const PLAYBACK_RATES = [0.5, 0.75, 1, 1.25, 1.5, 2];

function readRate() {
  const value = Number(localStorage.getItem(RATE_KEY));
  return PLAYBACK_RATES.includes(value) ? value : 1;
}

// hls.js is ~500 KB; load it only when an HLS source is actually played.
let hlsModule = null;
function loadHls() {
  if (!hlsModule) hlsModule = import("hls.js").then((module) => module.default || module);
  return hlsModule;
}

export const QUALITY_LABELS = {
  remote: "在线（源站）",
  cloud: "云盘 · 原片",
  low: "云盘 · 流畅",
  high: "云盘 · 原画",
};

function mediaURL(site, code, quality) {
  if (quality === "remote") {
    return `/api/stream?site=${encodeURIComponent(site)}&code=${encodeURIComponent(code)}`;
  }
  if (quality === "cloud") {
    return `/api/stream/cloud?site=${encodeURIComponent(site)}&code=${encodeURIComponent(code)}`;
  }
  const mediaCode = quality === "low" ? `${code}_low` : code;
  return `/library/hls/${encodeURIComponent(site)}/${encodeURIComponent(mediaCode)}/index.m3u8`;
}

// options.resumeFrom: () => seconds to seek to when the first line loads.
export function usePlayer(site, code, options = {}) {
  const videoRef = useRef(null);
  const hlsRef = useRef(null);
  const playbackToken = useRef(0);
  const attempts = useRef(new Set());
  const attemptRef = useRef(null);
  const mediaCleanup = useRef(() => {});
  const metadataRefreshed = useRef(false);
  const resumeFrom = useRef(options.resumeFrom);
  resumeFrom.current = options.resumeFrom;

  const [item, setItem] = useState(null);
  const [cache, setCache] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [quality, setQuality] = useState("");
  const [overlay, setOverlay] = useState({ message: "正在读取影片...", loading: true, error: false });
  const [streamStatus, setStreamStatus] = useState({ text: "读取状态", type: "" });
  const [submitting, setSubmitting] = useState(false);
  const [rate, setRateState] = useState(readRate);
  const rateRef = useRef(rate);
  rateRef.current = rate;

  const mergedItem = useMemo(() => {
    if (!item) return null;
    if (!cache) return item;
    return {
      ...item,
      local: typeof cache.local === "boolean" ? cache.local : item.local,
      has_local: typeof cache.local === "boolean" ? cache.local : item.has_local,
      has_low: typeof cache.has_low === "boolean" ? cache.has_low : item.has_low,
      has_raw: Boolean(cache.has_raw),
      has_hls: Boolean(cache.has_hls),
      raw_kind: cache.raw_kind || "",
      ready: typeof cache.ready === "boolean" ? cache.ready : item.ready,
    };
  }, [cache, item]);

  const capabilities = useMemo(() => ({
    cloud: Boolean(mergedItem?.has_raw),
    high: Boolean(mergedItem?.has_hls),
    low: Boolean(mergedItem?.has_low),
    remote: Boolean(mergedItem && (
      mergedItem.remote_playable || mergedItem.downloadable || REMOTE_SITES.has(site)
    )),
  }), [mergedItem, site]);
  const capabilitiesRef = useRef(capabilities);
  capabilitiesRef.current = capabilities;

  const destroyMedia = useCallback(() => {
    playbackToken.current += 1;
    mediaCleanup.current();
    mediaCleanup.current = () => {};
    if (hlsRef.current) {
      hlsRef.current.destroy();
      hlsRef.current = null;
    }
  }, []);

  attemptRef.current = async (nextQuality, context = {}) => {
    const video = videoRef.current;
    const available = capabilitiesRef.current;
    if (!video || !available[nextQuality] || attempts.current.has(nextQuality)) return;
    attempts.current.add(nextQuality);

    const position = Number(context.position ?? video.currentTime) || 0;
    const autoplay = Boolean(context.autoplay);
    destroyMedia();
    const token = playbackToken.current;
    const remote = nextQuality === "remote";
    const label = QUALITY_LABELS[nextQuality];

    video.pause();
    video.removeAttribute("src");
    video.load();
    setQuality(nextQuality);
    setOverlay({ message: remote ? "正在连接源站..." : `正在载入${label}...`, loading: true, error: false });
    setStreamStatus({ text: remote ? "连接源站中" : `载入${label}`, type: remote ? "processing" : "local" });

    const listeners = [];
    let startupTimer;
    const listen = (event, handler, options) => {
      video.addEventListener(event, handler, options);
      listeners.push([event, handler]);
    };
    mediaCleanup.current = () => {
      window.clearTimeout(startupTimer);
      listeners.forEach(([event, handler]) => {
        video.removeEventListener(event, handler);
      });
    };

    const resume = () => {
      if (token !== playbackToken.current) return;
      // load() resets the rate to the default; reapply the viewer's choice.
      video.defaultPlaybackRate = rateRef.current;
      video.playbackRate = rateRef.current;
      if (position > 0 && Number.isFinite(position)) {
        try { video.currentTime = position; } catch { /* Metadata may not be ready yet. */ }
      }
      if (autoplay) video.play().catch(() => {});
    };
    const ready = () => {
      if (token !== playbackToken.current) return;
      window.clearTimeout(startupTimer);
      setOverlay({ message: "", loading: false, error: false, hidden: true });
      setStreamStatus({
        text: remote
          ? (context.cloudFallback ? "云盘暂不可用 · 已切换源站" : "源站在线播放 · 经服务器中转")
          : context.sourceFallback ? `源站不可用 · 已切换${label}` : `${label}播放中`,
        type: remote ? "remote" : "local",
      });
    };
    const failed = (detail) => {
      if (token !== playbackToken.current) return;
      if (nextQuality === "cloud" && !context.cloudRetried) {
        attempts.current.delete("cloud");
        attemptRef.current("cloud", {
          position: Number(video.currentTime) || position, autoplay, cloudRetried: true,
        });
        return;
      }
      // Fall through the next best line: cloud <-> source, low <-> high.
      const alternate = nextQuality === "cloud" ? "remote"
        : nextQuality === "remote" ? "cloud"
          : nextQuality === "low" ? "high" : nextQuality === "high" ? "low" : "";
      if (alternate && capabilitiesRef.current[alternate] && !attempts.current.has(alternate)) {
        attemptRef.current(alternate, {
          position: Number(video.currentTime) || position,
          autoplay,
          cloudFallback: nextQuality === "cloud",
          sourceFallback: nextQuality === "remote",
        });
        return;
      }
      destroyMedia();
      setOverlay({ message: `播放失败：${detail}`, loading: false, error: true });
      setStreamStatus({ text: "播放失败", type: "error" });
    };
    listen("canplay", ready, { once: true });
    if (nextQuality === "cloud") {
      startupTimer = window.setTimeout(() => failed("云盘读取超时，请稍后重试"), 20000);
    }

    const start = async (kind, url) => {
      if (token !== playbackToken.current) return;
      if (kind === "hls") {
        let Hls = null;
        try {
          Hls = await loadHls();
        } catch {
          Hls = null;
        }
        if (token !== playbackToken.current) return;
        if (Hls?.isSupported()) {
          const hls = new Hls({
            startLevel: 0,
            startFragPrefetch: true,
            maxBufferLength: 24,
            maxMaxBufferLength: 60,
            maxBufferSize: 60 * 1000 * 1000,
            abrEwmaDefaultEstimate: 600000,
            manifestLoadingMaxRetry: 2,
            levelLoadingMaxRetry: 2,
            fragLoadingMaxRetry: 2,
            fragLoadingTimeOut: 15000,
          });
          hlsRef.current = hls;
          hls.loadSource(url);
          hls.attachMedia(video);
          listen("loadedmetadata", resume, { once: true });
          hls.on(Hls.Events.ERROR, (_event, data) => {
            if (data?.fatal) failed(data.details || "媒体流不可用");
          });
          return;
        }
      }
      if (kind === "mp4" || video.canPlayType("application/vnd.apple.mpegurl")) {
        listen("loadedmetadata", resume, { once: true });
        listen("error", () => failed(nextQuality === "cloud" ? "云盘读取或媒体解码失败，请稍后重试" : "媒体流不可用"), { once: true });
        video.src = url;
        video.load();
        return;
      }
      failed("当前浏览器不支持 HLS");
    };

    try {
      if (remote) {
        const info = await requestJSON(
          `/api/stream/info?site=${encodeURIComponent(site)}&code=${encodeURIComponent(code)}`,
          { timeout: 30000 },
        );
        await start(info.kind === "hls" ? "hls" : "mp4", info.stream_url || mediaURL(site, code, nextQuality));
      } else if (nextQuality === "cloud") {
        await start("mp4", mediaURL(site, code, nextQuality));
      } else {
        await start("hls", mediaURL(site, code, nextQuality));
      }
    } catch (caught) {
      failed(caught.message || "源站线路不可用");
    }
  };

  const loadQuality = useCallback((nextQuality, options = {}) => {
    const video = videoRef.current;
    const position = options.position ?? Number(video?.currentTime || 0);
    const autoplay = options.autoplay ?? Boolean(quality && video && !video.paused && !video.ended);
    attempts.current.clear();
    attemptRef.current?.(nextQuality, { position, autoplay });
  }, [quality]);

  const refreshVideo = useCallback(async () => {
    const result = await requestJSON(
      `/api/video?site=${encodeURIComponent(site)}&code=${encodeURIComponent(code)}&source_only=1`,
    );
    if (!result?.video) throw new Error("未找到该影片");
    const video = {
      ...result.video,
      site: result.video.site || site,
      code: String(result.video.code || code),
    };
    setItem((current) => current ? { ...current, ...video } : video);
    return video;
  }, [code, site]);

  const refreshCache = useCallback(async () => {
    const result = await requestJSON(
      `/api/cache/check?site=${encodeURIComponent(site)}&code=${encodeURIComponent(code)}`,
      { timeout: 15000 },
    );
    setCache(result);
    if (result.ready && !metadataRefreshed.current) {
      metadataRefreshed.current = true;
      refreshVideo().catch(() => {});
    }
    return result;
  }, [code, refreshVideo, site]);

  const refreshJob = useCallback(async () => {
    const result = await requestJSON(
      `/api/cache/job?site=${encodeURIComponent(site)}&code=${encodeURIComponent(code)}`,
      { timeout: 5000 },
    );
    setCache((current) => ({ ...(current || {}), job: result?.job || null }));
    return result;
  }, [code, site]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    setItem(null);
    setCache(null);
    setQuality("");
    metadataRefreshed.current = false;
    if (!site || !code) {
      setError("播放参数不完整");
      setLoading(false);
      return undefined;
    }
    refreshCache().catch(() => null);
    refreshVideo()
      .catch((caught) => {
        if (!cancelled) setError(caught.message || "影片加载失败");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; destroyMedia(); };
  }, [code, destroyMedia, refreshCache, refreshVideo, site]);

  const active = Boolean(cache?.job && ACTIVE_STATUSES.has(cache.job.status));
  useEffect(() => {
    if (!active) return undefined;
    let stopped = false;
    let timer;
    const poll = async () => {
      const result = await refreshJob().catch(() => null);
      if (stopped) return;
      if (result?.job && !ACTIVE_STATUSES.has(result.job.status)) {
        // Only the transition out of an active state needs a cloud/FUSE check.
        refreshCache().catch(() => {});
        return;
      }
      timer = window.setTimeout(poll, 1000);
    };
    timer = window.setTimeout(poll, 1000);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
    };
  }, [active, refreshCache, refreshJob]);

  useEffect(() => {
    if (!mergedItem || quality || loading) return;
    const position = Math.max(0, Number(resumeFrom.current?.()) || 0);
    if (capabilities.remote) loadQuality("remote", { position, autoplay: false });
    else if (capabilities.cloud) loadQuality("cloud", { position, autoplay: false });
    else if (capabilities.low) loadQuality("low", { position, autoplay: false });
    else if (capabilities.high) loadQuality("high", { position, autoplay: false });
    else {
      setOverlay({ message: "暂无可用播放线路", loading: false, error: false });
      setStreamStatus({ text: "线路不可用", type: "error" });
    }
  }, [capabilities, loadQuality, loading, mergedItem, quality]);

  const startCache = useCallback(async () => {
    if (!mergedItem) return null;
    setSubmitting(true);
    try {
      const result = await postJSON("/api/cache/start", {
        site, code, title: mergedItem.title || "",
      });
      metadataRefreshed.current = false;
      await refreshCache();
      return result;
    } finally {
      setSubmitting(false);
    }
  }, [code, mergedItem, refreshCache, site]);

  const cancelCache = useCallback(async () => {
    setSubmitting(true);
    try {
      const result = await postJSON("/api/cache/cancel", { site, code });
      await refreshCache();
      return result;
    } finally {
      setSubmitting(false);
    }
  }, [code, refreshCache, site]);

  const setRate = useCallback((value) => {
    const next = PLAYBACK_RATES.includes(Number(value)) ? Number(value) : 1;
    setRateState(next);
    try { localStorage.setItem(RATE_KEY, String(next)); } catch { /* Private mode may block storage. */ }
    const video = videoRef.current;
    if (video) {
      video.defaultPlaybackRate = next;
      video.playbackRate = next;
    }
  }, []);

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video || !quality) return;
    if (video.paused || video.ended) video.play().catch(() => {});
    else video.pause();
  }, [quality]);

  const seekBy = useCallback((seconds) => {
    const video = videoRef.current;
    if (!video || !Number.isFinite(video.duration)) return;
    video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + seconds));
  }, []);

  const adjustVolume = useCallback((delta) => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = false;
    video.volume = Math.max(0, Math.min(1, Math.round((video.volume + delta) * 20) / 20));
  }, []);

  const toggleMute = useCallback(() => {
    const video = videoRef.current;
    if (video) video.muted = !video.muted;
  }, []);

  const toggleFullscreen = useCallback(async () => {
    const video = videoRef.current;
    if (!video) return;
    const frame = video.parentElement || video;
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (frame.requestFullscreen) await frame.requestFullscreen();
      else if (video.webkitEnterFullscreen) video.webkitEnterFullscreen(); // iOS Safari
    } catch { /* Fullscreen may be denied outside a user gesture. */ }
  }, []);

  const pictureInPictureSupported = typeof document !== "undefined" && Boolean(document.pictureInPictureEnabled);
  const togglePictureInPicture = useCallback(async () => {
    const video = videoRef.current;
    if (!video || !document.pictureInPictureEnabled) return;
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else await video.requestPictureInPicture();
    } catch { /* Not available before metadata or on this stream. */ }
  }, []);

  return {
    videoRef, item: mergedItem, cache, loading, error, quality, overlay,
    streamStatus, submitting, capabilities, active, rate, pictureInPictureSupported,
    loadQuality, startCache, cancelCache, refreshCache,
    setRate, togglePlay, seekBy, adjustVolume, toggleMute, toggleFullscreen, togglePictureInPicture,
  };
}
