// The owner ID and history never enter the public catalogue or D1 plaintext.
const ROW = 'owner';
const AAD = new TextEncoder().encode('aromatia:private-state:owner:v1');
const encoder = new TextEncoder();
const reactions = new Set(['want', 'known', 'not_for_me', 'bought']);
const headers = {
  'Content-Type': 'application/json; charset=utf-8',
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
};

class HttpError extends Error {
  constructor(status, code) { super(code); this.status = status; this.code = code; }
}
const invalid = () => { throw new HttpError(400, 'invalid_request'); };
const reply = (value, status = 200) => new Response(JSON.stringify(value), { status, headers });
const integer = (value, minimum = 0) => Number.isSafeInteger(value) && value >= minimum;
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const clone = value => JSON.parse(JSON.stringify(value));
const ids = value => Array.isArray(value) && value.every(id => integer(id, 1));
const random = () => crypto.getRandomValues(new Uint32Array(1))[0] / 4294967296;
const pick = values => values.length ? values[Math.floor(random() * values.length)] : null;

function query(raw) {
  // URLSearchParams otherwise tolerates malformed escapes and duplicate names.
  if (/%(?![0-9a-f]{2})/i.test(raw)) invalid();
  const values = new Map();
  for (const part of raw.replace(/^\?/, '').split('&').filter(Boolean)) {
    const separator = part.indexOf('=');
    let key, value;
    try {
      key = decodeURIComponent((separator < 0 ? part : part.slice(0, separator)).replace(/\+/g, ' '));
      value = decodeURIComponent((separator < 0 ? '' : part.slice(separator + 1)).replace(/\+/g, ' '));
    } catch { invalid(); }
    if (!key || /[\r\n\u0000=]/.test(key) || /[\r\n\u0000]/.test(value) || values.has(key)) invalid();
    values.set(key, value);
  }
  return values;
}

async function hmac(key, message) {
  const imported = await crypto.subtle.importKey('raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', imported, encoder.encode(message)));
}

async function authenticate(request, env) {
  if (!env.TELEGRAM_BOT_TOKEN || !/^[1-9]\d*$/.test(String(env.TELEGRAM_CHAT_ID || ''))) {
    throw new HttpError(503, 'temporarily_unavailable');
  }
  const authorization = request.headers.get('Authorization') || '';
  const denied = () => { throw new HttpError(401, 'authorization_required'); };
  if (!authorization.startsWith('tma ') || authorization.length > 16384) denied();
  let values;
  try { values = query(authorization.slice(4)); } catch { denied(); }
  const hash = values.get('hash');
  const date = values.get('auth_date');
  if (!/^[0-9a-f]{64}$/i.test(hash || '') || !/^\d{1,12}$/.test(date || '')) denied();
  const age = Math.floor(Date.now() / 1000) - Number(date);
  if (age < 0 || age > 3600) denied();
  const check = [...values].filter(([key]) => key !== 'hash')
    .sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, value]) => `${key}=${value}`).join('\n');
  const secret = await hmac(encoder.encode('WebAppData'), env.TELEGRAM_BOT_TOKEN);
  const expected = await hmac(secret, check);
  let difference = 0;
  for (let index = 0; index < expected.length; index++) difference |= expected[index] ^ parseInt(hash.slice(index * 2, index * 2 + 2), 16);
  if (difference) denied();
  let user;
  try { user = JSON.parse(values.get('user')); } catch { denied(); }
  if (!object(user) || !integer(user.id, 1) || user.is_bot === true) denied();
  if (String(user.id) !== String(env.TELEGRAM_CHAT_ID)) throw new HttpError(403, 'access_denied');
}

async function dataKey(env) {
  if (!/^[0-9a-f]{64}$/i.test(env.WORKER_DATA_KEY || '')) throw new Error('configuration');
  const bytes = Uint8Array.from(env.WORKER_DATA_KEY.match(/../g), part => parseInt(part, 16));
  return crypto.subtle.importKey('raw', bytes, 'AES-GCM', false, ['encrypt', 'decrypt']);
}
function base64(bytes) {
  let binary = '';
  for (let index = 0; index < bytes.length; index += 8192) binary += String.fromCharCode(...bytes.subarray(index, index + 8192));
  return btoa(binary);
}
async function encryptJSON(value, env, aad = AAD) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: aad }, await dataKey(env), encoder.encode(JSON.stringify(value))));
  const bytes = new Uint8Array(iv.length + ciphertext.length);
  bytes.set(iv); bytes.set(ciphertext, iv.length);
  return `v1.${base64(bytes)}`;
}
async function decryptJSON(ciphertext, env, aad = AAD) {
  if (typeof ciphertext !== 'string' || !ciphertext.startsWith('v1.')) throw new Error('state_format');
  const bytes = Uint8Array.from(atob(ciphertext.slice(3)), character => character.charCodeAt(0));
  if (bytes.length < 29) throw new Error('state_format');
  const plain = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: bytes.slice(0, 12), additionalData: aad }, await dataKey(env), bytes.slice(12));
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(plain));
}
const encrypt = (state, env) => encryptJSON(state, env);
const decrypt = async (ciphertext, env) => validateState(await decryptJSON(ciphertext, env));
function validateState(state) {
  if (!object(state) || !integer(state.revision) || !(state.currentId === null || integer(state.currentId, 1)) ||
      !ids(state.seenIds) || !ids(state.sentIds) || !object(state.feedback)) throw new Error('state_format');
  for (const [id, feedback] of Object.entries(state.feedback)) {
    if (!/^[1-9]\d*$/.test(id) || !integer(Number(id), 1) || !object(feedback) || !reactions.has(feedback.reaction) ||
        typeof feedback.updated_at !== 'string' || !Number.isFinite(Date.parse(feedback.updated_at))) throw new Error('state_format');
  }
  if (state.undo !== null) {
    const undo = state.undo;
    if (!object(undo) || !integer(undo.perfumeId, 1) || !integer(undo.revisionAfter) || !reactions.has(undo.reaction) ||
        typeof undo.feedbackAt !== 'string' || !ids(undo.seenBefore)) throw new Error('state_format');
  }
  return state;
}

async function readState(env) {
  let row = await env.DB.prepare('SELECT revision, ciphertext FROM private_state WHERE id = ?').bind(ROW).first();
  if (!row) {
    // Bootstrap is a Worker secret, never a file bundled with the application.
    if (!env.BOOTSTRAP_STATE) throw new Error('bootstrap_required');
    const initial = validateState(JSON.parse(env.BOOTSTRAP_STATE));
    await env.DB.prepare('INSERT OR IGNORE INTO private_state (id, revision, ciphertext, updated_at) VALUES (?, ?, ?, ?)')
      .bind(ROW, initial.revision, await encrypt(initial, env), new Date().toISOString()).run();
    row = await env.DB.prepare('SELECT revision, ciphertext FROM private_state WHERE id = ?').bind(ROW).first();
  }
  if (!row) throw new Error('state_missing');
  const state = await decrypt(row.ciphertext, env);
  if (state.revision !== row.revision) throw new Error('state_revision');
  return state;
}
async function saveState(previousRevision, state, env) {
  const result = await env.DB.prepare('UPDATE private_state SET revision = ?, ciphertext = ?, updated_at = ? WHERE id = ? AND revision = ?')
    .bind(state.revision, await encrypt(state, env), new Date().toISOString(), ROW, previousRevision).run();
  return result.meta?.changes === 1;
}

function excluded(state) { return new Set([...state.sentIds, ...state.seenIds, ...Object.keys(state.feedback).map(Number)]); }
function rawNotes(card) {
  const pyramid = card.notes;
  const values = Array.isArray(pyramid) ? pyramid : object(pyramid) ? Object.values(pyramid).flat() : [];
  return [...new Set(values.filter(value => typeof value === 'string' && value.trim()))];
}
function notes(card) { return [...new Set(rawNotes(card).map(value => value.toLowerCase().trim()))]; }
function array(value) { return Array.isArray(value) ? value : typeof value === 'string' && value ? [value] : []; }
function recommend(state, catalogue, env) {
  const history = excluded(state);
  let candidates = catalogue.filter(card => !history.has(card.id));
  const latest = Object.entries(state.feedback).sort(([, a], [, b]) => b.updated_at.localeCompare(a.updated_at))[0];
  const lastHouse = latest && catalogue.find(card => card.id === Number(latest[0]))?.house;
  if (lastHouse && candidates.some(card => card.house !== lastHouse)) candidates = candidates.filter(card => card.house !== lastHouse);
  if (!candidates.length) return null;
  const profile = new Map();
  let positiveCount = 0, negativeCount = 0;
  for (const feedback of Object.values(state.feedback)) {
    if (feedback.reaction === 'want' || feedback.reaction === 'bought') positiveCount++;
    if (feedback.reaction === 'not_for_me') negativeCount++;
  }
  const houses = new Map();
  for (const card of catalogue) {
    if (history.has(card.id)) houses.set(card.house, (houses.get(card.house) || 0) + 1);
    const reaction = state.feedback[card.id]?.reaction;
    const weight = reaction === 'want' || reaction === 'bought' ? 1 / Math.max(positiveCount, 1) : reaction === 'not_for_me' ? -1 / Math.max(negativeCount, 1) : 0;
    if (weight) for (const note of notes(card)) profile.set(note, (profile.get(note) || 0) + weight);
  }
  const norm = Math.sqrt([...profile.values()].reduce((sum, weight) => sum + weight * weight, 0));
  const ranks = catalogue.map(card => card.popularity_rank).filter(rank => Number.isFinite(rank) && rank > 0);
  const maxRank = Math.max(1, ...ranks);
  const rawRatio = Number(env.EXPLORATION_RATIO ?? 0.30);
  const ratio = Number.isFinite(rawRatio) && rawRatio >= 0 && rawRatio <= 1 ? rawRatio : 0.30;
  if (random() < ratio) {
    const least = Math.min(...candidates.map(card => houses.get(card.house) || 0));
    return { card: pick(candidates.filter(card => (houses.get(card.house) || 0) === least)), reason: 'Новый аромат для знакомства с разными направлениями.' };
  }
  const ranked = candidates.map(card => {
    const cardNotes = notes(card);
    const taste = norm && cardNotes.length ? cardNotes.reduce((sum, note) => sum + (profile.get(note) || 0), 0) / (norm * Math.sqrt(cardNotes.length)) : 0;
    // Missing popularity is neutral, so these perfumes remain eligible.
    const hype = Number.isFinite(card.popularity_rank) && card.popularity_rank > 0 ? Math.min(card.popularity_rank, maxRank) / maxRank : 0.5;
    return { card, taste, score: 0.85 * taste + 0.15 * hype, tie: random() };
  }).sort((a, b) => b.score - a.score || a.tie - b.tie);
  const configured = Number(env.TOP_N ?? 20);
  const topN = Number.isSafeInteger(configured) && configured > 0 && configured <= 100 ? configured : 20;
  const best = ranked.slice(0, topN);
  // A softmax preserves useful ranking while avoiding the same first ID every day.
  const weights = best.map(candidate => Math.exp((candidate.score - best[0].score) * 6));
  let threshold = random() * weights.reduce((sum, weight) => sum + weight, 0);
  let selected = best[best.length - 1];
  for (let index = 0; index < best.length; index++) { threshold -= weights[index]; if (threshold <= 0) { selected = best[index]; break; } }
  return { card: selected.card, reason: selected.taste > 0.05 ? 'В подборке есть ноты, которые тебе понравились.' : 'Вариант для знакомства с новыми сочетаниями нот.' };
}
function setRecommendation(state, catalogue, env) {
  const selected = recommend(state, catalogue, env);
  state.currentId = selected?.card.id ?? null;
  state.currentReason = selected?.reason ?? null;
}
function undoAvailable(state) {
  const undo = state.undo;
  const feedback = undo && state.feedback[undo.perfumeId];
  return !!(undo && undo.revisionAfter === state.revision && feedback?.reaction === undo.reaction && feedback.updated_at === undo.feedbackAt && !state.sentIds.includes(undo.perfumeId));
}
function cardWithSeed(card, seed) {
  const performance = seed[String(card.id)] ?? seed[card.external_id];
  return { ...card, performance: object(performance) && Object.hasOwn(performance, 'performance') ? performance.performance : performance ?? card.performance ?? null };
}

const LONGEVITY = { poor: 'Очень низкая', weak: 'Низкая', moderate: 'Умеренная', 'long lasting': 'Высокая', 'very long lasting': 'Очень высокая' };
const SILLAGE = { intimate: 'Близко к коже', moderate: 'Умеренный', soft: 'Мягкий', strong: 'Сильный', enormous: 'Очень сильный' };
const normalize = value => String(value ?? '').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');
function exactMatch(card, row) {
  const brand = normalize(row.Brand), target = normalize(card.house), name = normalize(row.Name), expected = normalize(card.name);
  return !!(target && [target, target + 'perfumes'].includes(brand) &&
    [expected, target + expected, brand + expected].includes(name) &&
    (!normalize(card.concentration) || normalize(card.concentration) === normalize(row.OilType)));
}
async function performanceKey(card) {
  const digest = await crypto.subtle.digest('SHA-256', encoder.encode(JSON.stringify([card.house ?? null, card.name ?? null, card.concentration ?? null])));
  return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
}
const performanceAAD = key => encoder.encode(`aromatia:performance-cache:v1:${key}`);
async function readPerformanceCache(env) {
  const now = Math.floor(Date.now() / 1000);
  const result = await env.DB.prepare('SELECT key, ciphertext, expires FROM performance_cache WHERE expires > ? ORDER BY expires DESC LIMIT 64').bind(now).all();
  const cache = new Map();
  for (const row of result.results || []) {
    try {
      const value = await decryptJSON(row.ciphertext, env, performanceAAD(row.key));
      if (value === null || object(value)) cache.set(row.key, value);
    } catch { /* A corrupt optional cache entry never hides a successful mutation. */ }
  }
  return cache;
}
async function cachedPerformance(card, cache) {
  if (card?.performance || !card) return card;
  const cached = cache.get(await performanceKey(card));
  return cached ? { ...card, performance: cached } : card;
}
async function trimPerformanceCache(env) {
  await env.DB.prepare('DELETE FROM performance_cache WHERE key NOT IN (SELECT key FROM performance_cache ORDER BY expires DESC LIMIT 64)').run();
}
async function rememberPerformance(key, value, env) {
  const expires = Math.floor(Date.now() / 1000) + (value ? 86400 : 900);
  await env.DB.prepare('INSERT INTO performance_cache (key, ciphertext, expires) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET ciphertext = excluded.ciphertext, expires = excluded.expires')
    .bind(key, await encryptJSON(value, env, performanceAAD(key)), expires).run();
  await trimPerformanceCache(env);
}
async function providerGet(path, env, signal) {
  const response = await fetch(`https://api.fragella.com/api/v1/${path}`, {
    headers: { 'x-api-key': env.FRAGELLA_API_KEY }, redirect: 'error', signal,
    cache: 'no-store',
  });
  if (response.status !== 200) {
    await response.body?.cancel();
    if ([401, 402, 403, 429].includes(response.status)) {
      await env.DB.prepare("UPDATE performance_guard SET blocked_until = ? WHERE id = 'fragella'").bind(Math.floor(Date.now() / 1000) + 900).run();
    }
    return null;
  }
  const reader = response.body?.getReader();
  if (!reader) return null;
  const chunks = []; let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 1000000) { await reader.cancel(); return null; }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
}
async function enrichPerformance(card, env) {
  if (!card || card.performance) return card;
  try {
    const key = await performanceKey(card), now = Math.floor(Date.now() / 1000);
    const cache = await readPerformanceCache(env);
    if (cache.has(key)) return cache.get(key) ? { ...card, performance: cache.get(key) } : card;
    if (!env.FRAGELLA_API_KEY || String(env.FRAGELLA_ENABLED ?? 'true').toLowerCase() === 'false') return card;
    const day = new Date().toISOString().slice(0, 10);
    await env.DB.prepare("INSERT OR IGNORE INTO performance_guard (id, day, searches, blocked_until, lease_until, lease_token) VALUES ('fragella', '', 0, 0, 0, '')").run();
    await env.DB.prepare("UPDATE performance_guard SET day = ?, searches = 0 WHERE id = 'fragella' AND day <> ?").bind(day, day).run();
    const guard = await env.DB.prepare("SELECT searches, blocked_until FROM performance_guard WHERE id = 'fragella'").first();
    if (!guard || guard.searches >= 20 || guard.blocked_until > now) return card;
    // A short negative lease prevents simultaneous isolates from searching the same item.
    const leased = await env.DB.prepare('INSERT INTO performance_cache (key, ciphertext, expires) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET ciphertext = excluded.ciphertext, expires = excluded.expires WHERE performance_cache.expires <= ?')
      .bind(key, await encryptJSON(null, env, performanceAAD(key)), now + 15, now).run();
    if (leased.meta?.changes !== 1) return card;
    // Serialize quota checks across isolates, just as the local provider client does.
    const leaseToken = crypto.randomUUID();
    const globalLease = await env.DB.prepare("UPDATE performance_guard SET lease_until = ?, lease_token = ? WHERE id = 'fragella' AND lease_until <= ?")
      .bind(now + 15, leaseToken, now).run();
    if (globalLease.meta?.changes !== 1) return card;
    let result = null;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);
    try {
      const usage = await providerGet('usage', env, controller.signal);
      if (!object(usage) || String(usage.plan).toLowerCase() !== 'free' || !object(usage.usage) || !(Number(usage.usage.requests_remaining) > 0)) {
        await env.DB.prepare("UPDATE performance_guard SET blocked_until = ? WHERE id = 'fragella'").bind(now + 900).run();
      } else {
        // Claim the search atomically after verifying the free plan and remaining quota.
        const claim = await env.DB.prepare("UPDATE performance_guard SET searches = searches + 1 WHERE id = 'fragella' AND day = ? AND searches < 20 AND blocked_until <= ?").bind(day, now).run();
        if (claim.meta?.changes === 1) {
          const params = new URLSearchParams({ search: card.name, brand: card.house, limit: '10' });
          const payload = await providerGet(`fragrances?${params}`, env, controller.signal);
          const rows = object(payload) ? payload.data : payload;
          const matches = Array.isArray(rows) ? rows.filter(row => object(row) && exactMatch(card, row)) : [];
          if (matches.length === 1) {
            const row = matches[0], longevity = LONGEVITY[String(row.Longevity).toLowerCase()], sillage = SILLAGE[String(row.Sillage).toLowerCase()];
            if (longevity || sillage) result = { longevity: longevity || null, sillage: sillage || null, source_name: 'Fragella', source_url: 'https://api.fragella.com/', basis: 'provider_aggregate', provider_id: String(row._id ?? ''), observed_at: new Date().toISOString(), confidence: String(row.Confidence ?? '').toLowerCase(), sample_size: null };
          }
        }
      }
    } catch { /* Optional provider failure preserves the committed application state. */ }
    finally {
      clearTimeout(timeout);
      await env.DB.prepare("UPDATE performance_guard SET lease_until = 0, lease_token = '' WHERE id = 'fragella' AND lease_token = ?").bind(leaseToken).run();
    }
    await rememberPerformance(key, result, env);
    return result ? { ...card, performance: result } : card;
  } catch { return card; }
}
async function enrichResponse(result, env) {
  if (result.current) result.current = await enrichPerformance(result.current, env);
  return result;
}
async function enrichList(result, env) {
  try {
    const cache = await readPerformanceCache(env);
    result.items = await Promise.all(result.items.map(card => cachedPerformance(card, cache)));
  } catch { /* Lists never perform external lookups. */ }
  return result;
}
function stateResponse(state, catalogue, seed) {
  const history = excluded(state);
  const current = catalogue.find(card => card.id === state.currentId);
  const feedback = Object.values(state.feedback);
  return {
    revision: state.revision,
    current: current ? { ...cardWithSeed(current, seed), recommendation_reason_ru: state.currentReason || 'Аромат из твоей персональной подборки.' } : null,
    counts: { wishlist: feedback.filter(item => item.reaction === 'want').length, known: feedback.filter(item => item.reaction === 'known' || item.reaction === 'bought').length },
    seen_count: catalogue.filter(card => history.has(card.id)).length,
    remaining: catalogue.filter(card => !history.has(card.id)).length,
    total: catalogue.length,
    can_undo: undoAvailable(state),
  };
}
async function ensureCurrent(env, catalogue) {
  let state = await readState(env);
  for (let attempt = 0; attempt < 3; attempt++) {
    if (state.currentId !== null && catalogue.some(card => card.id === state.currentId) && !excluded(state).has(state.currentId)) return state;
    const next = clone(state);
    setRecommendation(next, catalogue, env);
    if (next.currentId === state.currentId) return state;
    next.revision++;
    next.undo = null;
    if (await saveState(state.revision, next, env)) return next;
    state = await readState(env);
  }
  throw new Error('contention');
}

async function body(request, fields) {
  if ((request.headers.get('Content-Type') || '').split(';')[0].trim().toLowerCase() !== 'application/json') invalid();
  const reader = request.body?.getReader();
  if (!reader) invalid();
  const chunks = []; let size = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > 4096) { await reader.cancel(); throw new HttpError(413, 'request_too_large'); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  let value;
  try { value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); } catch { invalid(); }
  if (!object(value) || Object.keys(value).length !== fields.length || !fields.every(field => Object.hasOwn(value, field)) || !integer(value.revision)) invalid();
  if (fields.includes('perfume_id') && !integer(value.perfume_id, 1)) invalid();
  if (fields.includes('reaction') && !reactions.has(value.reaction)) invalid();
  return value;
}
function filterCatalogue(url, state, catalogue, seed) {
  const values = query(url.search);
  const permitted = new Set(['q', 'family', 'season', 'note', 'time', 'offset', 'limit']);
  if ([...values.keys()].some(key => !permitted.has(key))) invalid();
  for (const key of ['q', 'family', 'season', 'note', 'time']) if ((values.get(key) || '').length > 120) invalid();
  const number = (key, fallback, maximum) => {
    const raw = values.get(key) ?? String(fallback);
    if (!/^\d{1,5}$/.test(raw) || Number(raw) > maximum) invalid();
    return Number(raw);
  };
  const offset = number('offset', 0, 10000), limit = number('limit', 30, 60);
  if (!limit) invalid();
  if (values.has('season') && !['spring', 'summer', 'autumn', 'winter', ''].includes(values.get('season'))) invalid();
  if (values.has('time') && !['day', 'evening', 'night', 'morning', ''].includes(values.get('time'))) invalid();
  const normalized = key => (values.get(key) || '').trim().toLowerCase();
  const text = normalized('q'), family = normalized('family'), season = normalized('season'), note = normalized('note'), time = normalized('time');
  const familyValues = card => array(card.family).filter(value => typeof value === 'string');
  const matches = catalogue.filter(card => (!text || [card.name, card.house, ...familyValues(card), ...notes(card)].join(' ').toLowerCase().includes(text)) &&
    (!family || familyValues(card).some(value => value.toLowerCase() === family)) &&
    (!season || array(card.seasons).includes(season)) && (!time || array(card.times_of_day).includes(time)) && (!note || notes(card).includes(note)));
  const distinct = items => [...new Set(items.filter(value => typeof value === 'string' && value))].sort();
  const history = excluded(state);
  return {
    items: matches.slice(offset, offset + limit).map(card => ({ ...cardWithSeed(card, seed), can_select: !history.has(card.id), reaction: state.feedback[card.id]?.reaction ?? null })),
    count: matches.length, total: catalogue.length, offset, limit,
    facets: { families: distinct(catalogue.flatMap(familyValues)), seasons: distinct(catalogue.flatMap(card => array(card.seasons))), notes: distinct(catalogue.flatMap(rawNotes)), times: distinct(catalogue.flatMap(card => array(card.times_of_day))) },
  };
}

/** Return null for static routes so the hosting Worker can delegate to assets. */
export async function handleApi(request, env, catalogue) {
  const url = new URL(request.url);
  if (url.pathname === '/health') return request.method === 'GET' ? reply({ status: 'ok' }) : reply({ error: 'invalid_request' }, 405);
  if (url.pathname !== '/api' && !url.pathname.startsWith('/api/')) return null;
  try {
    await authenticate(request, env);
    let seed = {};
    if (env.PERFORMANCE_SEED) {
      const parsed = JSON.parse(env.PERFORMANCE_SEED);
      seed = Array.isArray(parsed) ? Object.fromEntries(parsed.map(item => [String(item.id), item.performance])) : parsed;
      if (!object(seed)) throw new Error('configuration');
    }
    const route = url.pathname;
    const methods = { '/api/state': 'GET', '/api/library': 'GET', '/api/catalog': 'GET', '/api/react': 'POST', '/api/select': 'POST', '/api/undo': 'POST' };
    if (!Object.hasOwn(methods, route) && !route.startsWith('/api/performance/')) throw new HttpError(404, 'not_found');
    if (request.method !== (methods[route] || 'GET')) throw new HttpError(405, 'invalid_request');
    if (route.startsWith('/api/performance/')) {
      const id = route.slice('/api/performance/'.length);
      if (!/^[1-9]\d{0,9}$/.test(id)) invalid();
      const card = catalogue.find(item => item.id === Number(id));
      if (!card) throw new HttpError(404, 'not_found');
      return reply({ performance: (await enrichPerformance(cardWithSeed(card, seed), env)).performance });
    }
    const state = await ensureCurrent(env, catalogue);
    if (route === '/api/state') return reply(await enrichResponse(stateResponse(state, catalogue, seed), env));
    if (route === '/api/catalog') return reply(await enrichList(filterCatalogue(url, state, catalogue, seed), env));
    if (route === '/api/library') {
      const values = query(url.search), kind = values.get('kind');
      if (values.size !== 1 || !['wishlist', 'known'].includes(kind)) invalid();
      const allowed = kind === 'wishlist' ? ['want'] : ['known', 'bought'];
      const items = catalogue.filter(card => allowed.includes(state.feedback[card.id]?.reaction))
        .sort((a, b) => state.feedback[b.id].updated_at.localeCompare(state.feedback[a.id].updated_at))
        .map(card => ({ ...cardWithSeed(card, seed), reaction: state.feedback[card.id].reaction }));
      return reply(await enrichList({ kind, items, count: items.length }, env));
    }
    const fields = route === '/api/react' ? ['perfume_id', 'revision', 'reaction'] : route === '/api/select' ? ['perfume_id', 'revision'] : ['revision'];
    const input = await body(request, fields);
    const unchanged = async () => reply(await enrichResponse({ ...stateResponse(state, catalogue, seed), applied: false }, env));
    if (input.revision !== state.revision) return unchanged();
    const next = clone(state);
    if (route === '/api/react') {
      if (input.perfume_id !== state.currentId || excluded(state).has(input.perfume_id)) return unchanged();
      const timestamp = new Date().toISOString();
      next.feedback[input.perfume_id] = { reaction: input.reaction, updated_at: timestamp };
      next.seenIds = [...new Set([...state.seenIds, input.perfume_id])];
      next.undo = { perfumeId: input.perfume_id, revisionAfter: state.revision + 1, reaction: input.reaction, feedbackAt: timestamp, seenBefore: state.seenIds, reasonBefore: state.currentReason || null };
      setRecommendation(next, catalogue, env);
    } else if (route === '/api/select') {
      if (!catalogue.some(card => card.id === input.perfume_id) || excluded(state).has(input.perfume_id) || state.currentId === input.perfume_id) return unchanged();
      next.currentId = input.perfume_id;
      next.currentReason = 'Ты выбрал этот аромат в каталоге.';
      next.undo = null;
    } else {
      if (!undoAvailable(state)) return unchanged();
      delete next.feedback[state.undo.perfumeId];
      next.seenIds = state.undo.seenBefore;
      next.currentId = state.undo.perfumeId;
      next.currentReason = state.undo.reasonBefore || null;
      next.undo = null;
    }
    next.revision++;
    const applied = await saveState(state.revision, next, env);
    return reply(await enrichResponse({ ...stateResponse(applied ? next : await ensureCurrent(env, catalogue), catalogue, seed), applied }, env));
  } catch (error) {
    return error instanceof HttpError ? reply({ error: error.code }, error.status) : reply({ error: 'temporarily_unavailable' }, 503);
  }
}
