/**
 * Reader preferences — device-local (localStorage), no backend.
 * The inline script in Base.astro stamps <html data-*> before first paint;
 * this module keeps everything live after load: the masthead theme toggle
 * and the preferences-page controls. Keep the pref tables here in sync
 * with that script and with the token blocks in src/styles/global.css.
 */
const PREFIX = 'eclecta:';

function get(key) {
  try {
    return localStorage.getItem(PREFIX + key);
  } catch {
    return null;
  }
}
/** Write without re-applying (import batches writes, then applies once). */
function write(key, value) {
  try {
    if (value === null) localStorage.removeItem(PREFIX + key);
    else localStorage.setItem(PREFIX + key, value);
  } catch {
    /* private mode — prefs just don't persist */
  }
}
function set(key, value) {
  write(key, value);
  apply();
  window.dispatchEvent(new CustomEvent('eclecta:prefs'));
}

/* value prefs: [storage key, html data-* attribute, non-default values] */
const VALUE_PREFS = [
  ['theme', 'theme', ['light', 'dark']],
  ['accent', 'accent', ['cobalt', 'moss', 'plum', 'ink']],
  ['fontSize', 'fontsize', ['s', 'l', 'xl']],
  ['bodyFont', 'bodyfont', ['sans']],
  ['density', 'density', ['compact']],
  ['leading', 'leading', ['tight', 'relaxed']],
  ['measure', 'measure', ['narrow', 'wide']],
  ['motion', 'motion', ['reduce']],
];

/* flag prefs: "1" stamps html[data-*="1"] */
const FLAG_PREFS = [
  ['showScores', 'showscores'],
  ['showSignals', 'showsignals'],
  ['justify', 'justify'],
  ['underlineLinks', 'underline'],
  ['wideSpacing', 'widespacing'],
];

/* every key export/import handles, in display order */
const KNOWN_KEYS = [
  ...VALUE_PREFS.map(([k]) => k),
  'contrast',
  ...FLAG_PREFS.map(([k]) => k),
  'expandDetails',
  'externalNewTab',
  'mutedCategories',
];

/* defaults, stored as null — keep in sync with data-pref-group-default in
   src/pages/preferences.astro */
const VALUE_DEFAULTS = {
  theme: 'auto', accent: 'signal', fontSize: 'm', bodyFont: 'serif',
  density: 'comfortable', leading: 'normal', measure: 'normal',
  motion: 'auto', contrast: 'auto',
};

/** Non-default values a key accepts (mutedCategories is handled apart). */
function allowedValues(key) {
  const vp = VALUE_PREFS.find(([k]) => k === key);
  if (vp) return vp[2];
  if (key === 'contrast') return ['high'];
  return ['1']; // every flag-style key
}

const SLUG_RE = /^[a-z][a-z0-9-]*$/;

let lastExpand = null;

function apply() {
  const d = document.documentElement;
  for (const [key, attr, allowed] of VALUE_PREFS) {
    const v = get(key);
    if (v && allowed.includes(v)) d.dataset[attr] = v;
    else delete d.dataset[attr];
  }
  for (const [key, attr] of FLAG_PREFS) {
    if (get(key) === '1') d.dataset[attr] = '1';
    else delete d.dataset[attr];
  }

  // contrast: "high" forces it; auto follows the system preference
  const contrast = get('contrast');
  if (contrast === 'high' || (contrast === null && matchMedia('(prefers-contrast: more)').matches)) {
    d.dataset.contrast = 'high';
  } else {
    delete d.dataset.contrast;
  }

  const muted = mutedList();
  if (muted.length) d.dataset.muted = muted.join(' ');
  else delete d.dataset.muted;

  // unfold per-pick details when the pref flips; manual toggling stays yours
  const expand = get('expandDetails') === '1';
  if (expand !== lastExpand) {
    for (const det of document.querySelectorAll('.pick__details')) det.open = expand;
    lastExpand = expand;
  }

  syncToggleLabels();
  syncControls();
  syncIo();
  syncCount();
}

/* ── muted sections (a comma list in one key) ────────────────────────── */
function mutedList() {
  // the slug filter keeps hand-pasted junk from desyncing attr and controls
  return (get('mutedCategories') || '').split(',').filter((s) => SLUG_RE.test(s));
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
    btn.setAttribute('aria-label', 'Theme: ' + currentTheme() + '. Click to change.');
  }
}

/* ── external links in a new tab (Preferences) ───────────────────────── */
document.addEventListener('click', (e) => {
  if (get('externalNewTab') !== '1') return;
  const a = e.target.closest('a[href]');
  if (!a || !/^https?:/.test(a.getAttribute('href') || '')) return;
  if (a.host === location.host || a.target) return;
  a.target = '_blank';
  a.rel = a.rel ? a.rel + ' noopener' : 'noopener';
});

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

/* ── settings as JSON: the "Your data" panel ─────────────────────────── */
function ioEl() {
  return document.querySelector('[data-prefs-io]');
}

function currentJson() {
  const out = {};
  for (const k of KNOWN_KEYS) {
    const v = get(k);
    if (v !== null) out[k] = v;
  }
  return JSON.stringify(out, null, 2);
}

function syncIo() {
  const ta = ioEl();
  // leave the box alone while the reader is pasting into it
  if (ta && document.activeElement !== ta) ta.value = currentJson();
}

function syncCount() {
  const el = document.querySelector('[data-prefs-count]');
  if (!el) return;
  const n = KNOWN_KEYS.filter((k) => get(k) !== null).length;
  el.textContent = n === 0 ? 'all defaults' : n === 1 ? '1 setting changed' : n + ' settings changed';
}

/** Status line for the data panel; cleared then set so screen readers re-announce. */
function say(msg) {
  const el = document.querySelector('[data-prefs-status]');
  if (!el) return;
  el.textContent = '';
  setTimeout(() => {
    el.textContent = msg;
  }, 30);
}

async function exportToClipboard() {
  const ta = ioEl();
  if (!ta) return;
  ta.value = currentJson();
  try {
    await navigator.clipboard.writeText(ta.value);
    say('Copied. Paste it on another device and apply.');
  } catch {
    ta.focus();
    ta.select();
    say('Clipboard is blocked here. The settings are selected: copy them yourself.');
  }
}

/**
 * Read one pasted entry: {ok} says whether it was recognised, {value} is
 * what to store (null = the default). Explicit defaults and "0" flags are
 * recognised resets, not junk.
 */
function readImportEntry(key, v) {
  if (typeof v !== 'string' || v === '') return { ok: false, value: null };
  if (key === 'mutedCategories') {
    const items = v.split(',').filter((s) => SLUG_RE.test(s));
    return { ok: items.join(',') === v && items.length > 0, value: items.length ? items.join(',') : null };
  }
  const allowed = allowedValues(key);
  if (allowed.includes(v)) return { ok: true, value: v };
  if (v === VALUE_DEFAULTS[key] || (allowed[0] === '1' && v === '0')) return { ok: true, value: null };
  return { ok: false, value: null };
}

function importFromIo() {
  const ta = ioEl();
  if (!ta) return;
  let data;
  try {
    data = JSON.parse(ta.value);
  } catch {
    say('That is not valid JSON. Paste a set copied from this panel.');
    return;
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) {
    say('Expected a settings object. Paste a set copied from this panel.');
    return;
  }
  // replace semantics: pasted keys win, everything else returns to default
  let ignored = 0;
  for (const key of Object.keys(data)) {
    if (!KNOWN_KEYS.includes(key)) ignored++;
  }
  for (const key of KNOWN_KEYS) {
    if (data[key] === undefined) {
      write(key, null);
      continue;
    }
    const entry = readImportEntry(key, data[key]);
    if (!entry.ok) ignored++;
    write(key, entry.value);
  }
  apply();
  window.dispatchEvent(new CustomEvent('eclecta:prefs'));
  say(
    ignored === 0
      ? 'Applied. Anything you left out is back to defaults.'
      : 'Applied. Skipped ' + ignored + (ignored === 1 ? ' unrecognised entry.' : ' unrecognised entries.')
  );
}

/* ── event wiring ────────────────────────────────────────────────────── */
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
  if (e.target.closest('[data-prefs-export]')) {
    exportToClipboard();
    return;
  }
  if (e.target.closest('[data-prefs-import]')) {
    importFromIo();
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
    say('Everything is back to defaults.');
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

// re-stamp when the system contrast preference changes (contrast pref = auto)
try {
  matchMedia('(prefers-contrast: more)').addEventListener('change', () => apply());
} catch {
  /* older engines: the pref still works, it just won't live-update */
}

apply();
