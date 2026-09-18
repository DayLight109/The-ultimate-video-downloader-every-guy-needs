import { useCallback, useEffect, useState } from "react";
import { readStoredList, writeStoredList } from "../lib/storage";

export function useStoredList(type, user) {
  const [items, setItems] = useState(() => readStoredList(type, user));

  useEffect(() => {
    setItems(readStoredList(type, user));
    const sync = (event) => {
      if (!event.detail || (event.detail.type === type && event.detail.user === user)) {
        setItems(readStoredList(type, user));
      }
    };
    window.addEventListener("storage", sync);
    window.addEventListener("avweb-storage", sync);
    return () => {
      window.removeEventListener("storage", sync);
      window.removeEventListener("avweb-storage", sync);
    };
  }, [type, user]);

  const update = useCallback((next) => {
    const value = typeof next === "function" ? next(readStoredList(type, user)) : next;
    writeStoredList(type, user, value);
    setItems(value);
  }, [type, user]);

  return [items, update];
}
