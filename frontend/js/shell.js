// Общая оболочка: страницы меняются без перезапуска диалога и дополнительных команд агенту.
import { bindMic } from './voice.js';
import { $, state } from './state.js';
import { html, rub } from './format.js';
import { mountEconomics } from './economics.js';

let market = 'both';

export function syncShell() {
  const page = document.body.dataset.page;
  $('currentSearchLink').hidden = !state.sid && page !== 'agent';
  $('currentSearchLink').textContent = page === 'agent' ? 'Текущий подбор' : 'Продолжить подбор';
  document.querySelectorAll('.site-nav [data-action]').forEach(button => {
    const active = page === 'history' ? button.dataset.action === 'open-chats'
      : page === 'apartments' && button.dataset.action === (market === 'new' ? 'new-buildings' : 'apartments');
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  document.querySelectorAll('[data-tab]').forEach(button => {
    const active = button.dataset.tab === (page === 'economics' ? 'economics' : page === 'agent' ? 'deal' : '');
    button.classList.toggle('is-active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
}

export function showPage(page) {
  const agent = page === 'agent' || page === 'economics';
  document.body.dataset.page = page;
  $('welcomePage').hidden = page !== 'welcome';
  $('aggregatorHome').hidden = page !== 'apartments';
  $('historyPage').hidden = page !== 'history';
  $('agentApp').hidden = !agent;
  $('view-deal').hidden = page !== 'agent';
  $('view-economics').hidden = page !== 'economics';
  document.querySelector('.workspace-heading').hidden = page !== 'agent';
  $('pageLabel').textContent = {welcome:'Подбор с агентом', apartments:'Поиск квартиры', history:'Ваши сохранённые подборы',
    agent:'От пожеланий до сделки', economics:'Экономика для Сбера'}[page];
  $('entryDialog').close();
  syncShell();
  if (page === 'economics') mountEconomics($('view-economics'));
  document.dispatchEvent(new CustomEvent('pagechange', {detail: {page}}));
}

export function navigate(page, {fresh = false} = {}) {
  const hash = page === 'agent' && state.sid && !fresh ? `#chat-${state.sid}`
    : {welcome:'#welcome', apartments:'#apartments', history:'#history', agent:'#agent', economics:'#economics'}[page];
  if (location.hash !== hash) history.pushState(null, '', hash);
  showPage(page);
  window.scrollTo(0, 0);
}

export function showAgent() {
  showPage('agent');
}

export function setChatExpanded(expanded) {
  $('chatPanel').classList.toggle('expanded', expanded);
  $('chatToggle').textContent = expanded ? 'Свернуть ↓' : 'Развернуть ↑';
  $('chatToggle').setAttribute('aria-expanded', String(expanded));
}

export function refreshContext(profile) {
  const labels = profile ? [profile.city, profile.rooms != null ? `${profile.rooms === 0 ? 'Студия' : `${profile.rooms_options?.length ? profile.rooms_options.join('–') : profile.rooms} комн.`}` : '',
    profile.budget_max ? `до ${rub(profile.budget_max)}` : '',
    { new: 'Новостройки', secondary: 'Вторичка', both: 'Новостройки и вторичка' }[profile.housing]] : [];
  $('searchContext').innerHTML = labels.filter(Boolean).map(t => html`<span>${t}</span>`).join('');
}

export function initShell({ start }) {
  let launching = false;
  bindMic($('welcomeMicBtn'), $('welcomeQuery'), {
    onFinal: () => { $('welcomeVoiceStatus').textContent = 'Проверьте текст и нажмите «Начать подбор».'; },
    onError: message => { $('welcomeVoiceStatus').textContent = message; },
  });
  bindMic($('entryMicBtn'), $('entryQuery'), {
    onFinal: () => { $('entryVoiceStatus').textContent = 'Проверьте текст и нажмите «Начать подбор».'; },
    onError: message => { $('entryVoiceStatus').textContent = message; },
  });
  const selectMarket = (value) => {
    market = value;
    document.querySelectorAll('[data-market]').forEach(button => {
      button.classList.toggle('on', button.dataset.market === value);
      button.setAttribute('aria-pressed', String(button.dataset.market === value));
    });
    syncShell();
  };
  const openEntry = () => {
    if (!$('entryDialog').open) $('entryDialog').showModal();
    $('entryQuery').focus();
  };
  const launch = async (query) => {
    if (launching || query.trim().length < 3) return;
    launching = true;
    const buttons = document.querySelectorAll('[data-action="portal-search"], #entryForm [type="submit"], #welcomeForm [type="submit"], [data-welcome-example]');
    buttons.forEach(button => button.disabled = true);
    try {
      navigate('agent', {fresh:true});
      setChatExpanded(true);
      await start(query);
      if (state.sid) { $('entryQuery').value = ''; $('welcomeQuery').value = ''; }
    } finally {
      launching = false;
      buttons.forEach(button => button.disabled = false);
      syncShell();
    }
  };
  $('chatToggle').addEventListener('click', () => setChatExpanded(!$('chatPanel').classList.contains('expanded')));
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'voice-search') { openEntry(); $('entryMicBtn').click(); }
    if (action === 'open-agent' || action === 'smart-search') openEntry();
    if (action === 'market') selectMarket(button.dataset.market);
    if (action === 'apartments') { selectMarket('both'); navigate('apartments'); }
    if (action === 'home') navigate('welcome');
    if (action === 'new-buildings') {
      selectMarket('new'); navigate('apartments');
      $('portalForm').scrollIntoView({block:'center'});
    }
    if (action === 'focus-city') {
      navigate('apartments'); $('portalCity').focus();
    }
    if (action === 'open-chats') navigate('history');
    if (action === 'close-entry') $('entryDialog').close();
    if (action === 'open-account') document.querySelector('#account button')?.click();
  });
  $('portalForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const city = $('portalCity').value.trim();
    const rooms = $('portalRooms').value;
    const budget = Number($('portalBudget').value);
    const housing = {both:'новостройки и вторичка', new:'только новостройки', secondary:'только вторичка'}[market];
    launch(`Ищу ${rooms === '0' ? 'студию' : `${rooms}-комнатную квартиру`} в ${city || 'Москве'}${budget > 0 ? ` до ${budget} млн рублей` : ', бюджет пока не знаю'}, ${housing}`);
  });
  $('welcomeQuery').addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      $('welcomeForm').requestSubmit();
    }
  });
  $('welcomeForm').addEventListener('submit', event => {
    event.preventDefault();
    launch($('welcomeQuery').value.trim());
  });
  document.querySelectorAll('[data-welcome-example]').forEach(button => button.addEventListener('click', () => launch(button.dataset.welcomeExample)));
  $('entryForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const query = $('entryQuery').value.trim();
    if (query.length >= 3) launch(query);
  });
  document.querySelectorAll('[data-entry-example]').forEach(button => button.addEventListener('click', () => {
    $('entryQuery').value = button.dataset.entryExample;
    $('entryQuery').focus();
  }));
  // Открытие/закрытие окна не должно терять набранный текст.
  $('entryDialog').addEventListener('close', syncShell);
  syncShell();
}
