/**
 * Reader preferences — device-local (localStorage), no backend.
 * The inline script in Base.astro stamps <html data-*> before first paint;
 * this module keeps everything live after load: the theme toggle, the
 * preferences page controls, and the per-pick thumbs.
 */
const PREFIX = 'lede:';

function get(key) {
  try {
    return localStorage.getItem(PREFIX + key);
  } catch {
    return null;
  }
}
function set(key, value) {
  try {
    if (value === null) localStorage.removeItem(PREFIX + key);
    else localStorage.setItem(PREFIX + key, value);
  } catch {
    /* private mode — prefs just don't persist */
  }
  apply();
  window.dispatchEvent(new CustomEvent('lede:prefs'));
}

const FLAG_ATTRS = [
  ['showScores', 'showscores'],
  ['showSignals', 'showsignals'],
  ['hideDownvoted', 'hidedown'],
];

function apply() {
  const d = document.documentElement;
  const theme = get('theme');
  if (theme === 'light' || theme === 'dark') d.dataset.theme = theme;
  else delete d.dataset.theme;

  const size = get('fontSize');
  if (size && size !== 'm') d.dataset.fontsize = size;
  else delete d.dataset.fontsize;

  const density = get('density');
  if (density === 'compact') d.dataset.density = 'compact';
  else delete d.dataset.density;

  for (const [key, attr] of FLAG_ATTRS) {
    if (get(key) === '1') d.dataset[attr] = '1';
    else delete d.dataset[attr];
  }
  applyVotes();
  syncToggleLabels();
  syncControls();
}

/* ── theme toggle (masthead) ─────────────────────────────────────────── */
const THEME_CYCLE = { auto: 'light', light: 'dark', dark: 'auto' };

function currentTheme() {
  const t = get('theme');
  return t === 'light' || t === 'dark' ? t : 'auto';
}

function syncToggleLabels() {
  for (const btn of document.querySelectorAll('[data-theme-toggle]')) {
    btn.textContent = currentTheme();
    btn.setAttribute('aria-label', 'Theme: ' + currentTheme() + ' — click to change');
  }
}

/* ── thumbs (device-local) ───────────────────────────────────────────── */
function votes() {
  try {
    return JSON.parse(get('votes') || '{}') || {};
  } catch {
    return {};
  }
}

function applyVotes() {
  const v = votes();
  for (const el of document.querySelectorAll('[data-pick-id]')) {
    const vote = v[el.dataset.pickId];
    if (vote === 'up' || vote === 'down') el.dataset.vote = vote;
    else delete el.dataset.vote;
  }
}

/* ── preferences page controls ───────────────────────────────────────── */
function syncControls() {
  for (const input of document.querySelectorAll('input[data-pref]')) {
    const stored = get(input.dataset.pref);
    if (input.type === 'radio') {
      const group = input.closest('[data-pref-group]');
      const fallback = group ? group.dataset.prefGroupDefault : '';
      input.checked = (stored === null ? fallback : stored) === input.value;
    } else if (input.type === 'checkbox') {
      input.checked = stored === '1';
    }
  }
}

document.addEventListener('click', (e) => {
  const toggle = e.target.closest('[data-theme-toggle]');
  if (toggle) {
    set('theme', THEME_CYCLE[currentTheme()] === 'auto' ? null : THEME_CYCLE[currentTheme()]);
    return;
  }
  const voteBtn = e.target.closest('[data-vote-btn]');
  if (voteBtn) {
    const pick = voteBtn.closest('[data-pick-id]');
    if (!pick) return;
    const v = votes();
    const id = pick.dataset.pickId;
    v[id] = v[id] === voteBtn.dataset.voteBtn ? undefined : voteBtn.dataset.voteBtn;
    if (v[id] === undefined) delete v[id];
    set('votes', JSON.stringify(v));
    return;
  }
  const clear = e.target.closest('[data-prefs-reset]');
  if (clear) {
    try {
      for (const k of Object.keys(localStorage)) {
        if (k.startsWith(PREFIX)) localStorage.removeItem(k);
      }
    } catch {}
    apply();
  }
});

document.addEventListener('change', (e) => {
  const input = e.target.closest('input[data-pref]');
  if (!input) return;
  const key = input.dataset.pref;
  if (input.type === 'radio') {
    const isDefault = input.value === (input.closest('[data-pref-group]')?.dataset.prefGroupDefault ?? '');
    set(key, isDefault ? null : input.value);
  } else if (input.type === 'checkbox') {
    set(key, input.checked ? '1' : null);
  }
});

apply();
