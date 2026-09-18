export async function requestJSON(url, options = {}) {
  const controller = new AbortController();
  const relayAbort = () => controller.abort();
  if (options.signal) {
    if (options.signal.aborted) controller.abort();
    else options.signal.addEventListener("abort", relayAbort, { once: true });
  }
  const timeout = window.setTimeout(() => controller.abort(), options.timeout || 30000);
  try {
    const { timeout: _timeout, ...fetchOptions } = options;
    const response = await fetch(url, { ...fetchOptions, signal: controller.signal });
    let payload = null;
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
    if (!response.ok) {
      const error = new Error(payload?.error || `请求失败（${response.status}）`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时");
    throw error;
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", relayAbort);
  }
}

export function postJSON(url, body, options = {}) {
  return requestJSON(url, {
    ...options,
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
