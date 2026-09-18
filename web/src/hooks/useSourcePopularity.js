import { useCallback, useEffect, useState } from "react";
import { postJSON } from "../lib/api";

const EMPTY = { all: [], sites: [], tags: [], total: 0, matched: 0, pages: 1, page: 1, generation: "", updatedAt: 0, loading: true, error: "" };

export function useSourcePopularity(filters, page, historyKeys, cloudSignature, onPage) {
  const [state, setState] = useState(EMPTY);
  const [version, setVersion] = useState(0);
  const refresh = useCallback(() => setVersion((value) => value + 1), []);
  const signature = JSON.stringify({ ...filters, history: filters.unwatched ? [...historyKeys].slice(0, 300) : [] });
  const cloudVersion = filters.cloudOnly || filters.sort === "local" ? cloudSignature : "";

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    // Clear results as soon as filters/page change so old films are never
    // labelled as matching the new conditions. Debounce typing and abort stale requests.
    setState((current) => ({ ...current, loading: true, error: "" }));
    const timer = window.setTimeout(async () => {
      try {
        const body = { ...JSON.parse(signature), page, generation: page > 1 ? state.generation : "" };
        let payload;
        try {
          payload = await postJSON("/api/popular", body, { timeout: 15000, signal: controller.signal });
        } catch (error) {
          if (error.status !== 409 || cancelled) throw error;
          onPage(1, false);
          return;
        }
        if (cancelled) return;
        if (!payload || !Array.isArray(payload.items) || !Array.isArray(payload.sites) || !Array.isArray(payload.tags)
          || !Number.isInteger(payload.matched) || !Number.isInteger(payload.total)) throw new Error("热门榜数据格式错误，请重试");
        if (payload.page !== page) { onPage(payload.page, false); return; }
        setState({ all: payload.items, sites: payload.sites, tags: payload.tags, total: payload.total,
          matched: payload.matched, page: payload.page, pages: payload.pages, generation: payload.generation,
          updatedAt: Number(payload.updated_at) || 0, loading: false, error: "" });
      } catch (error) {
        if (!cancelled) setState((current) => ({ ...current, loading: false, error: error.message || "热门榜加载失败" }));
      }
    }, 180);
    return () => { cancelled = true; window.clearTimeout(timer); controller.abort(); };
  // generation is the last successful snapshot, not a request trigger.
  }, [signature, page, cloudVersion, version, onPage]);

  return { ...state, refresh };
}
