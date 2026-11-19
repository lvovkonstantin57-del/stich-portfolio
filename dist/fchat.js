// Мини-чат поверх готовой сборки: находит «сделано вручную» в футере и ставит кнопку.
// Ключ НЕ здесь — запросы идут на /api/chat (stich-api).
(() => {
  const MARKUP = `
  <span class="fchat__city">Москва ·</span>
  <button class="fchat__toggle" type="button" aria-expanded="false" aria-controls="fchat-panel">
    <span class="fchat__dot" aria-hidden="true"></span>спросить меня
  </button>
  <div class="fchat__panel" id="fchat-panel" role="dialog" aria-label="Мини-чат" hidden>
    <header class="fchat__head">
      <span class="fchat__title">STICH · ЧАТ</span>
      <button class="fchat__close" type="button" aria-label="Закрыть">×</button>
    </header>
    <div class="fchat__log" aria-live="polite">
      <div class="fchat__msg fchat__msg--bot">Привет! Я ассистент Stich Production. Спроси про съёмку, монтаж или как связаться с Костей.</div>
    </div>
    <form class="fchat__form">
      <input class="fchat__input" type="text" maxlength="500" placeholder="Напиши вопрос…" autocomplete="off" required>
      <button class="fchat__send" type="submit" aria-label="Отправить">→</button>
    </form>
  </div>`;

  const history = [];
  const MAX_HISTORY = 10;

  function findTarget() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      if (n.nodeValue.includes('сделано вручную')) return n.parentElement;
    }
    return null;
  }

  function mount() {
    if (document.querySelector('.fchat')) return true;
    const target = findTarget();
    if (!target) return false;

    const root = document.createElement('div');
    root.className = 'fchat';
    root.innerHTML = MARKUP;
    // не удаляем узел React'а (иначе он может упасть при перерисовке) — прячем и ставим чат рядом
    target.style.display = 'none';
    target.after(root);
    wire(root);
    return true;
  }

  function wire(root) {
    const toggle = root.querySelector('.fchat__toggle');
    const panel  = root.querySelector('.fchat__panel');
    const close  = root.querySelector('.fchat__close');
    const log    = root.querySelector('.fchat__log');
    const form   = root.querySelector('.fchat__form');
    const input  = root.querySelector('.fchat__input');
    const send   = root.querySelector('.fchat__send');

    const setOpen = (open) => {
      panel.hidden = !open;
      toggle.setAttribute('aria-expanded', String(open));
      if (open) input.focus();
    };
    toggle.addEventListener('click', (e) => { e.stopPropagation(); setOpen(panel.hidden); });
    close.addEventListener('click', () => setOpen(false));
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') setOpen(false); });
    document.addEventListener('click', (e) => { if (!root.contains(e.target)) setOpen(false); });

    const addMsg = (text, who, extra = '') => {
      const el = document.createElement('div');
      el.className = `fchat__msg fchat__msg--${who} ${extra}`.trim();
      el.textContent = text;
      log.appendChild(el);
      log.scrollTop = log.scrollHeight;
      return el;
    };

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      const q = input.value.trim();
      if (!q) return;
      addMsg(q, 'user');
      history.push({ role: 'user', content: q });
      input.value = '';
      send.disabled = true;
      const typing = addMsg('печатает…', 'bot', 'fchat__msg--typing');
      try {
        const res = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages: history.slice(-MAX_HISTORY) }),
        });
        const data = await res.json().catch(() => ({}));
        const reply = res.ok ? data.reply : (data.detail || 'Что-то пошло не так, попробуй ещё раз.');
        typing.remove();
        addMsg(typeof reply === 'string' ? reply : 'Что-то пошло не так.', 'bot');
        if (res.ok) history.push({ role: 'assistant', content: reply });
      } catch {
        typing.remove();
        addMsg('Нет связи с сервером. Напиши Косте в Telegram: @l1ttleStich', 'bot');
      } finally {
        send.disabled = false;
        input.focus();
      }
    });
  }

  // React рендерит страницу позже и может перерисовать футер — следим за DOM
  const start = () => {
    mount();
    new MutationObserver(() => { if (!document.querySelector('.fchat')) mount(); })
      .observe(document.body, { childList: true, subtree: true });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
