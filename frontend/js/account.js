// Вход и регистрация. Без входа диалоги живут в этом браузере (cookie); после входа они переходят
// в аккаунт и открываются с любого устройства.
import { api } from "./api.js";
import { el, html } from "./format.js";

let user = null;
let onChange = () => {};

export async function initAccount(opts = {}) {
  onChange = opts.onChange || onChange;
  try { user = (await api.me()).user; } catch { user = null; }
  render();
  cookieNotice();
}

function render() {
  const slot = document.getElementById("account");
  slot.innerHTML = user
    ? html`<button class="account-btn" data-account="menu" aria-haspopup="menu" title="${user.email}">
        <span class="avatar" aria-hidden="true">${(user.name || user.email)[0].toUpperCase()}</span>
        <span class="account-name">${user.name || user.email}</span></button>`
    : html`<button class="account-btn" data-account="login">Войти</button>`;
  slot.querySelector("button").addEventListener("click", () => (user ? menu(slot) : openDialog("login")));
}

function menu(slot) {
  if (slot.querySelector(".account-menu")) { slot.querySelector(".account-menu").remove(); return; }
  const m = el(html`<div class="account-menu" role="menu">
    <p class="account-email">${user.email}</p>
    <button role="menuitem" data-logout>Выйти</button></div>`);
  m.querySelector("[data-logout]").addEventListener("click", async () => {
    try { await api.logout(); } catch { /* cookie всё равно сотрём при следующем входе */ }
    user = null;
    render();
    onChange();
  });
  slot.append(m);
  const close = (e) => {
    if (slot.contains(e.target)) return;
    m.remove();
    document.removeEventListener("click", close);
  };
  setTimeout(() => document.addEventListener("click", close));
}

function openDialog(mode) {
  document.querySelector("dialog.auth")?.remove();
  const reg = mode === "register";
  const d = el(html`
    <dialog class="auth" aria-labelledby="authTitle">
      <form method="dialog" class="auth-form">
        <h3 id="authTitle">${reg ? "Регистрация" : "Вход"}</h3>
        <p class="auth-lead">${reg
          ? "Диалоги этого браузера перейдут в аккаунт — их можно будет открыть с любого устройства."
          : "Войдите, чтобы видеть свои диалоги на любом устройстве."}</p>
        ${html.raw(reg ? html`<label>Имя<input name="name" autocomplete="name" maxlength="120"></label>` : "")}
        <label>Почта<input name="email" type="email" required autocomplete="email"></label>
        <label>Пароль<input name="password" type="password" required minlength="${reg ? 8 : 1}"
          autocomplete="${reg ? "new-password" : "current-password"}"></label>
        <p class="auth-error" role="alert" hidden></p>
        <div class="auth-actions">
          <button class="btn btn-primary" value="submit">${reg ? "Зарегистрироваться" : "Войти"}</button>
          <button class="btn btn-ghost" value="cancel" formnovalidate>Отмена</button>
        </div>
        <button type="button" class="link-btn" data-switch>${reg ? "Уже есть аккаунт — войти" : "Нет аккаунта — зарегистрироваться"}</button>
      </form>
    </dialog>`);
  document.body.append(d);
  const form = d.querySelector("form");
  const error = d.querySelector(".auth-error");
  d.querySelector("[data-switch]").addEventListener("click", () => { d.close(); openDialog(reg ? "login" : "register"); });
  form.addEventListener("submit", async (e) => {
    if (e.submitter?.value === "cancel") return; // закроется сам
    e.preventDefault();
    const f = new FormData(form);
    const submit = form.querySelector('[value="submit"]');
    submit.disabled = true;
    error.hidden = true;
    try {
      const r = reg
        ? await api.register(f.get("email"), f.get("password"), f.get("name"))
        : await api.login(f.get("email"), f.get("password"));
      user = r.user;
      d.close();
      render();
      onChange();
    } catch (err) {
      error.textContent = err.message;
      error.hidden = false;
      submit.disabled = false;
    }
  });
  d.addEventListener("close", () => d.remove());
  d.showModal();
}

function cookieNotice() {
  try {
    if (localStorage.getItem("cookieNotice")) return;
  } catch { return; }
  const n = el(html`<div class="cookie-note" role="status">
    <p>Диалоги сохраняются в этом браузере (cookie), чтобы к ним можно было вернуться. После входа — в вашем аккаунте.</p>
    <button class="btn btn-ghost btn-small">Понятно</button></div>`);
  n.querySelector("button").addEventListener("click", () => {
    try { localStorage.setItem("cookieNotice", "1"); } catch { /* приватный режим */ }
    n.remove();
  });
  document.body.append(n);
}
