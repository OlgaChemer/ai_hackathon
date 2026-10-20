// Обёртки над HTTP API агента.

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* не JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  health: () => request("/api/health"),
  start: (query) => request("/api/session", { method: "POST", body: { query } }),
  confirm: (sid, kind, approved, data = {}) =>
    request(`/api/session/${sid}/confirm`, { method: "POST", body: { kind, approved, data } }),
  seller: (sid, text) => request(`/api/session/${sid}/seller`, { method: "POST", body: { text } }),
  economics: (params) => request(`/api/economics?${new URLSearchParams(params)}`),
  stream: (sid) => new EventSource(`/api/session/${sid}/stream`),
};
