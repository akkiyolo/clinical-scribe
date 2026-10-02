/* ClinicalScribe single-page app: vanilla JS, no build step, no inline handlers.
 *
 *  1. Config and state          7. Patient views
 *  2. DOM helpers               8. Doctor views
 *  3. API client                9. Admin views
 *  4. UI components            10. Recorder module
 *  5. Router                   11. Voice module
 *  6. Auth views               12. Bootstrap
 *
 * Every piece of server data reaches the page through textContent or DOM nodes, never innerHTML.
 */
'use strict';

/* ═══════════════ 1. Config and state ═══════════════ */

const meta = (name) => document.querySelector(`meta[name="${name}"]`)?.content || '';
const CONFIG = {
  env: meta('app-env'),
  voiceEnabled: meta('voice-enabled') === 'true',
  maxAudioMB: Number(meta('max-audio-mb')) || 25,
};
const PAGE_SIZE = 20;
const POLL_MS = 2500;
const state = {
  user: null, // current user from /api/auth/me
  cleanups: [], // functions run when the route changes (timers, polls, media, object URLs)
  navId: 0, // increments on every navigation; async work compares it to detect staleness
};

/* ═══════════════ 2. DOM helpers ═══════════════ */

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const APP = $('#app');
const enc = encodeURIComponent;

/** Create an element. props: class, text, dataset, on:{event:fn}, value, boolean/other attributes. */
function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key === 'text') el.textContent = value;
    else if (key === 'dataset') Object.assign(el.dataset, value);
    else if (key === 'on') for (const [evt, fn] of Object.entries(value)) el.addEventListener(evt, fn);
    else if (key === 'value') el.value = value;
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, value);
  }
  return append(el, children);
}

function append(parent, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

const clear = (el) => { el.replaceChildren(); return el; };
const slot = (name, root = APP) => root.querySelector(`[data-slot="${name}"]`);
const setText = (name, value, root = APP) => { const el = slot(name, root); if (el) el.textContent = value ?? ''; };
const toggle = (el, visible) => el.classList.toggle('hidden', !visible);

/** Replace the contents of a slot and return it. */
function fill(name, ...nodes) {
  const el = slot(name);
  if (el) { clear(el); append(el, nodes); }
  return el;
}

/* ═══════════════ 3. API client ═══════════════ */

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function csrfToken() {
  const cookie = document.cookie.split('; ').find((c) => c.startsWith('csrf_token='));
  return cookie ? decodeURIComponent(cookie.slice('csrf_token='.length)) : meta('csrf-token');
}

function errorMessage(status, data) {
  if (status === 429) return 'Too many requests. Please slow down and try again in a minute.';
  if (status === 413) return 'That file or request is too large.';
  if (status >= 500) return 'Something went wrong on our side. Please try again.';
  if (data && typeof data.detail === 'string') return data.detail;
  if (data && Array.isArray(data.detail)) return data.detail.map((e) => e.msg || '').join('; ');
  return `Request failed (${status})`;
}

/** Handle the status codes every call shares: 401 sends the user to sign in, 403 may mean "not verified". */
function handleAuthFailure(status, message, opts) {
  if (opts.keepSession) return;
  if (status === 401) {
    state.user = null;
    if (!['/login', '/register'].includes(currentPath())) go('/login');
  } else if (status === 403 && /not verified/i.test(message) && state.user?.role === 'doctor') {
    refreshUser().then(() => {
      if (currentPath() !== '/doctor/verification') go('/doctor/verification');
    });
  }
}

async function api(method, path, body, opts = {}) {
  const headers = { Accept: 'application/json' };
  const init = { method, credentials: 'same-origin', headers };
  if (method !== 'GET' && method !== 'HEAD') {
    headers['X-CSRF-Token'] = csrfToken();
    if (body instanceof FormData) init.body = body;
    else if (body !== undefined && body !== null) {
      headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (_) {
    throw new ApiError(0, 'Network error. Check your connection and try again.');
  }
  if (res.ok) {
    if (opts.blob) return res.blob();
    if (res.status === 204) return null;
    return res.json().catch(() => ({}));
  }
  const data = await res.json().catch(() => null);
  const message = errorMessage(res.status, data);
  handleAuthFailure(res.status, message, opts);
  throw new ApiError(res.status, message);
}

const apiGet = (path, opts) => api('GET', path, null, opts);
const apiPost = (path, body, opts) => api('POST', path, body ?? null, opts);
const apiPut = (path, body, opts) => api('PUT', path, body, opts);
const apiPatch = (path, body, opts) => api('PATCH', path, body, opts);
const apiDelete = (path, opts) => api('DELETE', path, null, opts);

/** Upload a file with progress (XMLHttpRequest, because fetch cannot report upload progress). */
function uploadFile(path, file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('file', file, file.name);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.setRequestHeader('X-CSRF-Token', csrfToken());
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.addEventListener('progress', (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    });
    xhr.addEventListener('load', () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch (_) { /* non-JSON error body */ }
      if (xhr.status >= 200 && xhr.status < 300) return resolve(data || {});
      const message = errorMessage(xhr.status, data);
      handleAuthFailure(xhr.status, message, {});
      reject(new ApiError(xhr.status, message));
    });
    xhr.addEventListener('error', () => reject(new ApiError(0, 'Network error. Check your connection and try again.')));
    xhr.send(form);
  });
}

async function refreshUser() {
  try {
    state.user = await apiGet('/api/auth/me', { keepSession: true });
    renderNav();
  } catch (_) { /* leave the old value; the next request will redirect if the session is gone */ }
  return state.user;
}

/* ═══════════════ 4. UI components ═══════════════ */

/* ── toast ── */
function toast(message, type = 'info', ms = 5000) {
  const icon = { success: '✓', error: '!', info: 'i' }[type] || 'i';
  const el = h('div', { class: `toast toast-${type}`, role: type === 'error' ? 'alert' : null },
    h('span', { class: 'toast-icon', 'aria-hidden': 'true', text: icon }), h('span', { text: message }));
  $('#toast-container').append(el);
  setTimeout(() => { el.classList.add('leaving'); setTimeout(() => el.remove(), 300); }, ms);
}

/* ── modal with focus trap, Escape to close and focus restore ── */
const modal = { open: false, opener: null, onClose: null };
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function openModal({ title, body, footer = [], onClose }) {
  closeModal();
  $('#modal-title').textContent = title;
  append(clear($('#modal-body')), [body]);
  append(clear($('#modal-footer')), footer);
  toggle($('#modal-footer'), footer.length > 0);
  modal.open = true;
  modal.opener = document.activeElement;
  modal.onClose = onClose || null;
  toggle($('#modal-overlay'), true);
  const first = $(FOCUSABLE, $('#modal-body')) || $('#modal-close');
  (first || $('#modal')).focus();
}

function closeModal() {
  if (!modal.open) return;
  modal.open = false;
  toggle($('#modal-overlay'), false);
  clear($('#modal-body'));
  clear($('#modal-footer'));
  const callback = modal.onClose;
  modal.onClose = null;
  if (modal.opener && document.contains(modal.opener)) modal.opener.focus();
  if (callback) callback();
}

function wireModal() {
  $('#modal-close').addEventListener('click', closeModal);
  $('#modal-overlay').addEventListener('mousedown', (e) => { if (e.target === e.currentTarget) closeModal(); });
  document.addEventListener('keydown', (e) => {
    if (!modal.open) return;
    if (e.key === 'Escape') { e.preventDefault(); closeModal(); return; }
    if (e.key !== 'Tab') return;
    const items = $$(FOCUSABLE, $('#modal')).filter((el) => el.offsetParent !== null);
    if (!items.length) return;
    const [first, last] = [items[0], items[items.length - 1]];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  });
}

/* ── form fields ── */
let fieldSeq = 0;
function fieldNode(f) {
  const id = `fld-${++fieldSeq}`;
  const common = { id, name: f.name, required: f.required || null, maxlength: f.maxLength || null, placeholder: f.placeholder || null };
  if (f.type === 'checkbox') {
    const box = h('input', { id, name: f.name, type: 'checkbox' });
    box.checked = Boolean(f.value);
    return h('div', { class: 'field' }, h('label', { class: 'check', for: id }, box, h('span', { text: f.label })),
      f.help ? h('span', { class: 'field-hint', text: f.help }) : null);
  }
  let control;
  if (f.type === 'textarea') control = h('textarea', { ...common, rows: f.rows || 4 });
  else if (f.type === 'select') {
    control = h('select', common, (f.options || []).map((o) => h('option', { value: o.value, text: o.label })));
  } else control = h('input', { ...common, type: f.type || 'text', min: f.min ?? null, max: f.max ?? null });
  if (f.value !== undefined) control.value = f.value;
  return h('div', { class: 'field' },
    h('label', { for: id }, f.label, f.optional ? h('em', { text: 'optional' }) : null),
    control,
    f.help ? h('span', { class: 'field-hint', text: f.help }) : null,
    h('span', { class: 'field-error', 'data-error-for': f.name, role: 'alert' }));
}

function clearFieldErrors(form) {
  $$('[data-error-for]', form).forEach((el) => { el.textContent = ''; });
  $$('[aria-invalid]', form).forEach((el) => el.removeAttribute('aria-invalid'));
}

function setFieldError(form, name, message) {
  const slotEl = $(`[data-error-for="${name}"]`, form);
  const control = form.elements[name];
  if (slotEl) slotEl.textContent = message;
  if (control && control.setAttribute) control.setAttribute('aria-invalid', 'true');
  return Boolean(slotEl);
}

/** Server validation errors look like "field: message; field2: message". Map them onto the form. */
function applyServerErrors(form, error, banner) {
  clearFieldErrors(form);
  const leftovers = [];
  for (const part of String(error.message).split('; ')) {
    const m = /^([a-z_]+): (.+)$/.exec(part);
    if (!(m && setFieldError(form, m[1], m[2]))) leftovers.push(part);
  }
  if (leftovers.length && banner) showBanner(banner, leftovers.join(' '));
  else if (leftovers.length) toast(leftovers.join(' '), 'error');
}

function showBanner(el, message) { el.textContent = message; toggle(el, true); }
function hideBanner(el) { if (el) { el.textContent = ''; toggle(el, false); } }

/** Read a form into an object. Blank values are dropped unless keepEmpty is set. */
function readForm(form, { keepEmpty = false } = {}) {
  const out = {};
  for (const [key, raw] of new FormData(form).entries()) {
    const value = typeof raw === 'string' ? raw.trim() : raw;
    if (value === '' && !keepEmpty) continue;
    out[key] = value === '' ? null : value;
  }
  return out;
}

/** Disable a button and show a spinner while an async action runs. */
async function withBusy(button, action) {
  if (button) { button.disabled = true; button.classList.add('loading'); }
  try { return await action(); }
  finally { if (button) { button.disabled = false; button.classList.remove('loading'); } }
}

/** Modal form: resolves to the entered values, or null when cancelled. */
/** `validate(values)` may return {field: message} to keep the dialog open; `perform(values)` may return an
 *  error message (string) to keep it open and show the failure inside the dialog. */
function dialog({ title, message, fields = [], confirmLabel = 'Confirm', danger = false, cancelLabel = 'Cancel', extra = null, validate = null, perform = null }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => { if (settled) return; settled = true; resolve(value); };
    const errorBox = h('div', { class: 'banner rejected hidden', role: 'alert' });
    const form = h('form', { class: 'form', novalidate: true },
      message ? h('p', { class: 'muted', text: message }) : null, extra, fields.map(fieldNode), errorBox);
    const confirm = h('button', { type: 'button', class: `btn ${danger ? 'btn-danger' : 'btn-primary'}`, text: confirmLabel });
    const cancel = h('button', { type: 'button', class: 'btn btn-secondary', text: cancelLabel });

    const submit = async () => {
      clearFieldErrors(form);
      hideBanner(errorBox);
      const values = {};
      let firstBad = null;
      for (const f of fields) {
        const control = form.elements[f.name];
        if (f.type === 'checkbox') { values[f.name] = control.checked; continue; }
        const value = control.value.trim();
        values[f.name] = value;
        let problem = null;
        if (f.required && !value) problem = `${f.label} is required`;
        else if (f.minLength && value && value.length < f.minLength) problem = `${f.label} must be at least ${f.minLength} characters`;
        if (problem) { setFieldError(form, f.name, problem); firstBad = firstBad || control; }
      }
      if (firstBad) { firstBad.focus(); return; }
      if (validate) {
        const problems = validate(values) || {};
        const names = Object.keys(problems);
        if (names.length) {
          names.forEach((name) => setFieldError(form, name, problems[name]));
          form.elements[names[0]].focus();
          return;
        }
      }
      if (perform) {
        const problem = await withBusy(confirm, () => perform(values));
        if (problem) { showBanner(errorBox, problem); return; }
      }
      finish(values);
      closeModal();
    };
    confirm.addEventListener('click', submit);
    cancel.addEventListener('click', () => closeModal());
    form.addEventListener('submit', (e) => { e.preventDefault(); submit(); });
    openModal({ title, body: form, footer: [cancel, confirm], onClose: () => finish(null) });
  });
}

const confirmDialog = (opts) => dialog(opts).then((values) => values !== null);

/* ── presentation helpers ── */
const prettify = (value) => String(value ?? '').replace(/_/g, ' ');
const badge = (label, kind) => h('span', { class: `badge badge-${String(kind ?? label).replace(/\s+/g, '_')}`, text: prettify(label) });
const initials = (name) => (name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((s) => s[0]).join('').toUpperCase();

function avatar(name, photoUrl, size = 'sm') {
  const box = h('div', { class: `avatar-${size}`, 'aria-hidden': 'true' });
  if (photoUrl) {
    const img = h('img', { src: photoUrl, alt: '', loading: 'lazy' });
    img.addEventListener('error', () => { img.remove(); box.textContent = initials(name); });
    box.append(img);
  } else box.textContent = initials(name);
  return box;
}

const fmtDate = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' }) : '—');
const fmtDateTime = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }) : '—');
const dash = (value) => (value === null || value === undefined || value === '' ? '—' : value);
const fileHref = (id) => `/api/files/${id}`;
const linkTo = (path, label, cls = 'btn btn-secondary btn-sm') => h('a', { class: cls, href: `#${path}`, text: label });
const fileLink = (id, label, cls = 'btn btn-secondary btn-sm') => h('a', { class: cls, href: fileHref(id), target: '_blank', rel: 'noopener', text: label });

function emptyState(title, text, action) {
  return h('div', { class: 'empty-card' }, h('strong', { text: title }), h('span', { text }), action || null);
}

function skeleton(rows = 3) {
  return h('div', { 'aria-busy': 'true', 'aria-label': 'Loading' }, Array.from({ length: rows }, () => h('div', { class: 'skeleton block' })));
}

function working(title, text) {
  return h('div', { class: 'working', role: 'status' }, h('div', { class: 'spinner', 'aria-hidden': 'true' }), h('strong', { text: title }), h('span', { text }));
}

/* ── tables, pagination and paged lists ── */
function renderTable({ columns, rows }) {
  return h('div', { class: 'table-wrap' }, h('table', null,
    h('thead', null, h('tr', null, columns.map((c) => h('th', { scope: 'col', text: c.label })))),
    h('tbody', null, rows.map((row) => h('tr', null, columns.map((c) => h('td', { class: c.class }, c.cell(row))))))));
}

function pagerNode({ total, limit, offset }, go_) {
  if (total <= limit) return h('div');
  const from = offset + 1;
  const to = Math.min(offset + limit, total);
  const prev = h('button', { class: 'btn btn-secondary btn-sm', type: 'button', text: 'Previous', on: { click: () => go_(Math.max(0, offset - limit)) } });
  const next = h('button', { class: 'btn btn-secondary btn-sm', type: 'button', text: 'Next', on: { click: () => go_(offset + limit) } });
  prev.disabled = offset <= 0;
  next.disabled = offset + limit >= total;
  return h('nav', { class: 'pagination', 'aria-label': 'Pagination' }, prev, h('span', { text: `${from}–${to} of ${total}` }), next);
}

/** A paginated list region: loads, renders, re-renders on page change. Returns {reload}. */
function pagedList({ target, pager, load, render, empty, limit = PAGE_SIZE }) {
  let offset = 0;
  const navId = state.navId;
  async function reload(to = offset) {
    offset = to;
    if (pager) clear(pager);
    clear(target).append(skeleton());
    try {
      const data = await load(limit, offset);
      if (navId !== state.navId) return;
      clear(target);
      if (!data.items.length && offset > 0) return reload(Math.max(0, offset - limit));
      if (!data.items.length) append(target, [empty]);
      else append(target, [render(data.items)]);
      if (pager) append(clear(pager), [pagerNode({ total: data.total, limit, offset }, reload)]);
    } catch (err) {
      if (navId !== state.navId) return;
      clear(target).append(h('div', { class: 'banner rejected', role: 'alert' }, err.message,
        h('div', { class: 'row' }, h('button', { class: 'btn btn-secondary btn-sm', type: 'button', text: 'Try again', on: { click: () => reload() } }))));
    }
  }
  reload(0);
  return { reload };
}

/* ── polling (cancelled automatically when the route changes) ── */
function poll(task, ms = POLL_MS) {
  let stopped = false;
  let timer = null;
  const tick = async () => {
    if (stopped) return;
    let done = false;
    try { done = await task(); } catch (_) { /* keep polling through blips */ }
    if (!done && !stopped) timer = setTimeout(tick, ms);
  };
  timer = setTimeout(tick, ms);
  const stop = () => { stopped = true; clearTimeout(timer); };
  state.cleanups.push(stop);
  return stop;
}

function onLeave(fn) { state.cleanups.push(fn); }

/* ── file upload with progress ── */
function wireUpload({ input, progressSlot, path, onDone, accept, maxMB }) {
  input.addEventListener('change', async () => {
    const file = input.files[0];
    input.value = '';
    if (!file) return;
    if (maxMB && file.size > maxMB * 1024 * 1024) return toast(`That file is larger than ${maxMB} MB.`, 'error');
    if (accept && !accept.some((type) => file.type === type)) return toast('That file type is not allowed.', 'error');
    const bar = h('i');
    const label = h('div', { class: 'progress-label', text: `Uploading ${file.name}…` });
    const box = h('div', null, h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100' }, bar), label);
    append(clear(progressSlot), [box]);
    try {
      const result = await uploadFile(path, file, (fraction) => { bar.style.width = `${Math.round(fraction * 100)}%`; });
      clear(progressSlot);
      await onDone(result);
    } catch (err) {
      clear(progressSlot);
      toast(err.message, 'error');
    }
  });
}

/* ═══════════════ 5. Router ═══════════════ */

const ROUTES = [];
const PUBLIC_PATHS = new Set(['/login', '/register']);
const HOME = { patient: '/patient', doctor: '/doctor', admin: '/admin' };

/** route(pattern, {template, title, roles, verifiedOnly, render}). Role checks here are UI convenience;
 *  the backend enforces every permission. */
function route(pattern, config) {
  const names = [];
  const source = pattern.replace(/:([a-z_]+)/g, (_, name) => { names.push(name); return '([^/]+)'; });
  ROUTES.push({ pattern, regex: new RegExp(`^${source}$`), names, ...config });
}

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, queryString = ''] = raw.split('?');
  return { path: path || '/', query: new URLSearchParams(queryString) };
}

const currentPath = () => parseHash().path;

function go(path) {
  const target = `#${path}`;
  if (location.hash === target) handleRoute();
  else location.hash = target;
}

function homePath() {
  const user = state.user;
  if (!user) return '/login';
  if (user.role === 'doctor' && user.doctor_status !== 'verified') return '/doctor/verification';
  return HOME[user.role] || '/login';
}
const goHome = () => go(homePath());

function navItems() {
  const user = state.user;
  if (user.role === 'patient') {
    return [['/patient', 'Dashboard'], ['/patient/doctors', 'Doctors'], ['/patient/appointments', 'Appointments'],
      ['/patient/prescriptions', 'Prescriptions'], ['/patient/consents', 'Consents'], ['/patient/report', 'Report'],
      ['/patient/profile', 'Profile']];
  }
  if (user.role === 'doctor') {
    if (user.doctor_status !== 'verified') return [['/doctor/verification', 'Verification'], ['/doctor/profile', 'Profile']];
    return [['/doctor', 'Dashboard'], ['/doctor/patients', 'Patients'], ['/doctor/consult/new', 'New consult'],
      ['/doctor/history', 'History'], ['/doctor/verification', 'License'], ['/doctor/profile', 'Profile']];
  }
  return [['/admin', 'Overview'], ['/admin/verification', 'Verification'], ['/admin/doctors', 'Doctors'],
    ['/admin/reports', 'Reports'], ['/admin/audit', 'Audit log']];
}

function renderNav() {
  const navbar = $('#navbar');
  if (!state.user) { toggle(navbar, false); return; }
  toggle(navbar, true);
  const path = currentPath();
  const root = HOME[state.user.role];
  const links = clear($('#nav-links'));
  for (const [href, label] of navItems()) {
    const active = path === href || (href !== root && path.startsWith(`${href}/`));
    links.append(h('a', { href: `#${href}`, text: label, 'aria-current': active ? 'page' : null }));
  }
  $('#nav-user-name').textContent = state.user.full_name || '';
  const chip = clear($('#nav-avatar'));
  if (state.user.profile_photo_url) chip.append(h('img', { src: state.user.profile_photo_url, alt: '' }));
  else chip.textContent = initials(state.user.full_name);
}

function runCleanups() {
  const pending = state.cleanups.splice(0);
  for (const fn of pending) {
    try { fn(); } catch (_) { /* a failing cleanup must not block navigation */ }
  }
}

function renderTemplate(id) {
  const template = document.getElementById(id);
  clear(APP).append(template.content.cloneNode(true));
}

function showRouteError(error) {
  clear(APP).append(h('div', { class: 'page narrow' },
    h('div', { class: 'banner rejected', role: 'alert' }, error.message || 'This page could not be loaded.',
      h('div', { class: 'row' },
        h('button', { class: 'btn btn-secondary btn-sm', type: 'button', text: 'Try again', on: { click: () => handleRoute() } }),
        h('a', { class: 'btn btn-ghost btn-sm', href: '#/', text: 'Home' })))));
}

async function handleRoute() {
  const navId = ++state.navId;
  runCleanups();
  closeModal();
  const { path, query } = parseHash();

  if (!state.user && !PUBLIC_PATHS.has(path)) {
    try { state.user = await apiGet('/api/auth/me', { keepSession: true }); } catch (_) { go('/login'); return; }
    if (navId !== state.navId) return;
  }
  if (state.user && state.user.role === 'doctor' && state.user.doctor_status !== 'verified' && !PUBLIC_PATHS.has(path)) {
    await refreshUser(); // an administrator may have approved this doctor since the last check
    if (navId !== state.navId) return;
  }
  if (state.user && (PUBLIC_PATHS.has(path) || path === '/')) { goHome(); return; }
  if (!state.user && path === '/') { go('/login'); return; }

  let match = null;
  let params = {};
  for (const candidate of ROUTES) {
    const found = candidate.regex.exec(path);
    if (found) {
      match = candidate;
      params = Object.fromEntries(candidate.names.map((name, i) => [name, decodeURIComponent(found[i + 1])]));
      break;
    }
  }

  renderNav();
  if (!match) {
    renderTemplate('view-not-found');
    document.title = 'Not found · ClinicalScribe';
    return;
  }
  if (match.roles && state.user && !match.roles.includes(state.user.role)) {
    toast('You do not have access to that page.', 'error');
    goHome();
    return;
  }
  if (match.verifiedOnly && state.user?.role === 'doctor' && state.user.doctor_status !== 'verified') {
    go('/doctor/verification');
    return;
  }

  renderTemplate(match.template);
  document.title = `${match.title} · ClinicalScribe`;
  window.scrollTo(0, 0);
  APP.focus({ preventScroll: true });
  const context = { params, query, isCurrent: () => navId === state.navId };
  try {
    await match.render(context);
  } catch (error) {
    if (context.isCurrent() && error.status !== 401) showRouteError(error);
  }
}

/* ═══════════════ 6. Auth views ═══════════════ */

const EMAIL_PATTERN = /^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$/;

route('/login', {
  template: 'view-login',
  title: 'Sign in',
  render() {
    const form = $('#login-form');
    const banner = $('#login-error');
    $('#login-email').focus();
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearFieldErrors(form);
      hideBanner(banner);
      const values = readForm(form);
      let invalid = false;
      if (!values.email || !EMAIL_PATTERN.test(values.email)) { setFieldError(form, 'email', 'Enter a valid email address'); invalid = true; }
      if (!values.password) { setFieldError(form, 'password', 'Enter your password'); invalid = true; }
      if (invalid) return;
      await withBusy($('#login-submit'), async () => {
        try {
          state.user = await apiPost('/api/auth/login', { email: values.email, password: values.password }, { keepSession: true });
          await refreshUser();
          toast('Signed in', 'success');
          goHome();
        } catch (err) {
          showBanner(banner, err.message);
        }
      });
    });
  },
});

route('/register', {
  template: 'view-register',
  title: 'Create account',
  render() {
    const form = $('#register-form');
    const banner = $('#register-error');
    const patientFields = $('#patient-fields');
    const doctorFields = $('#doctor-fields');
    const buttons = $$('.role-btn', $('#role-toggle'));
    doctorFields.disabled = true; // disabled fieldsets are not submitted, so only the chosen role's fields are sent

    function chooseRole(role) {
      form.elements.role.value = role;
      buttons.forEach((b) => {
        const on = b.dataset.role === role;
        b.classList.toggle('active', on);
        b.setAttribute('aria-checked', String(on));
      });
      toggle(patientFields, role === 'patient');
      toggle(doctorFields, role === 'doctor');
      patientFields.disabled = role !== 'patient';
      doctorFields.disabled = role !== 'doctor';
    }
    buttons.forEach((b) => b.addEventListener('click', () => chooseRole(b.dataset.role)));
    toggle(doctorFields, false);

    function validate(values) {
      const problems = {};
      if (!values.full_name) problems.full_name = 'Enter your full name';
      if (!values.email || !EMAIL_PATTERN.test(values.email)) problems.email = 'Enter a valid email address';
      const pw = values.password || '';
      if (pw.length < 8 || pw.length > 128) problems.password = 'Use 8 to 128 characters';
      else if (!/[a-zA-Z]/.test(pw) || !/\d/.test(pw)) problems.password = 'Include at least one letter and one digit';
      if (values.role === 'doctor') {
        for (const [key, label] of [['reg_number', 'registration number'], ['council', 'issuing council'], ['specialization', 'specialization']]) {
          if (!values[key]) problems[key] = `Enter your ${label}`;
        }
        const year = Number(values.reg_year);
        const max = new Date().getFullYear();
        if (!Number.isInteger(year) || year < 1950 || year > max) problems.reg_year = `Enter a year between 1950 and ${max}`;
      }
      if (values.dob && new Date(values.dob) > new Date()) problems.dob = 'Date of birth cannot be in the future';
      return problems;
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearFieldErrors(form);
      hideBanner(banner);
      const values = readForm(form);
      const problems = validate(values);
      const names = Object.keys(problems);
      if (names.length) {
        names.forEach((name) => setFieldError(form, name, problems[name]));
        form.elements[names[0]].focus();
        return;
      }
      if (values.reg_year) values.reg_year = Number(values.reg_year);
      await withBusy($('#register-submit'), async () => {
        try {
          state.user = await apiPost('/api/auth/register', values, { keepSession: true });
          await refreshUser();
          toast(values.role === 'doctor' ? 'Account created. Upload your license next.' : 'Account created', 'success');
          goHome();
        } catch (err) {
          applyServerErrors(form, err, banner);
        }
      });
    });
  },
});

async function logout() {
  try { await apiPost('/api/auth/logout', null, { keepSession: true }); } catch (_) { /* clear locally anyway */ }
  state.user = null;
  renderNav();
  go('/login');
}

/* ═══════════════ 7. Patient views ═══════════════ */

const PATIENT = ['patient'];

function statTile(label, value, foot, href) {
  const children = [h('span', { class: 'mono', text: label }), h('div', { class: 'stat-value', text: String(value) }), h('div', { class: 'stat-foot', text: foot })];
  return href ? h('a', { class: 'stat', href: `#${href}` }, children) : h('div', { class: 'stat' }, children);
}

function localInputToIso(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

function defaultAppointmentTime() {
  const date = new Date(Date.now() + 24 * 3600 * 1000);
  date.setMinutes(0, 0, 0);
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:00`;
}

/** Book an appointment. `doctor` pre-selects a doctor; otherwise the patient picks from the directory. */
async function bookAppointment(doctor = null) {
  let doctors = doctor ? [doctor] : (await apiGet('/api/doctors?limit=100')).items;
  if (!doctors.length) { toast('No verified doctors are available yet.', 'error'); return false; }
  const fields = [
    ...(doctor ? [] : [{ name: 'doctor_id', label: 'Doctor', type: 'select', required: true,
      options: doctors.map((d) => ({ value: d.id, label: `${d.full_name} – ${d.specialization}` })) }]),
    { name: 'scheduled_at', label: 'Date and time', type: 'datetime-local', required: true, value: defaultAppointmentTime() },
    { name: 'reason_for_visit', label: 'Reason for visit', type: 'textarea', optional: true, rows: 3, maxLength: 1000 },
    { name: 'grant_consent', label: 'Also let this doctor see my records so they can document my visit', type: 'checkbox', value: true,
      help: 'You can revoke access at any time under Consents.' },
  ];
  let booked = false;
  await dialog({
    title: doctor ? `Book with ${doctor.full_name}` : 'Book an appointment', fields, confirmLabel: 'Book appointment',
    validate: (v) => {
      const iso = localInputToIso(v.scheduled_at);
      return !iso || new Date(iso) < new Date() ? { scheduled_at: 'Choose a date and time in the future' } : {};
    },
    perform: async (v) => {
      const doctorId = doctor ? doctor.id : v.doctor_id;
      try {
        await apiPost('/api/appointments', { doctor_id: doctorId, scheduled_at: localInputToIso(v.scheduled_at), reason_for_visit: v.reason_for_visit || null });
        if (v.grant_consent) {
          try { await apiPost('/api/consents', { doctor_id: doctorId }); } catch (err) { if (err.status !== 409) throw err; }
        }
        booked = true;
        toast('Appointment requested. The doctor will confirm it.', 'success');
        return null;
      } catch (err) { return err.message; }
    },
  });
  return booked;
}

route('/patient', {
  template: 'view-patient-dashboard',
  title: 'Dashboard',
  roles: PATIENT,
  async render(ctx) {
    setText('name', (state.user.full_name || '').split(' ')[0]);
    fill('stats', skeleton(1));
    const [appointments, prescriptions, consents] = await Promise.all([
      apiGet('/api/appointments?limit=100'), apiGet('/api/prescriptions?limit=1'), apiGet('/api/consents?limit=100'),
    ]);
    if (!ctx.isCurrent()) return;

    const upcoming = appointments.items
      .filter((a) => ['requested', 'confirmed'].includes(a.status) && new Date(a.scheduled_at) >= new Date(Date.now() - 3600000))
      .sort((a, b) => new Date(a.scheduled_at) - new Date(b.scheduled_at));
    const activeConsents = consents.items.filter((c) => !c.revoked_at);
    fill('stats',
      statTile('Upcoming', upcoming.length, 'open appointments', '/patient/appointments'),
      statTile('Prescriptions', prescriptions.total, 'approved by your doctors', '/patient/prescriptions'),
      statTile('Access', activeConsents.length, 'doctors with consent', '/patient/consents'));

    const next = upcoming[0];
    fill('next-appointment', next
      ? h('div', null,
        h('div', { class: 'list-item' },
          h('div', { class: 'meta' }, h('strong', { text: next.doctor_name || 'Doctor' }), h('span', { class: 'small', text: fmtDateTime(next.scheduled_at) })),
          badge(next.status)),
        next.reason_for_visit ? h('p', { class: 'small', text: `Reason: ${next.reason_for_visit}` }) : null)
      : emptyState('No upcoming appointment', 'Book a visit with a verified doctor.', linkTo('/patient/doctors', 'Find a doctor', 'btn btn-primary btn-sm')));

    const latest = prescriptions.items[0];
    fill('latest-prescription', latest
      ? h('div', null,
        h('div', { class: 'list-item' },
          h('div', { class: 'meta' }, h('strong', { text: latest.doctor_name || 'Doctor' }),
            h('span', { class: 'small', text: `${fmtDate(latest.approved_at)} · ${latest.approval_code || ''}` })),
          badge('approved')),
        h('p', { text: (latest.diagnosis || []).join(', ') || 'No diagnosis recorded' }),
        h('div', { class: 'row gap mt' }, linkTo(`/patient/prescriptions/${latest.id}`, 'View', 'btn btn-primary btn-sm'),
          latest.docx_file_id ? fileLink(latest.docx_file_id, 'Download .docx') : null))
      : emptyState('No prescriptions yet', 'Approved prescriptions from your doctors appear here.'));

    fill('consent-summary', activeConsents.length
      ? h('div', null, activeConsents.map((c) => h('div', { class: 'list-item' }, h('strong', { text: c.doctor_name }), h('span', { class: 'small', text: `since ${fmtDate(c.granted_at)}` }))))
      : emptyState('Nobody has access', 'Doctors only see your records after you grant consent.'));
  },
});

route('/patient/doctors', {
  template: 'view-patient-doctors',
  title: 'Doctors',
  roles: PATIENT,
  async render() {
    const form = $('#doctor-search');
    let filters = { q: '', specialization: '' };
    let consented = new Set((await apiGet('/api/consents?limit=100')).items.filter((c) => !c.revoked_at).map((c) => c.doctor_id));

    function card(doctor) {
      const consentSlot = h('span');
      const draw = () => {
        const isGranted = consented.has(doctor.id);
        clear(consentSlot);
        if (isGranted) consentSlot.append(badge('access granted', 'ok'));
        else {
          consentSlot.append(h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Grant access', on: { click: async (e) => {
            await withBusy(e.currentTarget, async () => {
              try { await apiPost('/api/consents', { doctor_id: doctor.id }); consented.add(doctor.id); toast(`${doctor.full_name} can now see your records.`, 'success'); draw(); } catch (err) { toast(err.message, 'error'); }
            });
          } } }));
        }
      };
      draw();
      return h('article', { class: 'card' },
        h('div', { class: 'person' }, avatar(doctor.full_name, doctor.profile_photo_url),
          h('div', null, h('strong', { text: doctor.full_name }), h('div', { class: 'small', text: doctor.specialization }))),
        h('div', { class: 'row gap mt wrap' }, badge('verified doctor', 'verified')),
        doctor.clinic_name ? h('p', { class: 'small mt', text: [doctor.clinic_name, doctor.clinic_address].filter(Boolean).join(', ') }) : null,
        h('div', { class: 'row gap mt wrap' },
          h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Book appointment', on: { click: async () => {
            const done = await bookAppointment(doctor);
            if (done) { consented = new Set((await apiGet('/api/consents?limit=100')).items.filter((c) => !c.revoked_at).map((c) => c.doctor_id)); draw(); }
          } } }), consentSlot));
    }

    const list = pagedList({
      target: slot('list'), pager: slot('pager'), limit: 12,
      load: (limit, offset) => apiGet(`/api/doctors?q=${enc(filters.q)}&specialization=${enc(filters.specialization)}&limit=${limit}&offset=${offset}`),
      render: (items) => h('div', { class: 'cards-grid' }, items.map(card)),
      empty: emptyState('No doctors found', 'Try a different name or specialization.'),
    });
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      filters = { q: form.elements.q.value.trim(), specialization: form.elements.specialization.value.trim() };
      list.reload(0);
    });
  },
});

route('/patient/appointments', {
  template: 'view-patient-appointments',
  title: 'Appointments',
  roles: PATIENT,
  render() {
    const list = pagedList({
      target: slot('list'), pager: slot('pager'),
      load: (limit, offset) => apiGet(`/api/appointments?limit=${limit}&offset=${offset}`),
      empty: emptyState('No appointments yet', 'Book a visit with a verified doctor.', linkTo('/patient/doctors', 'Find a doctor', 'btn btn-primary btn-sm')),
      render: (items) => renderTable({
        rows: items,
        columns: [
          { label: 'Doctor', cell: (a) => h('strong', { text: a.doctor_name || '—' }) },
          { label: 'When', cell: (a) => fmtDateTime(a.scheduled_at), class: 'nowrap' },
          { label: 'Reason', cell: (a) => dash(a.reason_for_visit) },
          { label: 'Status', cell: (a) => badge(a.status) },
          { label: '', cell: (a) => (['requested', 'confirmed'].includes(a.status)
            ? h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Cancel', on: { click: async (e) => {
              if (!(await confirmDialog({ title: 'Cancel appointment?', message: `Cancel your appointment with ${a.doctor_name} on ${fmtDateTime(a.scheduled_at)}?`, confirmLabel: 'Cancel appointment', cancelLabel: 'Keep it', danger: true }))) return;
              await withBusy(e.currentTarget, async () => {
                try { await apiPatch(`/api/appointments/${a.id}`, { status: 'cancelled' }); toast('Appointment cancelled', 'success'); list.reload(); } catch (err) { toast(err.message, 'error'); }
              });
            } } })
            : null) },
        ],
      }),
    });
    $('#btn-book').addEventListener('click', async () => { if (await bookAppointment()) list.reload(0); });
  },
});

route('/patient/prescriptions', {
  template: 'view-patient-prescriptions',
  title: 'Prescriptions',
  roles: PATIENT,
  render() {
    pagedList({
      target: slot('list'), pager: slot('pager'),
      load: (limit, offset) => apiGet(`/api/prescriptions?limit=${limit}&offset=${offset}`),
      empty: emptyState('No approved prescriptions yet', 'When a doctor approves a prescription for you it appears here.'),
      render: (items) => h('div', { class: 'cards-grid' }, items.map((rx) => h('article', { class: 'card' },
        h('div', { class: 'card-head' }, h('strong', { text: rx.doctor_name || 'Doctor' }), badge(`v${rx.version}`, 'info')),
        h('p', { class: 'small', text: `${fmtDate(rx.approved_at)} · ${rx.approval_code || ''}` }),
        h('p', { class: 'mt', text: (rx.diagnosis || []).join(', ') || 'No diagnosis recorded' }),
        h('p', { class: 'small', text: `${(rx.medications || []).length} medication(s)` }),
        h('div', { class: 'row gap mt wrap' }, linkTo(`/patient/prescriptions/${rx.id}`, 'View', 'btn btn-primary btn-sm'),
          rx.docx_file_id ? fileLink(rx.docx_file_id, 'Download .docx') : null)))),
    });
  },
});

/** Read-only prescription sections shared by the patient detail page and the admin report view. */
function prescriptionSections(content) {
  const list = (items) => h('ul', null, items.map((i) => h('li', { text: i })));
  const meds = content.medications || [];
  return [
    (content.diagnosis || []).length ? h('div', { class: 'rx-section' }, h('h3', { text: 'Diagnosis' }), list(content.diagnosis)) : null,
    meds.length ? h('div', { class: 'rx-section' }, h('h3', { text: 'Medications' }), renderTable({
      rows: meds,
      columns: [
        { label: 'Drug', cell: (m) => h('strong', { text: [m.drug_name, m.strength ? `(${m.strength})` : ''].filter(Boolean).join(' ') }) },
        { label: 'Dose', cell: (m) => dash(m.dose) }, { label: 'Route', cell: (m) => dash(m.route) },
        { label: 'Frequency', cell: (m) => dash(m.frequency) }, { label: 'Duration', cell: (m) => dash(m.duration) },
        { label: 'Instructions', cell: (m) => dash(m.instructions) },
      ],
    })) : null,
    (content.tests_advised || []).length ? h('div', { class: 'rx-section' }, h('h3', { text: 'Tests advised' }), list(content.tests_advised)) : null,
    (content.advice || []).length ? h('div', { class: 'rx-section' }, h('h3', { text: 'Advice' }), list(content.advice)) : null,
    content.follow_up ? h('div', { class: 'rx-section' }, h('h3', { text: 'Follow-up' }), h('p', { text: content.follow_up })) : null,
    content.notes ? h('div', { class: 'rx-section' }, h('h3', { text: 'Notes' }), h('p', { text: content.notes })) : null,
  ];
}

route('/patient/prescriptions/:id', {
  template: 'view-patient-prescription-detail',
  title: 'Prescription',
  roles: PATIENT,
  async render(ctx) {
    const rx = await apiGet(`/api/prescriptions/${enc(ctx.params.id)}`);
    const versions = await apiGet(`/api/consults/${enc(rx.consult_id)}/prescriptions`).catch(() => ({ items: [] }));
    if (!ctx.isCurrent()) return;

    setText('code', rx.approval_code || 'Prescription');
    setText('title', `Prescription from ${rx.doctor_name || 'your doctor'}`);
    setText('subtitle', `Approved ${fmtDateTime(rx.approved_at)} · version ${rx.version}`);
    fill('actions',
      rx.docx_file_id ? fileLink(rx.docx_file_id, 'Download .docx', 'btn btn-primary') : null,
      linkTo(`/patient/report?doctor=${enc(rx.doctor_id)}&consult=${enc(rx.consult_id)}`, 'Report a problem', 'btn btn-ghost'));
    fill('body', h('section', { class: 'card form-card' }, prescriptionSections(rx)),
      h('p', { class: 'small', text: 'This summary reflects what your doctor approved. It is not a substitute for talking to your doctor. For urgent symptoms contact your doctor or emergency services.' }));

    fill('versions', versions.items.length
      ? versions.items.map((v) => h('div', { class: 'version-row' },
        h('div', null, h('strong', { text: `v${v.version}` }), h('div', { class: 'small', text: `${v.approval_code || ''} · ${fmtDate(v.approved_at)}` })),
        v.status === 'approved' ? badge('current', 'approved') : badge('superseded')))
      : h('p', { class: 'small', text: 'Only this version exists.' }));

    const voice = slot('voice');
    if (CONFIG.voiceEnabled) {
      append(clear(voice), [h('div', { class: 'card-head' }, h('h2', { id: 'pd-voice', text: 'Listen' })), voicePanel(rx.id)]);
    } else toggle(voice, false);
  },
});

route('/patient/consents', {
  template: 'view-patient-consents',
  title: 'Consents',
  roles: PATIENT,
  render() {
    const list = pagedList({
      target: slot('list'), pager: slot('pager'),
      load: (limit, offset) => apiGet(`/api/consents?limit=${limit}&offset=${offset}`),
      empty: emptyState('Nobody has access', 'Grant a verified doctor access when you book or start treatment.'),
      render: (items) => renderTable({
        rows: items,
        columns: [
          { label: 'Doctor', cell: (c) => h('strong', { text: c.doctor_name || '—' }) },
          { label: 'Granted', cell: (c) => fmtDate(c.granted_at), class: 'nowrap' },
          { label: 'Status', cell: (c) => (c.revoked_at ? h('span', null, badge('revoked'), h('span', { class: 'sub', text: fmtDate(c.revoked_at) })) : badge('active', 'ok')) },
          { label: '', cell: (c) => (c.revoked_at ? null : h('button', { class: 'btn btn-secondary btn-sm', type: 'button', text: 'Revoke', on: { click: async (e) => {
            if (!(await confirmDialog({ title: 'Revoke access?', message: `${c.doctor_name} will immediately lose access to your records. Prescriptions you already received stay available to you.`, confirmLabel: 'Revoke access', danger: true }))) return;
            await withBusy(e.currentTarget, async () => {
              try { await apiDelete(`/api/consents/${c.id}`); toast('Access revoked', 'success'); list.reload(); } catch (err) { toast(err.message, 'error'); }
            });
          } } })) },
        ],
      }),
    });
    $('#btn-grant').addEventListener('click', async () => {
      const [doctors, consents] = await Promise.all([apiGet('/api/doctors?limit=100'), apiGet('/api/consents?limit=100')]);
      const active = new Set(consents.items.filter((c) => !c.revoked_at).map((c) => c.doctor_id));
      const options = doctors.items.filter((d) => !active.has(d.id)).map((d) => ({ value: d.id, label: `${d.full_name} – ${d.specialization}` }));
      if (!options.length) { toast('You have already granted access to every verified doctor.', 'info'); return; }
      let granted = false;
      await dialog({ title: 'Grant access', message: 'The doctor will be able to see your profile and work on consultations for you.', confirmLabel: 'Grant access',
        fields: [{ name: 'doctor_id', label: 'Doctor', type: 'select', required: true, options }],
        perform: async (v) => {
          try { await apiPost('/api/consents', { doctor_id: v.doctor_id }); granted = true; return null; } catch (err) { return err.message; }
        } });
      if (granted) { toast('Access granted', 'success'); list.reload(0); }
    });
  },
});

route('/patient/report', {
  template: 'view-patient-report',
  title: 'Report a doctor',
  roles: PATIENT,
  async render(ctx) {
    const form = $('#report-form');
    const banner = $('#report-error');
    const [doctors, consults] = await Promise.all([apiGet('/api/doctors?limit=100'), apiGet('/api/consults?limit=100')]);
    if (!ctx.isCurrent()) return;
    const doctorSelect = form.elements.doctor_id;
    append(clear(doctorSelect), [h('option', { value: '', text: 'Choose a doctor' }),
      doctors.items.map((d) => h('option', { value: d.id, text: `${d.full_name} – ${d.specialization}` }))]);
    const preselected = ctx.query.get('doctor');
    if (preselected && doctors.items.some((d) => d.id === preselected)) doctorSelect.value = preselected;

    function loadConsults() {
      const select = form.elements.consult_id;
      const mine = consults.items.filter((c) => c.doctor_id === doctorSelect.value);
      append(clear(select), [h('option', { value: '', text: 'Not about a specific consult' }),
        mine.map((c) => h('option', { value: c.id, text: `${fmtDate(c.created_at)} – ${prettify(c.status)}` }))]);
      const wanted = ctx.query.get('consult');
      if (wanted && mine.some((c) => c.id === wanted)) select.value = wanted;
    }
    doctorSelect.addEventListener('change', loadConsults);
    loadConsults();

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearFieldErrors(form);
      hideBanner(banner);
      const values = readForm(form);
      if (!values.doctor_id) return setFieldError(form, 'doctor_id', 'Choose a doctor');
      if (!values.reason || values.reason.length < 5) return setFieldError(form, 'reason', 'Describe the problem in at least 5 characters');
      await withBusy(form.querySelector('[type=submit]'), async () => {
        try {
          await apiPost('/api/reports', values);
          toast('Report submitted. An administrator will review it.', 'success');
          go('/patient');
        } catch (err) { applyServerErrors(form, err, banner); }
      });
    });
  },
});

/* Profile page shared by patients and doctors. */
async function renderProfile(ctx) {
  const form = $('#profile-form');
  const profile = await apiGet('/api/me/profile');
  if (!ctx.isCurrent()) return;
  const isPatient = state.user.role === 'patient';
  for (const el of $$('[data-patient-only]', form)) toggle(el, isPatient);

  const drawAvatar = () => append(clear(slot('avatar')), [avatar(state.user.full_name, state.user.profile_photo_url, 'lg')]);
  drawAvatar();
  form.elements.full_name.value = profile.full_name || '';
  $('#prof-email').value = profile.email || '';
  form.elements.phone.value = profile.phone || '';
  if (isPatient) {
    form.elements.dob.value = profile.dob || '';
    form.elements.gender.value = profile.gender || '';
    form.elements.blood_group.value = profile.blood_group || '';
    form.elements.allergies.value = profile.allergies || '';
  }
  wireUpload({
    input: $('#photo-input'), progressSlot: slot('photo-progress'), path: '/api/me/photo', maxMB: 5,
    accept: ['image/jpeg', 'image/png', 'image/webp'],
    onDone: async () => { await refreshUser(); drawAvatar(); toast('Photo updated', 'success'); },
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    clearFieldErrors(form);
    const values = readForm(form, { keepEmpty: true });
    if (!values.full_name) return toast('Name is required.', 'error');
    await withBusy(form.querySelector('[type=submit]'), async () => {
      try {
        await apiPatch('/api/me/profile', values);
        await refreshUser();
        toast('Profile saved', 'success');
      } catch (err) { applyServerErrors(form, err); }
    });
  });
}

route('/patient/profile', { template: 'view-patient-profile', title: 'Profile', roles: PATIENT, render: renderProfile });

/* ═══════════════ 8. Doctor views ═══════════════ */

const DOCTOR = ['doctor'];

/* ── license verification ── */
route('/doctor/verification', {
  template: 'view-doctor-verification',
  title: 'License verification',
  roles: DOCTOR,
  async render(ctx) {
    const form = $('#verification-form');
    const banner = $('#verification-banner');
    let loaded = null;

    function statusBanner(v) {
      const base = { pending: 'pending', verified: 'verified', rejected: 'rejected', suspended: 'suspended' }[v.status];
      banner.className = `banner ${base}`;
      clear(banner);
      if (v.status === 'pending') {
        banner.append(h('strong', { text: 'Pending review. ' }),
          v.license_file_id
            ? 'An administrator will check your license. This page updates automatically once you are verified.'
            : 'Upload your license certificate below so an administrator can review it.');
      } else if (v.status === 'verified') {
        banner.append(h('strong', { text: `Verified${v.verified_at ? ` on ${fmtDate(v.verified_at)}` : ''}. ` }), 'All clinical features are unlocked.',
          h('div', { class: 'row' }, linkTo('/doctor', 'Go to dashboard', 'btn btn-primary btn-sm')));
      } else if (v.status === 'rejected') {
        banner.append(h('strong', { text: 'Rejected.' }), h('div', { text: v.rejection_reason || '' }),
          h('div', { text: 'Fix your details, upload a new certificate, then resubmit.' }));
      } else {
        banner.append(h('strong', { text: 'Suspended.' }), h('div', { text: v.suspension_reason || '' }),
          h('div', { text: 'Contact an administrator to be reinstated.' }));
      }
    }

    async function load() {
      const v = await apiGet('/api/doctor/verification');
      if (!ctx.isCurrent()) return;
      loaded = v;
      statusBanner(v);
      const map = { reg_number: v.reg_number, council: v.council, reg_year: v.reg_year, specialization: v.specialization,
        clinic_name: v.clinic_name, clinic_address: v.clinic_address, clinic_phone: v.clinic_phone };
      for (const [name, value] of Object.entries(map)) form.elements[name].value = value ?? '';
      for (const control of Array.from(form.elements)) control.disabled = !v.editable;
      toggle(slot('form-actions'), v.editable);
      fill('license-status', v.license_file_id
        ? h('span', null, badge('on file', 'ok'), ' ', fileLink(v.license_file_id, 'View certificate', 'link-subtle'))
        : badge('not uploaded', 'pending'));
      fill('photo-status', v.profile_photo_file_id ? badge('on file', 'ok') : h('span', { class: 'small', text: 'No photo yet' }));
      fill('resubmit', v.status === 'rejected'
        ? h('button', { class: 'btn btn-primary', type: 'button', text: 'Resubmit for review', on: { click: async (e) => {
          await withBusy(e.currentTarget, async () => {
            try { await apiPost('/api/doctor/resubmit'); toast('Resubmitted. An administrator will review it.', 'success'); await refreshUser(); await load(); } catch (err) { toast(err.message, 'error'); }
          });
        } } })
        : null);
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearFieldErrors(form);
      const values = readForm(form);
      if (values.reg_year) values.reg_year = Number(values.reg_year);
      await withBusy($('#ver-save'), async () => {
        try { await apiPatch('/api/doctor/verification', values); toast('Details saved', 'success'); await load(); } catch (err) { applyServerErrors(form, err); }
      });
    });
    wireUpload({ input: $('#ver-license'), progressSlot: slot('license-progress'), path: '/api/doctor/license', maxMB: 10,
      accept: ['application/pdf', 'image/jpeg', 'image/png'],
      onDone: async () => { toast('Certificate uploaded', 'success'); await load(); } });
    wireUpload({ input: $('#ver-photo'), progressSlot: slot('photo-progress'), path: '/api/me/photo', maxMB: 5,
      accept: ['image/jpeg', 'image/png', 'image/webp'],
      onDone: async () => { toast('Photo uploaded', 'success'); await refreshUser(); await load(); } });

    await load();
    if (loaded && loaded.status === 'pending') {
      poll(async () => {
        const previous = state.user?.doctor_status;
        await refreshUser();
        if (state.user?.doctor_status !== previous) { toast(state.user.doctor_status === 'verified' ? 'You are verified.' : 'Your verification status changed.', 'success'); handleRoute(); return true; }
        return false;
      }, 15000);
    }
  },
});

/* ── dashboard ── */
const CONSULT_NEEDS = {
  failed: 'Something failed. Retry needed',
  soap_ready: 'SOAP note ready for review',
  prescription_ready: 'Prescription ready for review',
};

function consultNeedsAction(c) {
  if (c.status === 'failed' || c.transcription_status === 'failed') return 'Something failed. Retry needed';
  if (CONSULT_NEEDS[c.status]) return CONSULT_NEEDS[c.status];
  if (c.status === 'draft' && c.transcription_status === 'ready') return 'Transcript ready for review';
  return null;
}

function consultPath(c) {
  if (c.status === 'prescription_ready' && c.latest_prescription) return `/doctor/prescription/${c.latest_prescription.id}`;
  return `/doctor/consult/${c.id}`;
}

route('/doctor', {
  template: 'view-doctor-dashboard',
  title: 'Dashboard',
  roles: DOCTOR,
  verifiedOnly: true,
  async render(ctx) {
    setText('name', state.user.full_name.replace(/^Dr\.?\s+/i, '').split(' ')[0]);
    fill('stats', skeleton(1));
    const [dash_, consults] = await Promise.all([apiGet('/api/doctor/dashboard'), apiGet('/api/consults?limit=100')]);
    if (!ctx.isCurrent()) return;
    const needs = dash_.needs_action;
    fill('stats',
      statTile('Today', dash_.appointments_today.length, 'appointments'),
      statTile('To review', needs.transcript_ready + needs.soap_ready + needs.prescription_ready, 'transcripts, notes and prescriptions'),
      statTile('Patients', dash_.patients, 'with active consent', '/doctor/patients'));

    async function setAppointment(appointment, status, button) {
      await withBusy(button, async () => {
        try { await apiPatch(`/api/appointments/${appointment.id}`, { status }); toast(`Appointment ${status}`, 'success'); handleRoute(); } catch (err) { toast(err.message, 'error'); }
      });
    }
    fill('appointments', dash_.appointments_today.length
      ? dash_.appointments_today.map((a) => h('div', { class: 'list-item' },
        h('div', { class: 'meta' }, h('strong', { text: a.patient_name }), h('span', { class: 'small', text: `${fmtDateTime(a.scheduled_at)}${a.reason_for_visit ? ` · ${a.reason_for_visit}` : ''}` })),
        h('div', { class: 'cell-actions' }, badge(a.status),
          a.status === 'requested' ? h('button', { class: 'btn btn-secondary btn-sm', type: 'button', text: 'Confirm', on: { click: (e) => setAppointment(a, 'confirmed', e.currentTarget) } }) : null,
          a.status === 'confirmed' ? linkTo(`/doctor/consult/new?patient=${enc(a.patient_id)}&appointment=${enc(a.id)}`, 'Start consult', 'btn btn-primary btn-sm') : null,
          a.status === 'confirmed' ? h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Mark completed', on: { click: (e) => setAppointment(a, 'completed', e.currentTarget) } }) : null)))
      : emptyState('Nothing scheduled today', 'Confirmed appointments for today appear here.'));

    const actionable = consults.items.map((c) => [c, consultNeedsAction(c)]).filter(([, why]) => why);
    fill('needs-action', actionable.length
      ? actionable.map(([c, why]) => h('div', { class: 'list-item' },
        h('div', { class: 'meta' }, h('strong', { text: c.patient_name || 'Patient' }), h('span', { class: 'small', text: `${why} · ${fmtDate(c.created_at)}` })),
        linkTo(consultPath(c), 'Open', 'btn btn-primary btn-sm')))
      : emptyState('All caught up', 'Consults that need your review appear here.'));
  },
});

/* ── patients ── */
route('/doctor/patients', {
  template: 'view-doctor-patients',
  title: 'Patients',
  roles: DOCTOR,
  verifiedOnly: true,
  render() {
    pagedList({
      target: slot('list'), pager: slot('pager'),
      load: (limit, offset) => apiGet(`/api/doctor/patients?limit=${limit}&offset=${offset}`),
      empty: emptyState('No patients yet', 'Patients appear here after they grant you access, usually when they book an appointment.'),
      render: (items) => renderTable({
        rows: items,
        columns: [
          { label: 'Patient', cell: (p) => h('span', null, h('strong', { text: p.full_name }), h('span', { class: 'sub', text: p.email })) },
          { label: 'Allergies', cell: (p) => dash(p.allergies) },
          { label: 'Consent since', cell: (p) => fmtDate(p.consent_granted_at), class: 'nowrap' },
          { label: '', cell: (p) => h('div', { class: 'cell-actions' }, linkTo(`/doctor/patients/${p.id}`, 'Open'), linkTo(`/doctor/consult/new?patient=${enc(p.id)}`, 'New consult', 'btn btn-primary btn-sm')) },
        ],
      }),
    });
  },
});

function ageFrom(dob) {
  if (!dob) return null;
  const born = new Date(dob);
  const now = new Date();
  let age = now.getFullYear() - born.getFullYear();
  if (now.getMonth() < born.getMonth() || (now.getMonth() === born.getMonth() && now.getDate() < born.getDate())) age -= 1;
  return age;
}

route('/doctor/patients/:id', {
  template: 'view-doctor-patient-detail',
  title: 'Patient',
  roles: DOCTOR,
  verifiedOnly: true,
  async render(ctx) {
    const [patient, consults] = await Promise.all([
      apiGet(`/api/doctor/patients/${enc(ctx.params.id)}`), apiGet('/api/consults?limit=100'),
    ]);
    if (!ctx.isCurrent()) return;
    setText('name', patient.full_name);
    const age = ageFrom(patient.dob);
    setText('subtitle', [age !== null ? `${age} years` : null, patient.gender].filter(Boolean).join(' · ') || 'Demographics not provided');
    fill('actions', linkTo(`/doctor/consult/new?patient=${enc(patient.id)}`, 'Start consult', 'btn btn-primary'));
    fill('details', h('dl', { class: 'facts' },
      h('dt', { text: 'Email' }), h('dd', { text: patient.email }), h('dt', { text: 'Phone' }), h('dd', { text: dash(patient.phone) }),
      h('dt', { text: 'Date of birth' }), h('dd', { text: dash(patient.dob) }), h('dt', { text: 'Blood group' }), h('dd', { text: dash(patient.blood_group) }),
      h('dt', { text: 'Allergies' }), h('dd', null, patient.allergies ? badge(patient.allergies, 'high') : '—'),
      h('dt', { text: 'Consent granted' }), h('dd', { text: fmtDateTime(patient.consent_granted_at) })));
    const mine = consults.items.filter((c) => c.patient_id === patient.id);
    fill('consults', mine.length
      ? renderTable({ rows: mine, columns: [
        { label: 'Started', cell: (c) => fmtDateTime(c.created_at), class: 'nowrap' },
        { label: 'Status', cell: (c) => badge(c.status) },
        { label: 'Prescription', cell: (c) => (c.latest_prescription ? h('span', null, `v${c.latest_prescription.version} `, badge(c.latest_prescription.status)) : '—') },
        { label: '', cell: (c) => linkTo(consultPath(c), 'Open') },
      ] })
      : emptyState('No consults yet', 'Start one to record or paste a consultation.'));
  },
});

/* ── start a consult ── */
route('/doctor/consult/new', {
  template: 'view-doctor-consult-new',
  title: 'New consult',
  roles: DOCTOR,
  verifiedOnly: true,
  async render(ctx) {
    const form = $('#new-consult-form');
    const banner = $('#new-consult-error');
    const [patients, appointments] = await Promise.all([apiGet('/api/doctor/patients?limit=100'), apiGet('/api/appointments?limit=100')]);
    if (!ctx.isCurrent()) return;
    const patientSelect = form.elements.patient_id;
    if (!patients.items.length) {
      clear(form).append(emptyState('No patients yet', 'A patient must grant you access before you can start a consult.', linkTo('/doctor/patients', 'View patients', 'btn btn-secondary btn-sm')));
      return;
    }
    append(clear(patientSelect), [h('option', { value: '', text: 'Choose a patient' }), patients.items.map((p) => h('option', { value: p.id, text: p.full_name }))]);
    const wantedPatient = ctx.query.get('patient');
    if (wantedPatient && patients.items.some((p) => p.id === wantedPatient)) patientSelect.value = wantedPatient;

    function drawAppointments() {
      const select = form.elements.appointment_id;
      const mine = appointments.items.filter((a) => a.patient_id === patientSelect.value && ['requested', 'confirmed'].includes(a.status));
      append(clear(select), [h('option', { value: '', text: 'Not linked to an appointment' }),
        mine.map((a) => h('option', { value: a.id, text: `${fmtDateTime(a.scheduled_at)} – ${prettify(a.status)}` }))]);
      const wanted = ctx.query.get('appointment');
      if (wanted && mine.some((a) => a.id === wanted)) select.value = wanted;
    }
    patientSelect.addEventListener('change', drawAppointments);
    drawAppointments();

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      clearFieldErrors(form);
      hideBanner(banner);
      const values = readForm(form);
      if (!values.patient_id) return setFieldError(form, 'patient_id', 'Choose a patient');
      await withBusy(form.querySelector('[type=submit]'), async () => {
        try {
          const consult = await apiPost('/api/consults', { patient_id: values.patient_id, appointment_id: values.appointment_id || null });
          go(`/doctor/consult/${consult.id}`);
        } catch (err) { showBanner(banner, err.message); }
      });
    });
  },
});

/* ── consult workflow: transcript -> SOAP -> prescription ── */
const WORKING = ['uploaded', 'transcribing'];

function consultStep(c) {
  const soapApproved = c.soap && c.soap.status === 'approved';
  if (['soap_approved', 'prescription_generating', 'prescription_ready', 'completed'].includes(c.status)) return 'prescription';
  if (c.status === 'failed') {
    if (soapApproved) return 'prescription';
    if (c.soap || (c.transcript_text && c.transcription_status === 'ready')) return 'soap';
    return 'transcript';
  }
  if (['soap_generating', 'soap_ready'].includes(c.status)) return 'soap';
  return 'transcript';
}

function icd10Editor(initial) {
  let codes = (initial || []).map((c) => ({ ...c }));
  const chips = h('div', { class: 'chips', 'aria-live': 'polite' });
  const code = h('input', { class: 'input', placeholder: 'Code, e.g. G44.2', maxlength: '20', 'aria-label': 'ICD-10 code' });
  const description = h('input', { class: 'input', placeholder: 'Description', maxlength: '300', 'aria-label': 'ICD-10 description' });
  const draw = () => {
    clear(chips);
    if (!codes.length) chips.append(h('span', { class: 'small', text: 'No ICD-10 suggestions.' }));
    codes.forEach((c, i) => chips.append(h('span', { class: 'chip' }, h('strong', { text: c.code }), c.description || '',
      c.confidence != null ? h('span', { class: 'small', text: `${Math.round(c.confidence * 100)}%` }) : null,
      h('button', { type: 'button', 'aria-label': `Remove ${c.code}`, text: '×', on: { click: () => { codes.splice(i, 1); draw(); } } }))));
  };
  const add = () => {
    const value = code.value.trim();
    if (!value) return code.focus();
    codes.push({ code: value, description: description.value.trim(), confidence: null });
    code.value = '';
    description.value = '';
    draw();
  };
  draw();
  const node = h('div', { class: 'field' }, h('span', { class: 'label', text: 'ICD-10 suggestions' }), chips,
    h('div', { class: 'row gap wrap mt' }, code, description, h('button', { type: 'button', class: 'btn btn-secondary btn-sm', text: 'Add', on: { click: add } })));
  return { node, value: () => codes };
}

route('/doctor/consult/:id', {
  template: 'view-doctor-consult-detail',
  title: 'Consult',
  roles: DOCTOR,
  verifiedOnly: true,
  async render(ctx) {
    const id = ctx.params.id;
    let stopPolling = () => {};

    async function load() { return apiGet(`/api/consults/${enc(id)}`); }

    function watch(isBusy) {
      stopPolling();
      stopPolling = poll(async () => {
        const fresh = await load();
        if (!ctx.isCurrent()) return true;
        if (isBusy(fresh)) return false;
        await draw(fresh);
        return true;
      });
    }

    async function retry(button) {
      await withBusy(button, async () => {
        try { toast((await apiPost(`/api/consults/${enc(id)}/retry`)).detail, 'info'); await draw(); } catch (err) { toast(err.message, 'error'); }
      });
    }

    function transcriptStep(c) {
      const body = [];
      if (c.transcription_status === 'failed') {
        body.push(h('div', { class: 'banner rejected', role: 'alert' }, c.error_message || 'Transcription failed.',
          c.audio_file_id ? h('div', { class: 'row' }, h('button', { class: 'btn btn-secondary btn-sm', type: 'button', text: 'Retry transcription', on: { click: (e) => retry(e.currentTarget) } })) : null));
      }
      const panels = {};
      const tabs = h('div', { class: 'tabs', role: 'tablist', 'aria-label': 'How to add the transcript' });
      const select = (name) => {
        for (const [key, panel] of Object.entries(panels)) toggle(panel, key === name);
        $$('.tab', tabs).forEach((t) => t.setAttribute('aria-selected', String(t.dataset.tab === name)));
      };

      panels.record = h('div', { role: 'tabpanel' }, recorderPanel(id, () => draw()));

      const fileInput = h('input', { type: 'file', accept: 'audio/mpeg,audio/wav,audio/mp4,audio/webm,audio/ogg,.mp3,.wav,.m4a,.webm,.ogg', class: 'visually-hidden', id: 'audio-file' });
      const progress = h('div');
      panels.upload = h('div', { role: 'tabpanel', class: 'stack' },
        h('p', { class: 'small', text: `MP3, WAV, M4A, WebM or OGG, up to ${CONFIG.maxAudioMB} MB. If your recording uses another format, convert it first or record in the browser.` }),
        h('label', { class: 'btn btn-secondary file-button', for: 'audio-file' }, 'Choose audio file', fileInput), progress);
      wireUpload({ input: fileInput, progressSlot: progress, path: `/api/consults/${enc(id)}/audio`, maxMB: CONFIG.maxAudioMB, onDone: () => draw() });

      const paste = h('textarea', { class: 'transcript-edit', rows: '10', 'aria-label': 'Pasted transcript', placeholder: 'Paste the consultation transcript here…', maxlength: '100000' });
      panels.paste = h('div', { role: 'tabpanel', class: 'stack' }, paste,
        h('div', { class: 'row' }, h('button', { class: 'btn btn-primary', type: 'button', text: 'Use this transcript', on: { click: async (e) => {
          if (!paste.value.trim()) return toast('Paste a transcript first.', 'error');
          await withBusy(e.currentTarget, async () => {
            try { await apiPut(`/api/consults/${enc(id)}/transcript`, { transcript_text: paste.value }); await draw(); } catch (err) { toast(err.message, 'error'); }
          });
        } } })));

      for (const [key, label] of [['record', 'Record in browser'], ['upload', 'Upload audio'], ['paste', 'Paste transcript']]) {
        tabs.append(h('button', { class: 'tab', type: 'button', role: 'tab', dataset: { tab: key }, text: label, on: { click: () => select(key) } }));
      }
      select('record');
      body.push(h('section', { class: 'card form-card' }, h('div', { class: 'card-head' }, h('h2', { text: 'Add the consultation' })), tabs, Object.values(panels)));
      return body;
    }

    function transcriptEditor(c, { editable, withGenerate }) {
      const area = h('textarea', { class: 'transcript-edit', 'aria-label': 'Transcript', maxlength: '100000' });
      area.value = c.transcript_text || '';
      area.readOnly = !editable;
      const save = async (button) => withBusy(button, async () => {
        try { await apiPut(`/api/consults/${enc(id)}/transcript`, { transcript_text: area.value }); toast('Transcript saved', 'success'); return true; } catch (err) { toast(err.message, 'error'); return false; }
      });
      const actions = h('div', { class: 'row gap wrap mt' });
      if (editable) {
        actions.append(h('button', { class: 'btn btn-secondary', type: 'button', text: 'Save transcript', on: { click: (e) => save(e.currentTarget) } }));
      }
      if (withGenerate) {
        actions.append(h('button', { class: 'btn btn-primary', type: 'button', text: c.soap ? 'Regenerate SOAP note' : 'Generate SOAP note', on: { click: async (e) => {
          if (c.soap && !(await confirmDialog({ title: 'Regenerate the SOAP note?', message: 'This replaces the current SOAP draft, including any edits you made.', confirmLabel: 'Regenerate', danger: true }))) return;
          await withBusy(e.currentTarget, async () => {
            try {
              if (editable && area.value !== (c.transcript_text || '')) await apiPut(`/api/consults/${enc(id)}/transcript`, { transcript_text: area.value });
              await apiPost(`/api/consults/${enc(id)}/soap/generate`);
              await draw();
            } catch (err) { toast(err.message, 'error'); }
          });
        } } }));
      }
      const note = c.transcript_edited ? h('span', { class: 'badge badge-info', text: 'edited' }) : null;
      return h('section', { class: 'card form-card' }, h('div', { class: 'card-head' }, h('h2', { text: 'Transcript' }), note), area, actions);
    }

    function soapEditor(c) {
      const soap = c.soap || {};
      const fields = {};
      const make = (key, label) => {
        const area = h('textarea', { rows: '4', maxlength: '10000', id: `soap-${key}` });
        area.value = soap[key] || '';
        fields[key] = area;
        return h('div', { class: 'field' }, h('label', { for: `soap-${key}`, text: label }), area);
      };
      const icd = icd10Editor(soap.icd10_codes);
      const payload = () => ({
        subjective: fields.subjective.value, objective: fields.objective.value,
        assessment: fields.assessment.value, plan: fields.plan.value, icd10_codes: icd.value(),
      });
      const actions = h('div', { class: 'row gap wrap' },
        h('button', { class: 'btn btn-secondary', type: 'button', text: 'Save draft', on: { click: (e) => withBusy(e.currentTarget, async () => {
          try { await apiPut(`/api/consults/${enc(id)}/soap`, payload()); toast('SOAP draft saved', 'success'); } catch (err) { toast(err.message, 'error'); }
        }) } }),
        h('button', { class: 'btn btn-primary', type: 'button', text: 'Approve SOAP and draft prescription', on: { click: async (e) => {
          const ok = await confirmDialog({ title: 'Approve this SOAP note?', confirmLabel: 'Approve and draft',
            message: 'An AI agent will draft a prescription from the approved note. Nothing is issued: you review, edit and approve it yourself, and the patient sees it only after you approve.' });
          if (!ok) return;
          await withBusy(e.currentTarget, async () => {
            try { await apiPut(`/api/consults/${enc(id)}/soap`, payload()); await apiPost(`/api/consults/${enc(id)}/soap/approve`); await draw(); } catch (err) { toast(err.message, 'error'); }
          });
        } } }));
      return h('section', { class: 'card form-card stack' }, h('div', { class: 'card-head' }, h('h2', { text: 'SOAP note' }), badge('draft: review required')),
        h('p', { class: 'small', text: 'Drafted by AI from the transcript. Check every section against what was said; it may contain mistakes.' }),
        make('subjective', 'Subjective'), make('objective', 'Objective'), make('assessment', 'Assessment'), make('plan', 'Plan'), icd.node, actions);
    }

    async function prescriptionStep(c) {
      const body = [];
      const latest = c.latest_prescription;
      if (c.status === 'prescription_generating') {
        body.push(h('section', { class: 'card' }, working('Drafting the prescription', 'The agent is reading the approved SOAP note. This usually takes under a minute.')));
        watch((f) => f.status === 'prescription_generating');
      } else if (latest) {
        const versions = (await apiGet(`/api/consults/${enc(id)}/prescriptions`)).items;
        const draftOpen = latest.status === 'draft';
        body.push(h('section', { class: 'card form-card stack' },
          h('div', { class: 'card-head' }, h('h2', { text: 'Prescription' }), badge(latest.status)),
          h('p', { text: draftOpen ? 'A draft prescription is ready. Review it, resolve any safety flags and approve it to issue it to the patient.'
            : latest.status === 'approved' ? 'This prescription is approved and visible to the patient.' : 'This version is no longer current.' }),
          h('div', { class: 'row gap wrap' }, linkTo(`/doctor/prescription/${latest.id}`, draftOpen ? 'Review the draft' : 'Open prescription', 'btn btn-primary')),
          h('div', null, h('h3', { class: 'label', text: 'Versions' }),
            versions.map((v) => h('div', { class: 'version-row' },
              h('div', null, h('strong', { text: `v${v.version}` }), h('div', { class: 'small', text: `${v.approval_code || 'not approved'} · ${fmtDateTime(v.created_at)}` })),
              h('div', { class: 'cell-actions' }, badge(v.status), linkTo(`/doctor/prescription/${v.id}`, 'Open')))))));
      } else if (c.status === 'soap_approved') {
        body.push(h('section', { class: 'card form-card stack' }, h('div', { class: 'card-head' }, h('h2', { text: 'Prescription' })),
          h('p', { text: 'There is no draft prescription. Ask the agent to draft a new one.' }),
          draftAgainButton()));
      } else if (c.status === 'failed') {
        body.push(h('section', { class: 'card form-card stack' }, h('div', { class: 'card-head' }, h('h2', { text: 'Prescription' })),
          h('p', { text: 'Drafting the prescription did not finish.' }),
          h('div', { class: 'row gap' }, h('button', { class: 'btn btn-primary', type: 'button', text: 'Retry', on: { click: (e) => retry(e.currentTarget) } }))));
      }
      return body;
    }

    function draftAgainButton() {
      return h('div', { class: 'row' }, h('button', { class: 'btn btn-primary', type: 'button', text: 'Draft a prescription', on: { click: async (e) => {
        const values = await dialog({ title: 'Draft a prescription', confirmLabel: 'Draft', fields: [
          { name: 'note', label: 'Note for the agent', type: 'textarea', optional: true, rows: 3, maxLength: 1000, help: 'It cannot override the safety rules; it may only clarify what you decided.' }] });
        if (!values) return;
        await withBusy(e.currentTarget, async () => {
          try { await apiPost(`/api/consults/${enc(id)}/prescriptions/regenerate`, { note: values.note || null }); await draw(); } catch (err) { toast(err.message, 'error'); }
        });
      } } }));
    }

    async function draw(known) {
      stopPolling();
      const c = known || (await load());
      if (!ctx.isCurrent()) return;

      setText('meta', `Consult · started ${fmtDateTime(c.created_at)}`);
      setText('title', c.patient_name || 'Consult');
      fill('actions', linkTo(`/doctor/patients/${c.patient_id}`, 'Patient record', 'btn btn-ghost'));
      const step = consultStep(c);
      const order = ['transcript', 'soap', 'prescription'];
      $$('.step', slot('stepper')).forEach((el) => {
        const index = order.indexOf(el.dataset.step);
        el.classList.toggle('active', el.dataset.step === step);
        el.classList.toggle('done', index < order.indexOf(step));
        if (el.dataset.step === step) el.setAttribute('aria-current', 'step'); else el.removeAttribute('aria-current');
      });

      const banners = [];
      if (c.transcription_provider === 'mock' && step === 'transcript') {
        banners.push(h('div', { class: 'banner pending' }, 'Demo transcription is on (STT_PROVIDER=mock). Audio is not transcribed; a fixed sample consultation is returned instead.'));
      }
      if (c.llm_provider === 'mock') {
        banners.push(h('div', { class: 'banner pending' }, 'Demo AI is on (LLM_PROVIDER=mock). The SOAP note and prescription are canned samples that do not depend on your transcript.'));
      }
      if (c.llm_provider === 'unconfigured') {
        banners.push(h('div', { class: 'banner warning' }, 'No AI model is configured. SOAP and prescription generation will fail until LLM_API_KEY is set.'));
      }
      if (c.status === 'failed' && c.transcription_status !== 'failed') {
        banners.push(h('div', { class: 'banner rejected', role: 'alert' }, c.error_message || 'Processing failed.',
          step !== 'prescription' ? h('div', { class: 'row' }, h('button', { class: 'btn btn-secondary btn-sm', type: 'button', text: 'Retry', on: { click: (e) => retry(e.currentTarget) } })) : null));
      }
      fill('banners', ...banners);

      const content = [];
      if (WORKING.includes(c.transcription_status)) {
        content.push(h('section', { class: 'card' }, working('Transcribing the audio', 'This usually takes under a minute. You can leave this page; the transcript will be here when you return.')));
        watch((f) => WORKING.includes(f.transcription_status));
      } else if (step === 'transcript') {
        if (c.transcription_status === 'ready' && c.transcript_text) content.push(transcriptEditor(c, { editable: true, withGenerate: true }));
        else content.push(...transcriptStep(c));
      } else if (step === 'soap') {
        if (c.status === 'soap_generating') {
          content.push(h('section', { class: 'card' }, working('Writing the SOAP note', 'The AI is drafting from your transcript.')));
          watch((f) => f.status === 'soap_generating');
        } else if (c.status === 'soap_ready') {
          content.push(soapEditor(c), h('details', { class: 'card' }, h('summary', { class: 'strong', text: 'Transcript (edit and regenerate the SOAP note)' }),
            transcriptEditor(c, { editable: true, withGenerate: true })));
        } else content.push(transcriptEditor(c, { editable: true, withGenerate: true }));
      } else {
        content.push(...(await prescriptionStep(c)));
      }
      fill('content', ...content);
    }

    await draw();
  },
});

/* ── prescription review ── */
const SEVERITY_ORDER = ['high', 'medium', 'low'];
const splitLines = (text) => text.split('\n').map((s) => s.trim()).filter(Boolean);
const orNull = (value) => { const v = String(value ?? '').trim(); return v === '' ? null : v; };

/** Highlight each quote inside the transcript. Returns the <mark> element created for every quote index. */
function highlightTranscript(container, transcript, quotes) {
  clear(container);
  const text = transcript || '';
  const lower = text.toLowerCase();
  const ranges = [];
  quotes.forEach((quote, index) => {
    const needle = (quote || '').trim().toLowerCase();
    if (!needle) return;
    const start = lower.indexOf(needle);
    if (start >= 0) ranges.push({ start, end: start + needle.length, index });
  });
  ranges.sort((a, b) => a.start - b.start);
  const marks = {};
  let cursor = 0;
  for (const range of ranges) {
    if (range.start < cursor) continue; // overlapping quotes: keep the first
    container.append(document.createTextNode(text.slice(cursor, range.start)));
    const mark = h('mark', { class: 'quote', text: text.slice(range.start, range.end) });
    marks[range.index] = mark;
    container.append(mark);
    cursor = range.end;
  }
  container.append(document.createTextNode(text.slice(cursor)));
  if (!text) container.textContent = 'No transcript available.';
  return marks;
}

route('/doctor/prescription/:id', {
  template: 'view-doctor-prescription-review',
  title: 'Prescription review',
  roles: DOCTOR,
  verifiedOnly: true,
  async render(ctx) {
    const rx = await apiGet(`/api/prescriptions/${enc(ctx.params.id)}`);
    const [consult, versions] = await Promise.all([
      apiGet(`/api/consults/${enc(rx.consult_id)}`), apiGet(`/api/consults/${enc(rx.consult_id)}/prescriptions`),
    ]);
    if (!ctx.isCurrent()) return;

    const isDraft = rx.status === 'draft';
    const isApproved = rx.status === 'approved';
    const latestVersion = Math.max(...versions.items.map((v) => v.version));
    const editable = (isDraft || isApproved) && rx.version === latestVersion;
    const flags = rx.safety_flags || [];
    const highFlags = flags.filter((f) => f.severity === 'high');
    const content = rx.content || { diagnosis: [], icd10: [], medications: [], tests_advised: [], advice: [], follow_up: null, notes: null };
    let dirty = false;

    setText('meta', `Version ${rx.version} · ${prettify(rx.status)}`);
    setText('title', `Prescription for ${rx.patient_name || 'patient'}`);
    setText('subtitle', isApproved ? `Approved ${fmtDateTime(rx.approved_at)} · ${rx.approval_code}` : 'AI-drafted. Nothing is issued until you approve it.');
    fill('top-actions', linkTo(`/doctor/consult/${rx.consult_id}`, 'Back to consult', 'btn btn-ghost'),
      rx.docx_file_id ? fileLink(rx.docx_file_id, isApproved ? 'Download approved .docx' : 'Download draft .docx', 'btn btn-secondary') : null);

    const banners = [];
    if (consult.llm_provider === 'mock') banners.push(h('div', { class: 'banner pending' }, 'Demo AI is on: this prescription is a canned sample that may not match the transcript.'));
    if (isApproved) banners.push(h('div', { class: 'banner verified' }, 'This version is approved and cannot be changed. Editing below saves a new draft version that you review and approve again; the patient keeps seeing this one until then.'));
    if (!isDraft && !isApproved) banners.push(h('div', { class: 'banner rejected' }, `This version is ${rx.status}.`, h('div', { class: 'row' }, linkTo(`/doctor/consult/${rx.consult_id}`, 'Open the current version', 'btn btn-secondary btn-sm'))));
    if (isDraft && highFlags.length) banners.push(h('div', { class: 'banner rejected', role: 'alert' }, `${highFlags.length} high-risk flag(s) need your review before you can approve.`));
    fill('banners', ...banners);

    /* editor */
    const medBoxes = [];
    const diagnosis = h('textarea', { rows: '3', 'aria-label': 'Diagnosis, one per line' });
    diagnosis.value = (content.diagnosis || []).join('\n');
    const tests = h('textarea', { rows: '3', 'aria-label': 'Tests advised, one per line' });
    tests.value = (content.tests_advised || []).join('\n');
    const advice = h('textarea', { rows: '4', 'aria-label': 'Advice, one per line' });
    advice.value = (content.advice || []).join('\n');
    const followUp = h('input', { maxlength: '500', 'aria-label': 'Follow-up' });
    followUp.value = content.follow_up || '';
    const notes = h('textarea', { rows: '2', maxlength: '2000', 'aria-label': 'Notes' });
    notes.value = content.notes || '';

    const medContainer = h('div', { class: 'stack' });
    const transcriptBox = slot('transcript');
    let marks = {};
    function medCard(med, index) {
      const inputs = {};
      const ref = `medications[${index}]`;
      const mine = flags.filter((f) => (f.field_ref || '').startsWith(ref));
      const worst = SEVERITY_ORDER.find((sev) => mine.some((f) => f.severity === sev));
      const field = (key, label, extra = {}) => {
        const input = h('input', { id: `med-${index}-${key}`, maxlength: extra.maxlength || '200' });
        input.value = med[key] ?? '';
        inputs[key] = input;
        return h('div', { class: 'field' }, h('label', { for: `med-${index}-${key}`, text: label }), input);
      };
      const card = h('div', { class: `med-card${worst ? ` flagged-${worst}` : ''}`, dataset: { index: String(index) } },
        h('div', { class: 'med-head' }, h('strong', { text: `Medication ${index + 1}` }),
          h('div', { class: 'cell-actions' },
            worst ? badge(`${worst} flag`, worst) : null,
            med.source_quote ? h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Show in transcript', on: { click: () => {
              const mark = marks[index];
              if (mark) { mark.scrollIntoView({ block: 'center', behavior: 'smooth' }); mark.classList.add('active'); setTimeout(() => mark.classList.remove('active'), 2500); } else toast('That quote is not in the transcript text.', 'info');
            } } }) : null,
            editable ? h('button', { class: 'btn btn-ghost btn-sm', type: 'button', text: 'Remove', on: { click: () => { medBoxes.splice(medBoxes.indexOf(entry), 1); drawMeds(); markDirty(); } } }) : null)),
        h('div', { class: 'grid-2' }, field('drug_name', 'Drug'), field('strength', 'Strength', { maxlength: '100' })),
        h('div', { class: 'grid-3' }, field('dose', 'Dose', { maxlength: '100' }), field('route', 'Route', { maxlength: '100' }), field('duration', 'Duration', { maxlength: '100' })),
        field('frequency', 'Frequency'), field('instructions', 'Instructions', { maxlength: '500' }),
        med.source_quote ? h('p', { class: 'med-quote', text: `Source: “${med.source_quote}”` }) : null,
        mine.map((f) => h('div', { class: `flag flag-${f.severity}` }, h('span', { text: f.message }))));
      const entry = { card, inputs, source_quote: med.source_quote ?? null };
      return entry;
    }
    function drawMeds() {
      clear(medContainer);
      medBoxes.forEach((entry, i) => { entry.card.querySelector('strong').textContent = `Medication ${i + 1}`; });
      append(medContainer, medBoxes.map((e) => e.card));
      if (!medBoxes.length) medContainer.append(h('p', { class: 'small', text: 'No medications in this prescription.' }));
      if (editable) medContainer.append(h('div', { class: 'row' }, h('button', { class: 'btn btn-secondary btn-sm', type: 'button', text: 'Add medication', on: { click: () => {
        medBoxes.push(medCard({ drug_name: '' }, medBoxes.length));
        drawMeds();
        markDirty();
        medBoxes[medBoxes.length - 1].inputs.drug_name.focus();
      } } })));
    }
    (content.medications || []).forEach((m, i) => medBoxes.push(medCard(m, i)));
    drawMeds();

    function collect() {
      return {
        diagnosis: splitLines(diagnosis.value),
        icd10: content.icd10 || [],
        medications: medBoxes.map((e) => ({
          drug_name: e.inputs.drug_name.value.trim(), strength: orNull(e.inputs.strength.value), dose: orNull(e.inputs.dose.value),
          route: orNull(e.inputs.route.value), frequency: orNull(e.inputs.frequency.value), duration: orNull(e.inputs.duration.value),
          instructions: orNull(e.inputs.instructions.value), source_quote: e.source_quote,
        })).filter((m) => m.drug_name),
        tests_advised: splitLines(tests.value), advice: splitLines(advice.value),
        follow_up: orNull(followUp.value), notes: orNull(notes.value),
      };
    }

    const section = (label, node, id) => h('div', { class: 'field' }, h('label', { for: id || null, text: label }), node);
    diagnosis.id = 'rx-diagnosis'; tests.id = 'rx-tests'; advice.id = 'rx-advice'; followUp.id = 'rx-followup'; notes.id = 'rx-notes';
    const icdNode = (content.icd10 || []).length
      ? h('div', { class: 'field' }, h('span', { class: 'label', text: 'ICD-10 suggestions' }), h('div', { class: 'chips' }, content.icd10.map((c) => h('span', { class: 'chip' }, h('strong', { text: c.code }), c.description || ''))))
      : null;
    const editor = h('div', { class: 'stack' }, section('Diagnosis (one per line)', diagnosis, 'rx-diagnosis'), icdNode,
      h('div', null, h('h3', { class: 'label', text: 'Medications' }), medContainer),
      section('Tests advised (one per line)', tests, 'rx-tests'), section('Advice (one per line)', advice, 'rx-advice'),
      section('Follow-up', followUp, 'rx-followup'), section('Notes', notes, 'rx-notes'));
    fill('editor', editor);
    if (!editable) $$('input, textarea, select', editor).forEach((el) => { el.disabled = true; });

    /* flags */
    const flagList = h('div', { class: 'flags' });
    if (!flags.length) flagList.append(h('p', { class: 'small', text: 'No flags. The draft matches the transcript and the patient’s allergy list.' }));
    for (const severity of SEVERITY_ORDER) {
      for (const f of flags.filter((x) => x.severity === severity)) {
        flagList.append(h('div', { class: `flag flag-${severity}` },
          h('div', { class: 'row gap wrap' }, badge(severity), f.acknowledged ? badge('acknowledged', 'ok') : null),
          h('span', { text: f.message }), f.field_ref ? h('span', { class: 'flag-ref', text: f.field_ref }) : null));
      }
    }
    fill('flags', flagList, h('p', { class: 'small mt', text: 'Flags are evidence for you. They never change the prescription on their own.' }));

    /* transcript with highlighted quotes */
    marks = highlightTranscript(transcriptBox, consult.transcript_text, (content.medications || []).map((m) => m.source_quote));

    /* version history */
    fill('versions', versions.items.map((v) => h('div', { class: 'version-row' },
      h('div', null, h('strong', { text: `v${v.version}` }), h('div', { class: 'small', text: `${v.approval_code || 'not approved'} · ${fmtDate(v.created_at)}` })),
      h('div', { class: 'cell-actions' }, badge(v.status), v.id === rx.id ? h('span', { class: 'small', text: 'viewing' }) : linkTo(`/doctor/prescription/${v.id}`, 'Open')))));

    /* decision */
    const decision = [];
    const save = h('button', { class: 'btn btn-secondary', type: 'button', text: isApproved ? 'Save as new version' : 'Save edits' });
    save.disabled = true;
    const approve = h('button', { class: 'btn btn-primary', type: 'button', text: 'Approve and issue' });
    const ack = h('input', { type: 'checkbox', id: 'ack-high' });
    function refreshButtons() {
      save.disabled = !dirty;
      approve.disabled = dirty || (highFlags.length > 0 && !ack.checked);
    }
    function markDirty() { dirty = true; refreshButtons(); }
    editor.addEventListener('input', markDirty);

    save.addEventListener('click', () => withBusy(save, async () => {
      const body = collect();
      if (!body.medications.length && !(await confirmDialog({ title: 'Save with no medications?', message: 'The prescription has no medications. Save it anyway?', confirmLabel: 'Save anyway' }))) return;
      try {
        const next = await apiPut(`/api/prescriptions/${enc(rx.id)}`, { content: body, expected_version: rx.version });
        toast(`Saved as version ${next.version}`, 'success');
        go(`/doctor/prescription/${next.id}`);
      } catch (err) {
        toast(err.message, 'error');
        if (err.status === 409) handleRoute();
      }
    }));

    approve.addEventListener('click', async () => {
      if (dirty) return toast('Save your edits first.', 'error');
      const ok = await confirmDialog({ title: `Approve version ${rx.version}?`, confirmLabel: 'Approve and issue',
        message: `This finalises the document and makes it visible to ${rx.patient_name || 'the patient'}. An approved prescription cannot be edited; any later change becomes a new version.` });
      if (!ok) return;
      await withBusy(approve, async () => {
        try {
          await apiPost(`/api/prescriptions/${enc(rx.id)}/approve`, { acknowledged_flag_ids: highFlags.map((f) => f.id) });
          toast('Prescription approved and issued', 'success');
          handleRoute();
        } catch (err) { toast(err.message, 'error'); }
      });
    });

    const reject = h('button', { class: 'btn btn-danger', type: 'button', text: 'Reject or regenerate', on: { click: async () => {
      const values = await dialog({ title: 'Reject this draft', confirmLabel: 'Reject draft', danger: true,
        message: 'The draft is discarded. You can ask the agent to try again, optionally with a note.',
        fields: [{ name: 'regenerate', label: 'Ask the agent to draft a new version', type: 'checkbox', value: true },
          { name: 'note', label: 'Note for the agent', type: 'textarea', optional: true, rows: 3, maxLength: 1000 }] });
      if (!values) return;
      try {
        await apiPost(`/api/prescriptions/${enc(rx.id)}/reject`, { regenerate: values.regenerate, note: values.note || null });
        toast(values.regenerate ? 'Draft rejected. Drafting a new version…' : 'Draft rejected', 'success');
        go(`/doctor/consult/${rx.consult_id}`);
      } catch (err) { toast(err.message, 'error'); }
    } } });

    if (isDraft) {
      if (highFlags.length) {
        ack.addEventListener('change', refreshButtons);
        decision.push(h('label', { class: 'check', for: 'ack-high' }, ack, h('span', { text: 'I have reviewed the high-risk flags and take responsibility for this prescription.' })));
      }
      decision.push(h('div', { class: 'row gap wrap mt' }, approve, save, reject),
        h('p', { class: 'small', text: 'Approve is disabled while you have unsaved edits, and until every high-risk flag is acknowledged.' }));
    } else if (isApproved) {
      decision.push(h('dl', { class: 'facts' }, h('dt', { text: 'Approval code' }), h('dd', { text: rx.approval_code }),
        h('dt', { text: 'Approved' }), h('dd', { text: fmtDateTime(rx.approved_at) })),
      editable ? h('div', { class: 'row gap wrap mt' }, save, h('p', { class: 'small', text: 'Edit the fields above, then save to create a new version.' })) : null);
    } else {
      decision.push(h('p', { class: 'small', text: 'No actions are available for this version.' }));
    }
    fill('decision', ...decision);
    refreshButtons();
  },
});

/* ── history ── */
route('/doctor/history', {
  template: 'view-doctor-history',
  title: 'History',
  roles: DOCTOR,
  verifiedOnly: true,
  render() {
    pagedList({
      target: slot('list'), pager: slot('pager'),
      load: (limit, offset) => apiGet(`/api/consults?limit=${limit}&offset=${offset}`),
      empty: emptyState('No consults yet', 'Your consults and prescriptions appear here.', linkTo('/doctor/consult/new', 'Start a consult', 'btn btn-primary btn-sm')),
      render: (items) => renderTable({
        rows: items,
        columns: [
          { label: 'Patient', cell: (c) => h('strong', { text: c.patient_name || '—' }) },
          { label: 'Started', cell: (c) => fmtDateTime(c.created_at), class: 'nowrap' },
          { label: 'Status', cell: (c) => badge(c.status) },
          { label: 'Prescription', cell: (c) => (c.latest_prescription ? h('span', null, `v${c.latest_prescription.version} `, badge(c.latest_prescription.status)) : '—') },
          { label: '', cell: (c) => h('div', { class: 'cell-actions' }, linkTo(`/doctor/consult/${c.id}`, 'Consult'),
            c.latest_prescription ? linkTo(`/doctor/prescription/${c.latest_prescription.id}`, 'Prescription') : null) },
        ],
      }),
    });
  },
});

route('/doctor/profile', { template: 'view-doctor-profile', title: 'Profile', roles: DOCTOR, render: renderProfile });

/* ═══════════════ 9. Admin views ═══════════════ */

const ADMIN = ['admin'];

function evidenceBadges(d) {
  const score = d.name_match_score;
  return h('div', { class: 'chips' },
    d.registry_match === null ? badge('not checked', 'low')
      : d.registry_match ? badge('registry match', 'ok') : badge('no registry match', 'high'),
    d.registry_match && score !== null ? badge(`name ${Math.round(score)}%`, score >= 85 ? 'ok' : score >= 60 ? 'medium' : 'high') : null,
    d.duplicate_reg_flag ? badge('duplicate registration', 'high') : null,
    d.license_file_id ? null : badge('no certificate', 'pending'));
}

route('/admin', {
  template: 'view-admin-dashboard',
  title: 'Overview',
  roles: ADMIN,
  async render(ctx) {
    fill('stats', skeleton(1));
    const [stats, pending, reports] = await Promise.all([
      apiGet('/api/admin/stats'), apiGet('/api/admin/doctors?status=pending&limit=5'), apiGet('/api/admin/reports?state=open&limit=5'),
    ]);
    if (!ctx.isCurrent()) return;
    fill('stats',
      statTile('Pending licenses', stats.pending_licenses, 'awaiting review', '/admin/verification'),
      statTile('Verified doctors', stats.verified_doctors, 'can use clinical features', '/admin/doctors?status=verified'),
      statTile('Suspended', stats.suspended_doctors, 'doctors', '/admin/doctors?status=suspended'),
      statTile('Open reports', stats.open_reports, 'from patients', '/admin/reports'));
    fill('queue', pending.items.length
      ? pending.items.map((d) => h('div', { class: 'list-item' },
        h('div', { class: 'meta' }, h('strong', { text: d.full_name }), h('span', { class: 'small', text: `${d.council} · submitted ${fmtDate(d.submitted_at)}` })),
        linkTo(`/admin/verification/${d.user_id}`, 'Review', 'btn btn-primary btn-sm')))
      : emptyState('Queue is empty', 'No doctor is waiting for a license review.'));
    fill('reports', reports.items.length
      ? reports.items.map((r) => h('div', { class: 'list-item' },
        h('div', { class: 'meta' }, h('strong', { text: r.reason }), h('span', { class: 'small', text: `${r.reporter_name} about ${r.doctor_name}` })),
        linkTo(`/admin/reports/${r.id}`, 'Open', 'btn btn-secondary btn-sm')))
      : emptyState('No open reports', 'Patient reports appear here.'));
  },
});

route('/admin/verification', {
  template: 'view-admin-verification-queue',
  title: 'Verification queue',
  roles: ADMIN,
  render() {
    pagedList({
      target: slot('list'), pager: slot('pager'),
      load: (limit, offset) => apiGet(`/api/admin/doctors?status=pending&limit=${limit}&offset=${offset}`),
      empty: emptyState('Queue is empty', 'No doctor is waiting for a license review.'),
      render: (items) => renderTable({
        rows: items,
        columns: [
          { label: 'Doctor', cell: (d) => h('span', null, h('strong', { text: d.full_name }), h('span', { class: 'sub', text: d.email })) },
          { label: 'Registration', cell: (d) => h('span', null, d.reg_number, h('span', { class: 'sub', text: `${d.council} · ${d.reg_year}` })) },
          { label: 'Specialization', cell: (d) => d.specialization },
          { label: 'Submitted', cell: (d) => fmtDateTime(d.submitted_at), class: 'nowrap' },
          { label: 'Evidence', cell: evidenceBadges },
          { label: '', cell: (d) => linkTo(`/admin/verification/${d.user_id}`, 'Review', 'btn btn-primary btn-sm') },
        ],
      }),
    });
  },
});

/** Run an admin decision: confirm (with a reason when required), call the API, then reload. */
async function adminDecision({ doctorId, action, title, message, confirmLabel, danger, needsReason, done }) {
  let succeeded = false;
  await dialog({
    title, message, confirmLabel, danger,
    fields: needsReason ? [{ name: 'reason', label: 'Reason', type: 'textarea', required: true, minLength: 10, rows: 4, maxLength: 1000,
      help: 'At least 10 characters. The doctor can see this reason.' }] : [],
    perform: async (values) => {
      try {
        await apiPost(`/api/admin/doctors/${enc(doctorId)}/${action}`, needsReason ? { reason: values.reason } : null);
        succeeded = true;
        return null;
      } catch (err) { return err.message; }
    },
  });
  if (succeeded) toast(done, 'success');
  return succeeded;
}

route('/admin/verification/:id', {
  template: 'view-admin-verification-detail',
  title: 'License review',
  roles: ADMIN,
  async render(ctx) {
    const d = await apiGet(`/api/admin/doctors/${enc(ctx.params.id)}`);
    if (!ctx.isCurrent()) return;
    setText('name', d.full_name);
    const sub = slot('subtitle');
    append(clear(sub), [`${d.email} · `, badge(d.status)]);

    const banner = slot('banner');
    const messages = {
      pending: ['pending', 'Waiting for your decision. Registry results below are evidence only.'],
      verified: ['verified', `Verified${d.verified_at ? ` on ${fmtDate(d.verified_at)}` : ''}.`],
      rejected: ['rejected', `Rejected: ${d.rejection_reason || ''}`],
      suspended: ['suspended', `Suspended: ${d.suspension_reason || ''}`],
    };
    banner.className = `banner ${messages[d.status][0]}`;
    banner.textContent = messages[d.status][1];

    const reload = () => handleRoute();
    const actions = [];
    if (d.status === 'pending') {
      const approve = h('button', { class: 'btn btn-primary', type: 'button', text: 'Approve', on: { click: async () => {
        if (await adminDecision({ doctorId: d.user_id, action: 'approve', title: 'Approve this doctor?', confirmLabel: 'Approve',
          message: `${d.full_name} will be able to use every clinical feature immediately.`, done: 'Doctor approved' })) go('/admin/verification');
      } } });
      if (!d.license_file_id) { approve.disabled = true; approve.title = 'The doctor has not uploaded a license certificate yet'; }
      actions.push(approve, h('button', { class: 'btn btn-danger', type: 'button', text: 'Reject', on: { click: async () => {
        if (await adminDecision({ doctorId: d.user_id, action: 'reject', title: 'Reject this submission', confirmLabel: 'Reject', danger: true, needsReason: true,
          message: 'The doctor will see your reason and can fix their details and resubmit.', done: 'Doctor rejected' })) go('/admin/verification');
      } } }));
    } else if (d.status === 'verified') {
      actions.push(h('button', { class: 'btn btn-danger', type: 'button', text: 'Suspend', on: { click: async () => {
        if (await adminDecision({ doctorId: d.user_id, action: 'suspend', title: 'Suspend this doctor', confirmLabel: 'Suspend', danger: true, needsReason: true,
          message: 'Suspension takes effect on their very next request; active sessions lose clinical access immediately.', done: 'Doctor suspended' })) reload();
      } } }));
    } else if (d.status === 'suspended') {
      actions.push(h('button', { class: 'btn btn-primary', type: 'button', text: 'Reinstate', on: { click: async () => {
        if (await adminDecision({ doctorId: d.user_id, action: 'reinstate', title: 'Reinstate this doctor', confirmLabel: 'Reinstate', needsReason: true,
          message: 'The doctor regains clinical access.', done: 'Doctor reinstated' })) reload();
      } } }));
    }
    fill('actions', ...actions);

    fill('details', ...[
      ['Registration number', d.reg_number], ['Council', d.council], ['Registration year', d.reg_year], ['Specialization', d.specialization],
      ['Clinic', d.clinic_name], ['Clinic address', d.clinic_address], ['Clinic phone', d.clinic_phone],
      ['Submitted', fmtDateTime(d.submitted_at)], ['Verified', d.verified_at ? fmtDateTime(d.verified_at) : null],
    ].flatMap(([label, value]) => [h('dt', { text: label }), h('dd', { text: dash(value) })]));

    const latest = d.checks[0];
    fill('evidence', h('p', { class: 'small', text: d.registry_source }),
      latest ? h('div', { class: 'stack' }, evidenceBadges(d),
        h('p', { text: latest.notes || '' }),
        latest.matched_record ? h('dl', { class: 'facts' },
          h('dt', { text: 'Registry name' }), h('dd', { text: latest.matched_record.full_name }),
          h('dt', { text: 'Registry year' }), h('dd', { text: String(latest.matched_record.reg_year) }),
          h('dt', { text: 'Registration' }), h('dd', null, latest.matched_record.is_active ? badge('active', 'ok') : badge('inactive', 'high')))
          : null,
        h('p', { class: 'small', text: `Checked ${fmtDateTime(latest.checked_at)}${d.checks.length > 1 ? ` · ${d.checks.length} checks in total` : ''}` }))
        : h('p', { class: 'small', text: 'No registry check has run yet.' }));

    fill('documents',
      d.license_file_id
        ? h('div', { class: 'stack' }, h('p', { text: 'The certificate opens in a new tab through a permission-checked, audit-logged link.' }), fileLink(d.license_file_id, 'Open license certificate', 'btn btn-secondary'))
        : emptyState('No certificate uploaded', 'The doctor has not uploaded a license certificate yet.'),
      d.profile_photo_url ? h('div', { class: 'profile-row mt' }, avatar(d.full_name, d.profile_photo_url, 'lg'), h('span', { class: 'small', text: 'Profile photo' })) : null);

    fill('history', ...d.events.map((e) => h('li', null,
      h('strong', { text: prettify(e.action) }), e.from_status || e.to_status ? ` (${e.from_status || 'new'} → ${e.to_status || ''})` : '',
      h('div', { class: 'when', text: `${fmtDateTime(e.created_at)}${e.actor_id ? '' : ' · system'}` }),
      e.reason ? h('div', { class: 'small', text: e.reason }) : null)));
  },
});

route('/admin/doctors', {
  template: 'view-admin-doctors',
  title: 'Doctors',
  roles: ADMIN,
  render(ctx) {
    const form = $('#doctors-filter');
    form.elements.status.value = ctx.query.get('status') || '';
    const filters = () => ({ status: form.elements.status.value, q: form.elements.q.value.trim() });
    const list = pagedList({
      target: slot('list'), pager: slot('pager'),
      load: (limit, offset) => {
        const f = filters();
        return apiGet(`/api/admin/doctors?status=${enc(f.status)}&q=${enc(f.q)}&limit=${limit}&offset=${offset}`);
      },
      empty: emptyState('No doctors match', 'Change the filter to see more.'),
      render: (items) => renderTable({
        rows: items,
        columns: [
          { label: 'Doctor', cell: (d) => h('span', null, h('strong', { text: d.full_name }), h('span', { class: 'sub', text: d.email })) },
          { label: 'Registration', cell: (d) => h('span', null, d.reg_number, h('span', { class: 'sub', text: d.council })) },
          { label: 'Specialization', cell: (d) => d.specialization },
          { label: 'Status', cell: (d) => badge(d.status) },
          { label: '', cell: (d) => h('div', { class: 'cell-actions' }, linkTo(`/admin/verification/${d.user_id}`, 'Details'),
            d.status === 'verified' ? h('button', { class: 'btn btn-danger btn-sm', type: 'button', text: 'Suspend', on: { click: async () => {
              if (await adminDecision({ doctorId: d.user_id, action: 'suspend', title: `Suspend ${d.full_name}`, confirmLabel: 'Suspend', danger: true, needsReason: true,
                message: 'Suspension takes effect immediately.', done: 'Doctor suspended' })) list.reload();
            } } }) : null,
            d.status === 'suspended' ? h('button', { class: 'btn btn-primary btn-sm', type: 'button', text: 'Reinstate', on: { click: async () => {
              if (await adminDecision({ doctorId: d.user_id, action: 'reinstate', title: `Reinstate ${d.full_name}`, confirmLabel: 'Reinstate', needsReason: true,
                message: 'The doctor regains clinical access.', done: 'Doctor reinstated' })) list.reload();
            } } }) : null) },
        ],
      }),
    });
    form.addEventListener('submit', (e) => { e.preventDefault(); list.reload(0); });
  },
});

route('/admin/reports', {
  template: 'view-admin-reports',
  title: 'Reports',
  roles: ADMIN,
  render() {
    const form = $('#reports-filter');
    const list = pagedList({
      target: slot('list'), pager: slot('pager'),
      load: (limit, offset) => apiGet(`/api/admin/reports?state=${enc(form.elements.state.value)}&limit=${limit}&offset=${offset}`),
      empty: emptyState('No reports', 'Nothing matches this filter.'),
      render: (items) => renderTable({
        rows: items,
        columns: [
          { label: 'Reason', cell: (r) => h('strong', { text: r.reason }) },
          { label: 'Reporter', cell: (r) => r.reporter_name },
          { label: 'Doctor', cell: (r) => r.doctor_name },
          { label: 'Filed', cell: (r) => fmtDate(r.created_at), class: 'nowrap' },
          { label: 'Status', cell: (r) => (r.resolved ? badge('resolved', 'ok') : badge('open', 'pending')) },
          { label: '', cell: (r) => linkTo(`/admin/reports/${r.id}`, 'Open') },
        ],
      }),
    });
    form.elements.state.addEventListener('change', () => list.reload(0));
    form.addEventListener('submit', (e) => e.preventDefault());
  },
});

route('/admin/reports/:id', {
  template: 'view-admin-report-detail',
  title: 'Report',
  roles: ADMIN,
  async render(ctx) {
    const report = await apiGet(`/api/admin/reports/${enc(ctx.params.id)}`);
    if (!ctx.isCurrent()) return;
    setText('title', report.reason);
    setText('subtitle', `Filed ${fmtDateTime(report.created_at)} by ${report.reporter_name} about ${report.doctor_name}`);
    fill('summary', h('dl', { class: 'facts' },
      h('dt', { text: 'Status' }), h('dd', null, report.resolved ? badge('resolved', 'ok') : badge('open', 'pending')),
      h('dt', { text: 'Details' }), h('dd', { text: dash(report.details) }),
      h('dt', { text: 'Doctor' }), h('dd', null, h('a', { href: `#/admin/verification/${report.doctor_id}`, text: report.doctor_name })),
      report.resolved ? [h('dt', { text: 'Resolution' }), h('dd', { text: dash(report.resolution_note) })] : null));

    if (!report.consult) {
      fill('consult', h('div', { class: 'card-head' }, h('h2', { text: 'Related consult' })), h('p', { class: 'small', text: 'This report does not reference a specific consult.' }));
    } else {
      const holder = h('div');
      const open = h('button', { class: 'btn btn-secondary', type: 'button', text: 'Open consult content', on: { click: async () => {
        if (!(await confirmDialog({ title: 'Open clinical content?', confirmLabel: 'Open and log access',
          message: 'You are about to view the transcript, SOAP note and prescriptions for this consult. Your access is recorded in the audit log with this report’s id.' }))) return;
        await withBusy(open, async () => {
          try {
            const data = await apiGet(`/api/admin/reports/${enc(report.id)}/consult`);
            append(clear(holder), [
              h('h3', { class: 'label', text: 'Transcript' }), h('div', { class: 'transcript', tabindex: '0', text: data.transcript || 'No transcript.' }),
              data.soap ? [h('h3', { class: 'label mt', text: 'SOAP note' }), h('dl', { class: 'facts' },
                ['subjective', 'objective', 'assessment', 'plan'].flatMap((k) => [h('dt', { text: prettify(k) }), h('dd', { text: dash(data.soap[k]) })]))] : null,
              data.prescriptions.map((p) => h('div', { class: 'mt' }, h('h3', { class: 'label', text: `Prescription v${p.version} (${p.status})` }), prescriptionSections(p.content || {}))),
            ]);
            open.remove();
          } catch (err) { toast(err.message, 'error'); }
        });
      } } });
      fill('consult', h('div', { class: 'card-head' }, h('h2', { text: 'Related consult' }), badge(report.consult.status)),
        h('p', { class: 'small', text: 'Clinical content is hidden by default. Opening it is audit-logged.' }), open, holder);
    }

    if (report.resolved) { toggle(slot('resolve'), false); return; }
    const note = h('textarea', { rows: '3', maxlength: '2000', id: 'resolution-note', 'aria-label': 'Resolution note' });
    const error = h('span', { class: 'field-error', role: 'alert' });
    fill('resolve', h('div', { class: 'card-head' }, h('h2', { text: 'Resolve this report' })),
      h('div', { class: 'field' }, h('label', { for: 'resolution-note', text: 'Resolution note' }), note, error),
      h('div', { class: 'row mt' }, h('button', { class: 'btn btn-primary', type: 'button', text: 'Mark resolved', on: { click: async (e) => {
        error.textContent = '';
        if (note.value.trim().length < 5) { error.textContent = 'Write at least 5 characters.'; note.focus(); return; }
        await withBusy(e.currentTarget, async () => {
          try { await apiPost(`/api/admin/reports/${enc(report.id)}/resolve`, { resolution_note: note.value.trim() }); toast('Report resolved', 'success'); handleRoute(); } catch (err) { toast(err.message, 'error'); }
        });
      } } })));
  },
});

route('/admin/audit', {
  template: 'view-admin-audit',
  title: 'Audit log',
  roles: ADMIN,
  render() {
    const form = $('#audit-filter');
    const query = (limit, offset) => {
      const params = new URLSearchParams({ limit, offset });
      for (const [key, value] of new FormData(form).entries()) if (String(value).trim()) params.set(key, String(value).trim());
      return params.toString();
    };
    const compact = (value) => {
      if (!value) return '';
      const text = JSON.stringify(value);
      return text.length > 220 ? `${text.slice(0, 220)}…` : text;
    };
    const list = pagedList({
      target: slot('list'), pager: slot('pager'), limit: 30,
      load: (limit, offset) => apiGet(`/api/admin/audit-logs?${query(limit, offset)}`),
      empty: emptyState('No matching entries', 'Adjust the filters.'),
      render: (items) => renderTable({
        rows: items,
        columns: [
          { label: 'Time', cell: (l) => fmtDateTime(l.created_at), class: 'nowrap' },
          { label: 'Actor', cell: (l) => h('span', null, dash(l.actor_role), l.actor_id ? h('span', { class: 'sub', text: l.actor_id.slice(0, 8) }) : null) },
          { label: 'Action', cell: (l) => h('span', { class: 'mono raw', text: l.action }) },
          { label: 'Resource', cell: (l) => h('span', null, dash(l.resource_type), l.resource_id ? h('span', { class: 'sub', text: l.resource_id.slice(0, 8) }) : null) },
          { label: 'IP', cell: (l) => dash(l.ip), class: 'nowrap' },
          { label: 'Details', cell: (l) => h('span', { class: 'json', text: compact(l.metadata) }) },
        ],
      }),
    });
    form.addEventListener('submit', (e) => { e.preventDefault(); list.reload(0); });
  },
});

/* ═══════════════ 10. Recorder module ═══════════════ */

const RECORDER_BYTES_PER_SECOND = 16000; // conservative estimate for compressed speech (about 128 kbps)
const RECORDER_MIME_CANDIDATES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];

const formatClock = (seconds) => `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
const audioExtension = (mime) => (mime.includes('ogg') ? 'ogg' : mime.includes('mp4') ? 'm4a' : 'webm');

/** In-browser consultation recorder: record, pause/resume, stop, preview, discard, upload.
 *  The microphone is requested only on the Start click and every track is stopped when finished. */
function recorderPanel(consultId, onUploaded) {
  const box = h('div', { class: 'recorder' });
  if (typeof MediaRecorder === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
    box.append(h('div', { class: 'banner warning' }, 'Recording is not supported in this browser. Use the "Upload audio" tab instead.'));
    return box;
  }

  const maxBytes = CONFIG.maxAudioMB * 1024 * 1024;
  const maxSeconds = Math.floor((maxBytes * 0.9) / RECORDER_BYTES_PER_SECOND);
  let stream = null;
  let recorder = null;
  let chunks = [];
  let size = 0;
  let seconds = 0;
  let timer = null;
  let previewUrl = null;
  let phase = 'idle'; // idle | recording | paused | recorded | uploading

  const clock = h('span', { 'aria-live': 'off', text: '00:00' });
  const label = h('span', { class: 'small', text: `Up to ${formatClock(maxSeconds)} (${CONFIG.maxAudioMB} MB)` });
  const errorBox = h('div', { class: 'banner rejected hidden', role: 'alert' });
  const preview = h('div');
  const progress = h('div');
  const controls = h('div', { class: 'rec-controls' });
  const dot = h('span', { class: 'rec-dot', 'aria-hidden': 'true' });
  append(box, [h('div', { class: 'recorder-status' }, dot, clock, label), errorBox, controls, preview, progress]);

  function releaseStream() {
    if (stream) { stream.getTracks().forEach((track) => track.stop()); stream = null; }
    clearInterval(timer);
    timer = null;
  }
  function releasePreview() {
    if (previewUrl) { URL.revokeObjectURL(previewUrl); previewUrl = null; }
    clear(preview);
  }
  function reset() {
    releaseStream();
    releasePreview();
    recorder = null;
    chunks = [];
    size = 0;
    seconds = 0;
    clock.textContent = '00:00';
    setPhase('idle');
  }
  onLeave(() => {
    if (recorder && recorder.state !== 'inactive') recorder.onstop = null;
    try { if (recorder && recorder.state !== 'inactive') recorder.stop(); } catch (_) { /* already stopped */ }
    releaseStream();
    releasePreview();
  });

  function button(text, cls, action) {
    return h('button', { class: `btn ${cls}`, type: 'button', text, on: { click: action } });
  }

  function setPhase(next) {
    phase = next;
    box.classList.toggle('recording', phase === 'recording');
    box.classList.toggle('paused', phase === 'paused');
    clear(controls);
    if (phase === 'idle') controls.append(button('Start recording', 'btn-primary', start));
    if (phase === 'recording') controls.append(button('Pause', 'btn-secondary', () => { recorder.pause(); setPhase('paused'); }), button('Stop', 'btn-primary', stop), button('Discard', 'btn-ghost', reset));
    if (phase === 'paused') controls.append(button('Resume', 'btn-secondary', () => { recorder.resume(); setPhase('recording'); }), button('Stop', 'btn-primary', stop), button('Discard', 'btn-ghost', reset));
    if (phase === 'recorded') controls.append(button('Upload recording', 'btn-primary', upload), button('Discard', 'btn-ghost', reset));
  }

  async function start() {
    hideBanner(errorBox);
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      const denied = err && (err.name === 'NotAllowedError' || err.name === 'SecurityError');
      showBanner(errorBox, denied
        ? 'Microphone permission was denied. Allow it in your browser settings, or use the "Upload audio" tab.'
        : 'No microphone could be opened. Check your device, or use the "Upload audio" tab.');
      return;
    }
    const mime = RECORDER_MIME_CANDIDATES.find((type) => MediaRecorder.isTypeSupported?.(type)) || '';
    try {
      recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    } catch (_) {
      releaseStream();
      showBanner(errorBox, 'This browser cannot record audio in a supported format. Use the "Upload audio" tab.');
      return;
    }
    chunks = [];
    size = 0;
    seconds = 0;
    recorder.ondataavailable = (event) => {
      if (!event.data || !event.data.size) return;
      chunks.push(event.data);
      size += event.data.size;
      if (size >= maxBytes * 0.95 && recorder.state !== 'inactive') stop();
    };
    recorder.onstop = () => {
      releaseStream();
      const type = (recorder.mimeType || mime || 'audio/webm').split(';')[0];
      const blob = new Blob(chunks, { type });
      if (!blob.size) { showBanner(errorBox, 'Nothing was recorded. Try again.'); setPhase('idle'); return; }
      releasePreview();
      previewUrl = URL.createObjectURL(blob);
      append(preview, [h('audio', { controls: true, src: previewUrl, 'aria-label': 'Recorded consultation preview' })]);
      recorder.blob = blob;
      setPhase('recorded');
    };
    recorder.start(1000);
    timer = setInterval(() => {
      if (phase !== 'recording') return;
      seconds += 1;
      clock.textContent = formatClock(seconds);
      if (seconds >= maxSeconds) { toast('Maximum recording length reached.', 'info'); stop(); }
    }, 1000);
    setPhase('recording');
  }

  function stop() {
    if (recorder && recorder.state !== 'inactive') recorder.stop();
  }

  async function upload() {
    const blob = recorder?.blob;
    if (!blob) return;
    const file = new File([blob], `recording.${audioExtension(blob.type)}`, { type: blob.type });
    const bar = h('i');
    append(clear(progress), [h('div', { class: 'progress' }, bar), h('div', { class: 'progress-label', text: 'Uploading recording…' })]);
    phase = 'uploading';
    $$('button', controls).forEach((b) => { b.disabled = true; });
    try {
      await uploadFile(`/api/consults/${enc(consultId)}/audio`, file, (fraction) => { bar.style.width = `${Math.round(fraction * 100)}%`; });
      releasePreview();
      clear(progress);
      await onUploaded();
    } catch (err) {
      clear(progress);
      setPhase('recorded');
      showBanner(errorBox, err.message);
    }
  }

  setPhase('idle');
  return box;
}

/* ═══════════════ 11. Voice module ═══════════════ */

const PCM_RATE = 16000;

function floatTo16BitBase64(float32) {
  const bytes = new Uint8Array(float32.length * 2);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < float32.length; i += 1) {
    const sample = Math.max(-1, Math.min(1, float32[i]));
    view.setInt16(i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true);
  }
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function playPcmBase64(context, base64, state_) {
  const binary = atob(base64);
  const samples = new Float32Array(binary.length / 2);
  for (let i = 0; i < samples.length; i += 1) {
    const value = binary.charCodeAt(i * 2) | (binary.charCodeAt(i * 2 + 1) << 8);
    samples[i] = (value >= 0x8000 ? value - 0x10000 : value) / 0x8000;
  }
  const buffer = context.createBuffer(1, samples.length, PCM_RATE);
  buffer.copyToChannel(samples, 0);
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(context.destination);
  state_.next = Math.max(state_.next, context.currentTime);
  source.start(state_.next);
  state_.next += buffer.duration;
  state_.sources.push(source);
}

/** Optional ElevenLabs agent session (beta). The server creates the signed URL so the API key never
 *  reaches the browser. Streaming protocol details are marked VERIFY AGAINST DOCS in the README. */
async function connectVoiceAgent(session, ui) {
  const context = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: PCM_RATE });
  const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  const socket = new WebSocket(session.signed_url);
  const playback = { next: 0, sources: [] };
  let processor = null;
  let input = null;

  const stop = () => {
    try { socket.close(); } catch (_) { /* already closed */ }
    mic.getTracks().forEach((t) => t.stop());
    if (processor) processor.disconnect();
    if (input) input.disconnect();
    playback.sources.forEach((s) => { try { s.stop(); } catch (_) { /* finished */ } });
    context.close().catch(() => {});
    ui.onEnd();
  };
  onLeave(stop);

  socket.addEventListener('open', () => {
    socket.send(JSON.stringify({ type: 'conversation_initiation_client_data', dynamic_variables: session.dynamic_variables || {} }));
    input = context.createMediaStreamSource(mic);
    processor = context.createScriptProcessor(4096, 1, 1);
    processor.onaudioprocess = (event) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ user_audio_chunk: floatTo16BitBase64(event.inputBuffer.getChannelData(0)) }));
    };
    input.connect(processor);
    processor.connect(context.destination);
    ui.onStatus('Listening. Ask about your prescription.');
  });
  socket.addEventListener('message', (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch (_) { return; }
    if (message.type === 'audio' && message.audio_event?.audio_base_64) playPcmBase64(context, message.audio_event.audio_base_64, playback);
    else if (message.type === 'ping') socket.send(JSON.stringify({ type: 'pong', event_id: message.ping_event?.event_id }));
    else if (message.type === 'interruption') { playback.sources.forEach((s) => { try { s.stop(); } catch (_) { /* finished */ } }); playback.next = 0; }
    else if (message.type === 'agent_response' && message.agent_response_event?.agent_response) ui.onLine('Assistant', message.agent_response_event.agent_response);
    else if (message.type === 'user_transcript' && message.user_transcription_event?.user_transcript) ui.onLine('You', message.user_transcription_event.user_transcript);
  });
  socket.addEventListener('error', () => { ui.onStatus('The assistant connection failed.'); stop(); });
  socket.addEventListener('close', stop);
  return stop;
}

/** Listen (text-to-speech summary) and the optional voice assistant for one approved prescription. */
function voicePanel(prescriptionId) {
  const playerSlot = h('div');
  const transcript = h('ul', { class: 'stack small' });
  const status = h('p', { class: 'small', role: 'status' });
  let audioUrl = null;
  let stopAgent = null;
  onLeave(() => { if (audioUrl) URL.revokeObjectURL(audioUrl); if (stopAgent) stopAgent(); });

  const listen = h('button', { class: 'btn btn-secondary', type: 'button', text: 'Listen to summary', on: { click: () => withBusy(listen, async () => {
    try {
      const blob = await api('POST', `/api/prescriptions/${enc(prescriptionId)}/speak`, null, { blob: true });
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      audioUrl = URL.createObjectURL(blob);
      const player = h('audio', { class: 'audio-player', controls: true, src: audioUrl, 'aria-label': 'Prescription summary' });
      append(clear(playerSlot), [player]);
      player.play().catch(() => {});
    } catch (err) { toast(err.message, 'error'); }
  }) } });

  const talk = h('button', { class: 'btn btn-secondary', type: 'button', text: 'Talk to the assistant (beta)', on: { click: () => withBusy(talk, async () => {
    try {
      const session = await apiPost('/api/voice/session', { prescription_id: prescriptionId });
      status.textContent = 'Connecting…';
      stopAgent = await connectVoiceAgent(session, {
        onStatus: (text) => { status.textContent = text; },
        onLine: (who, text) => transcript.append(h('li', null, h('strong', { text: `${who}: ` }), text)),
        onEnd: () => { status.textContent = 'Session ended.'; stopAgent = null; talk.textContent = 'Talk to the assistant (beta)'; },
      });
      talk.textContent = 'End conversation';
      talk.onclick = () => { if (stopAgent) stopAgent(); };
    } catch (err) {
      status.textContent = '';
      toast(err.name === 'NotAllowedError' ? 'Microphone permission was denied.' : err.message, 'error');
    }
  }) } });

  return h('div', { class: 'stack' },
    h('p', { class: 'small', text: 'A plain-language summary of this prescription. It only repeats what your doctor wrote.' }),
    h('div', { class: 'row gap wrap' }, listen, talk), playerSlot, status, transcript,
    h('p', { class: 'small', text: 'The assistant explains this prescription only. For urgent symptoms contact your doctor or emergency services.' }));
}

/* ═══════════════ 12. Bootstrap ═══════════════ */

function bootstrap() {
  wireModal();
  const toggleButton = $('#nav-toggle');
  toggleButton.addEventListener('click', () => {
    const open = $('#nav-links').classList.toggle('open');
    toggleButton.setAttribute('aria-expanded', String(open));
  });
  $('#nav-links').addEventListener('click', (e) => {
    if (e.target.closest('a')) { $('#nav-links').classList.remove('open'); toggleButton.setAttribute('aria-expanded', 'false'); }
  });
  $('#btn-logout').addEventListener('click', logout);
  window.addEventListener('hashchange', handleRoute);
  window.addEventListener('unhandledrejection', (event) => {
    if (event.reason instanceof ApiError) { event.preventDefault(); if (event.reason.status !== 401) toast(event.reason.message, 'error'); }
  });
  toggle($('#loading-overlay'), true);
  handleRoute().finally(() => toggle($('#loading-overlay'), false));
}

bootstrap();
