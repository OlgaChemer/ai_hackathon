// Обёртки над HTTP API агента.

async function request(path, options = {}) {
  const command = options.method === "POST" && /\/(?:message|confirm)$/.test(path);
  const body = options.body ? { ...options.body } : undefined;
  if (command && !body.command_id) body.command_id = crypto.randomUUID();
  const init = {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: body ? JSON.stringify(body) : undefined,
  };
  let res;
  try { res = await fetch(path, init); }
  catch (error) {
    if (!command) throw error;
    // Повтор сетевой отправки использует тот же ID, сервер вернёт квитанцию.
    res = await fetch(path, init);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* не JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  voiceMode: false,
  decisions: {},
  health: () => request("/api/health"),
  start: (query) => request("/api/session", { method: "POST", body: { query, voice_mode: api.voiceMode } }),
  confirm: (sid, kind, approved, data = {}, decisionId = api.decisions[kind]) =>
    request(`/api/session/${sid}/confirm`, { method: "POST", body: { kind, approved, data, voice_mode: api.voiceMode,
      ...(decisionId ? {decision_id: decisionId} : {}) } }),
  seller: (sid, text) => request(`/api/session/${sid}/seller`, { method: "POST", body: { text } }),
  product: (sid, product, amount) => request(`/api/session/${sid}/product`, { method: "POST", body: { product, amount } }),
  economics: (params) => request(`/api/economics?${new URLSearchParams(params)}`),

  // диалоги: лента (история + живые события), сообщение (агент работает — ответ; закончил — продолжение)
  chats: () => request("/api/chats"),
  chatStream: (id, after = 0) => new EventSource(`/api/chat/${id}/stream?after=${after}`),
  chatMessage: (id, text, marks = {}) => request(`/api/chat/${id}/message`, { method: "POST", body: { text, ...marks, voice_mode: api.voiceMode } }),

  me: () => request("/api/me"),
  login: (email, password) => request("/api/auth/login", { method: "POST", body: { email, password } }),
  register: (email, password, name) => request("/api/auth/register", { method: "POST", body: { email, password, name } }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
};
