import { useCallback, useEffect, useState } from "react";
import { requestJSON } from "../lib/api";

export function useRecommendations(site = "") {
  const [state, setState] = useState({ videos: [], loading: true, error: "" });

  const refresh = useCallback(async () => {
    setState((current) => ({ ...current, loading: true }));
    try {
      const query = new URLSearchParams({ limit: "100" });
      if (site) query.set("site", site);
      const result = await requestJSON(`/api/recommendations?${query}`);
      setState({
        videos: Array.isArray(result?.videos) ? result.videos : [],
        loading: false,
        error: "",
      });
    } catch (error) {
      setState({ videos: [], loading: false, error: error.message || "热门推荐加载失败" });
    }
  }, [site]);

  useEffect(() => { refresh(); }, [refresh]);
  return { ...state, refresh };
}
