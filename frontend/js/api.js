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
  product: (sid, product, amount) => request(`/api/session/${sid}/product`, { method: "POST", body: { product, amount } }),
  economics: (params) => request(`/api/economics?${new URLSearchParams(params)}`),

  // диалоги: лента (история + живые события), сообщение (агент работает — ответ; закончил — продолжение)
  chats: () => request("/api/chats"),
  chatStream: (id, after = 0) => new EventSource(`/api/chat/${id}/stream?after=${after}`),
  chatMessage: (id, text, marks = {}) => request(`/api/chat/${id}/message`, { method: "POST", body: { text, ...marks } }),

  me: () => request("/api/me"),
  login: (email, password) => request("/api/auth/login", { method: "POST", body: { email, password } }),
  register: (email, password, name) => request("/api/auth/register", { method: "POST", body: { email, password, name } }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
};
