import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { postJSON, requestJSON } from "../lib/api";
import { videoKey } from "../lib/format";

export const ACTIVE_STATUSES = new Set(["queued", "running", "cancelling"]);

export function useCache({ polling = true } = {}) {
  const [data, setData] = useState({ jobs: [], records: [], now: Date.now() / 1000, ttl: 0, queueRemaining: null, batchLimit: 100 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const mounted = useRef(true);
  const refreshVersion = useRef(0);
  const inFlight = useRef(null);
  // Keep array identities stable across polls when the payload has not changed,
  // so memoised cards and derived sets do not recompute every second.
  const previous = useRef({ jobsKey: "", jobs: [], recordsKey: "", records: [] });

  const stable = useCallback((jobs, records) => {
    const jobsKey = JSON.stringify(jobs);
    const recordsKey = JSON.stringify(records);
    const cache = previous.current;
    const nextJobs = jobsKey === cache.jobsKey ? cache.jobs : jobs;
    const nextRecords = recordsKey === cache.recordsKey ? cache.records : records;
    previous.current = { jobsKey, jobs: nextJobs, recordsKey, records: nextRecords };
    return [nextJobs, nextRecords];
  }, []);

  const refresh = useCallback(async function refresh(silent = false, afterMutation = false) {
    if (inFlight.current) {
      if (!afterMutation) return inFlight.current;
      await inFlight.current.catch(() => {});
      return refresh(silent);
    }
    const version = ++refreshVersion.current;
    if (!silent) setLoading(true);
    const pending = (async () => {
    try {
      const result = await requestJSON("/api/cache/status");
      if (mounted.current && version === refreshVersion.current) {
        const [jobs, records] = stable(
          Array.isArray(result.jobs) ? result.jobs : [],
          Array.isArray(result.records) ? result.records : [],
        );
        setData({
          jobs,
          records,
          now: Number(result.now || Date.now() / 1000),
          ttl: Number(result.ttl ?? 0),
          queueRemaining: Number.isInteger(result.queue_remaining) ? Math.max(0, result.queue_remaining) : null,
          batchLimit: Math.max(1, Math.min(100, Number(result.batch_limit) || 100)),
        });
        setError("");
      }
      return result;
    } catch (caught) {
      if (mounted.current && version === refreshVersion.current) setError(caught.message || "缓存状态加载失败");
      throw caught;
    } finally {
      if (mounted.current && version === refreshVersion.current) setLoading(false);
    }
    })();
    inFlight.current = pending;
    try { return await pending; }
    finally { if (inFlight.current === pending) inFlight.current = null; }
  }, [stable]);

  useEffect(() => {
    mounted.current = true;
    refresh(true).catch(() => {});
    return () => { mounted.current = false; };
  }, [refresh]);

  const activeCount = useMemo(
    () => data.jobs.filter((job) => ACTIVE_STATUSES.has(job.status)).length,
    [data.jobs],
  );

  useEffect(() => {
    if (!polling) return undefined;
    let stopped = false;
    let timer;
    const poll = async () => {
      await refresh(true).catch(() => {});
      // Schedule after the request finishes so a slow API call cannot create
      // overlapping polls and stale responses. Active jobs get near-real-time
      // updates; idle pages stay quiet.
      if (!stopped) timer = window.setTimeout(poll, activeCount ? 1000 : 30000);
    };
    timer = window.setTimeout(poll, activeCount ? 1000 : 30000);
    const onFocus = () => refresh(true).catch(() => {});
    window.addEventListener("focus", onFocus);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [activeCount, polling, refresh]);

  const start = useCallback(async (item) => {
    const result = await postJSON("/api/cache/start", {
      site: item.site, code: item.code, title: item.title || "",
    });
    await refresh(true, true).catch(() => {});
    return result;
  }, [refresh]);

  const cancel = useCallback(async (site, code) => {
    const result = await postJSON("/api/cache/cancel", { site, code });
    await refresh(true, true).catch(() => {});
    return result;
  }, [refresh]);

  const removeJob = useCallback(async (site, code) => {
    try {
      return await postJSON("/api/cache/remove-job", { site, code });
    } finally {
      await refresh(true, true).catch(() => {});
    }
  }, [refresh]);

  const startBatch = useCallback(async (items) => {
    try {
      return await postJSON("/api/cache/start-batch", {
        items: items.map(({ site, code, title }) => ({ site, code, title: title || "" })),
      });
    } finally {
      // Also reconcile after a timeout: the server may have accepted tasks.
      await refresh(true, true).catch(() => {});
    }
  }, [refresh]);

  const clear = useCallback(async (site, code) => {
    const result = await postJSON("/api/cache/clear", { site, code });
    await refresh(true, true).catch(() => {});
    return result;
  }, [refresh]);

  const clearAll = useCallback(async () => {
    const result = await postJSON("/api/cache/clear-all", {}, { timeout: 180000 });
    await refresh(true, true).catch(() => {});
    return result;
  }, [refresh]);

  // Records carry timestamps that change without membership changing; derive
  // the set from the key list so sort/filter callers only recompute on real changes.
  const localSignature = useMemo(() => data.records.map(videoKey).sort().join("\n"), [data.records]);
  const localKeys = useMemo(
    () => new Set(localSignature ? localSignature.split("\n") : []),
    [localSignature],
  );
  const jobsByKey = useMemo(
    () => new Map(data.jobs.map((job) => [videoKey(job), job])),
    [data.jobs],
  );

  return {
    ...data, loading, error, activeCount, localKeys, jobsByKey,
    refresh, start, startBatch, cancel, removeJob, clear, clearAll, localSignature,
  };
}
