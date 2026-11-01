// Оболочка согласованного дизайна; все данные и действия поступают от рабочего агента.
import { $, state } from './state.js';
import { html, rub } from './format.js';

export function showAgent() {
  $('aggregatorHome').hidden = true;
  $('agentApp').hidden = false;
  $('entryDialog').close();
}

export function setChatExpanded(expanded) {
  $('chatPanel').classList.toggle('expanded', expanded);
  $('chatToggle').textContent = expanded ? 'Свернуть ↓' : 'Развернуть ↑';
  $('chatToggle').setAttribute('aria-expanded', String(expanded));
}

export function refreshContext(profile) {
  const labels = profile ? [profile.city, profile.rooms != null ? `${profile.rooms === 0 ? 'Студия' : `${profile.rooms} комн.`}` : '',
    profile.budget_max ? `до ${rub(profile.budget_max)}` : '',
    { new: 'Новостройки', secondary: 'Вторичка', both: 'Новостройки и вторичка' }[profile.housing]] : [];
  $('searchContext').innerHTML = labels.filter(Boolean).map(t => html`<span>${t}</span>`).join('');
}

export function initShell({ start }) {
  let market = 'both';
  const selectMarket = (value) => {
    market = value;
    document.querySelectorAll('[data-market]').forEach(button => {
      button.classList.toggle('on', button.dataset.market === value);
      button.setAttribute('aria-pressed', String(button.dataset.market === value));
    });
  };
  $('chatToggle').addEventListener('click', () => setChatExpanded(!$('chatPanel').classList.contains('expanded')));
  const openEntry = () => {
    if (state.sid) { showAgent(); setChatExpanded(true); return; }
    $('entryDialog').showModal();
    $('entryQuery').focus();
  };
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    const action = button.dataset.action;
    if (action === 'open-agent') openEntry();
    if (action === 'market') selectMarket(button.dataset.market);
    if (action === 'new-buildings') {
      selectMarket('new');
      $('portalCity').focus();
    }
    if (action === 'focus-city') $('portalCity').focus();
    if (action === 'open-chats') {
      showAgent(); setChatExpanded(true);
      if ($('chatsPanel').hidden) $('chatsBtn').click();
    }
    if (action === 'close-entry') $('entryDialog').close();
    if (action === 'home') {
      $('agentApp').hidden = true;
      $('aggregatorHome').hidden = false;
    }
    if (action === 'open-account') {
      showAgent();
      document.querySelector('#account button')?.click();
    }
    if (action === 'portal-search') {
      const city = $('portalCity').value.trim();
      const rooms = $('portalRooms').value;
      const budget = Number($('portalBudget').value);
      const housing = {both: 'новостройки и вторичка', new: 'только новостройки', secondary: 'только вторичка'}[market];
      const query = `Ищу ${rooms === '0' ? 'студию' : `${rooms}-комнатную квартиру`} в ${city || 'Москве'}${budget > 0 ? ` до ${budget} млн рублей` : ', бюджет пока не знаю'}, ${housing}`;
      showAgent(); setChatExpanded(true); start(query);
    }
  });
  $('entryForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const query = $('entryQuery').value.trim();
    if (query.length < 3) return;
    showAgent(); setChatExpanded(true); start(query);
  });
  document.querySelectorAll('[data-entry-example]').forEach(button => button.addEventListener('click', () => {
    showAgent(); setChatExpanded(true); start(button.dataset.entryExample);
  }));
}
