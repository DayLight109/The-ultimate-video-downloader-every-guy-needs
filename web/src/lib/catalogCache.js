const DB_NAME = "avweb-catalog";
const DB_VERSION = 1;
const SCHEMA_VERSION = 2;
const META_KEY = "current";

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("目录缓存读取失败"));
  });
}

function transactionDone(transaction) {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error || new Error("目录缓存写入已中止"));
    transaction.onerror = () => reject(transaction.error || new Error("目录缓存写入失败"));
  });
}

function openDatabase() {
  return new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error("当前浏览器不支持目录缓存"));
      return;
    }
    const request = window.indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains("meta")) database.createObjectStore("meta", { keyPath: "id" });
      if (!database.objectStoreNames.contains("sites")) database.createObjectStore("sites", { keyPath: "site" });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error("目录缓存无法打开"));
    request.onblocked = () => reject(new Error("旧页面占用了目录缓存，请关闭后重试"));
  });
}

export async function readCatalogCache() {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(["meta", "sites"], "readonly");
    const done = transactionDone(transaction);
    const [meta, sites] = await Promise.all([
      requestResult(transaction.objectStore("meta").get(META_KEY)),
      requestResult(transaction.objectStore("sites").getAll()),
    ]);
    await done;
    if (!meta || meta.schema !== SCHEMA_VERSION || !Array.isArray(meta.metadata) || !Array.isArray(sites) || !sites.length) {
      return null;
    }
    const available = new Set(sites.filter((entry) => Array.isArray(entry.videos)).map((entry) => entry.site));
    if (meta.metadata.some((entry) => !available.has(entry.site))) return null;
    return { metadata: meta.metadata, savedAt: Number(meta.savedAt || 0), sites };
  } finally {
    database.close();
  }
}

export async function writeCatalogCache(metadata, sites) {
  const database = await openDatabase();
  try {
    const transaction = database.transaction(["meta", "sites"], "readwrite");
    const done = transactionDone(transaction);
    const siteStore = transaction.objectStore("sites");
    siteStore.clear();
    sites.forEach((entry) => siteStore.put({
      site: entry.site,
      count: entry.videos.length,
      videos: entry.videos,
    }));
    transaction.objectStore("meta").put({
      id: META_KEY,
      schema: SCHEMA_VERSION,
      metadata,
      savedAt: Date.now(),
    });
    await done;
    window.navigator.storage?.persist?.().catch(() => {});
  } finally {
    database.close();
  }
}
