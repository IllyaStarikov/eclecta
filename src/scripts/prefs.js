/**
 * Reader preferences — device-local (localStorage), no backend.
 * The inline script in Base.astro stamps <html data-*> before first paint;
 * this module keeps everything live after load: the masthead theme toggle
 * and the preferences-page controls.
 */
const PREFIX = 'eclecta:';

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
  window.dispatchEvent(new CustomEvent('eclecta:prefs'));
}

const FLAG_ATTRS = [
  ['showScores', 'showscores'],
  ['showSignals', 'showsignals'],
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

  const muted = mutedList();
  if (muted.length) d.dataset.muted = muted.join(' ');
  else delete d.dataset.muted;

  syncToggleLabels();
  syncControls();
}

/* ── muted sections (a comma list in one key) ────────────────────────── */
function mutedList() {
  return (get('mutedCategories') || '').split(',').filter(Boolean);
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
  // section mutes: a checked box means the section is SHOWN (not muted)
  const muted = new Set(mutedList());
  for (const box of document.querySelectorAll('[data-mute]')) {
    box.checked = !muted.has(box.dataset.mute);
  }
}

/** Recompute the muted list from the section toggles (unchecked = muted). */
function writeMutesFromControls() {
  const muted = [];
  for (const box of document.querySelectorAll('[data-mute]')) {
    if (!box.checked) muted.push(box.dataset.mute);
  }
  set('mutedCategories', muted.length ? muted.join(',') : null);
}

function allMuteSlugs() {
  return [...document.querySelectorAll('[data-mute]')].map((b) => b.dataset.mute);
}

document.addEventListener('click', (e) => {
  const toggle = e.target.closest('[data-theme-toggle]');
  if (toggle) {
    set('theme', THEME_CYCLE[currentTheme()] === 'auto' ? null : THEME_CYCLE[currentTheme()]);
    return;
  }
  const muteAll = e.target.closest('[data-mute-all]');
  if (muteAll) {
    set('mutedCategories', allMuteSlugs().join(',') || null);
    return;
  }
  const showAll = e.target.closest('[data-mute-none]');
  if (showAll) {
    set('mutedCategories', null);
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
  const muteBox = e.target.closest('[data-mute]');
  if (muteBox) {
    writeMutesFromControls();
    return;
  }
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
