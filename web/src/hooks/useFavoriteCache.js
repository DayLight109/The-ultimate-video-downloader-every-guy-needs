import { useCallback } from "react";
import { toggleStoredFavorite } from "../lib/storage";

// Stable callback so memoised cards do not re-render on every parent update.
export function useFavoriteCache(user, notify) {
  return useCallback(async (video) => {
    if (!video) return;
    try {
      const added = toggleStoredFavorite(user, video);
      notify(added ? "已收藏" : "已取消收藏");
    } catch {
      notify("收藏保存失败，请检查浏览器存储空间", "error");
    }
  }, [notify, user]);
}
