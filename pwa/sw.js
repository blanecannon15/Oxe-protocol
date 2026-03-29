const CACHE_NAME = 'oxe-v21';
const DB_NAME = 'oxe-offline';
const DB_VERSION = 2;

const SHELL = [
  '/', '/search', '/train', '/drill', '/library',
  '/conversa', '/plan', '/chunks', '/assembly',
  '/shadowing', '/speech', '/stories',
];

// ── IndexedDB ──────────────────────────────────────────

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      const stores = ['drill_items', 'pending_reviews', 'stories', 'dictionary', 'stats', 'api_cache'];
      stores.forEach(name => {
        if (!db.objectStoreNames.contains(name)) {
          if (name === 'drill_items') db.createObjectStore(name, { keyPath: 'chunk_id' });
          else if (name === 'pending_reviews') db.createObjectStore(name, { autoIncrement: true });
          else if (name === 'stories') db.createObjectStore(name, { keyPath: 'id' });
          else if (name === 'dictionary') db.createObjectStore(name, { keyPath: 'word_id' });
          else if (name === 'stats') db.createObjectStore(name, { keyPath: 'key' });
          else if (name === 'api_cache') db.createObjectStore(name, { keyPath: 'url' });
        }
      });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function idbGet(store, key) {
  return openDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readonly');
    const req = tx.objectStore(store).get(key);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  }));
}

function idbGetAll(store) {
  return openDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readonly');
    const req = tx.objectStore(store).getAll();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  }));
}

function idbPut(store, value) {
  return openDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readwrite');
    const req = tx.objectStore(store).put(value);
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  }));
}

function idbDelete(store, key) {
  return openDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readwrite');
    const req = tx.objectStore(store).delete(key);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  }));
}

function idbClear(store) {
  return openDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readwrite');
    const req = tx.objectStore(store).clear();
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  }));
}

function idbCount(store) {
  return openDB().then(db => new Promise((resolve, reject) => {
    const tx = db.transaction(store, 'readonly');
    const req = tx.objectStore(store).count();
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  }));
}

function b64toBlob(b64, mime) {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  return new Blob([arr], { type: mime });
}

function jsonResponse(data, status) {
  return new Response(JSON.stringify(data), {
    status: status || 200,
    headers: { 'Content-Type': 'application/json' }
  });
}

// ── Install & Activate ─────────────────────────────────

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── Offline API handlers ───────────────────────────────

async function offlineDrillNext() {
  const items = await idbGetAll('drill_items');
  if (!items.length) return jsonResponse({ error: 'Nenhum chunk offline' }, 404);
  const item = items[0];
  return jsonResponse({
    chunk_id: item.chunk_id, word: item.word, word_id: item.word_id,
    target_chunk: item.target_chunk, carrier_sentence: item.carrier_sentence,
    current_pass: item.current_pass, tier: item.tier,
    audio_file: '_offline_audio_' + item.chunk_id,
    image_file: item.image_b64 ? '_offline_image_' + item.chunk_id : null,
    due_count: items.length, offline: true,
  });
}

async function offlineDrillComplete(body) {
  await idbPut('pending_reviews', {
    chunk_id: body.chunk_id, rating: body.rating || 3,
    latency_ms: body.latency_ms, biometric_score: body.biometric_score || null,
    timestamp: new Date().toISOString(),
  });
  await idbDelete('drill_items', body.chunk_id);
  const remaining = await idbCount('drill_items');
  return jsonResponse({
    rating: body.rating || 3,
    rating_name: ['', 'De novo', 'Difícil', 'Bom', 'Fácil'][body.rating || 3],
    new_mastery: 0, latency_downgraded: false, offline: true, remaining: remaining,
  });
}

async function offlineDrillExplain(url) {
  const word = new URL(url).searchParams.get('word') || '';
  const items = await idbGetAll('drill_items');
  const item = items.find(i => i.target_chunk === word || i.word === word);
  if (item && item.explain_text) {
    return jsonResponse({
      explanation: item.explain_text,
      audio_file: item.explain_audio_b64 ? '_offline_explain_' + item.chunk_id : null,
      offline: true,
    });
  }
  return jsonResponse({ explanation: '', audio_file: null });
}

async function offlineStoryList(level) {
  const stories = await idbGetAll('stories');
  const filtered = level ? stories.filter(s => s.level === level) : stories;
  return jsonResponse(filtered.map(s => ({
    id: s.id, title: s.title, level: s.level,
    has_audio: !!(s.audio_chunks && s.audio_chunks.story_chunks && s.audio_chunks.story_chunks.length),
  })));
}

async function offlineStory(id) {
  const story = await idbGet('stories', parseInt(id));
  if (!story) return jsonResponse({ error: 'not found' }, 404);
  return jsonResponse(story);
}

async function offlineLevels() {
  const stats = await idbGet('stats', 'levels');
  return stats ? jsonResponse(stats.data) : jsonResponse([]);
}

async function offlineHomeStats() {
  const stats = await idbGet('stats', 'home_stats');
  return stats ? jsonResponse(stats.data) : jsonResponse({});
}

async function offlineDashboard() {
  const stats = await idbGet('stats', 'dashboard');
  return stats ? jsonResponse(stats.data) : jsonResponse({});
}

async function offlineSpeechStage() {
  const stats = await idbGet('stats', 'speech_stage');
  return stats ? jsonResponse(stats.data) : jsonResponse({ stage: 1 });
}

async function offlineWordData(wordId) {
  const word = await idbGet('dictionary', parseInt(wordId));
  if (!word) return null;
  return word;
}

async function offlineSearch(query) {
  if (!query) return jsonResponse([]);
  const all = await idbGetAll('dictionary');
  const q = query.toLowerCase();
  const matches = all.filter(w => w.word.toLowerCase().startsWith(q)).slice(0, 20);
  return jsonResponse(matches.map(w => ({
    id: w.word_id, word: w.word, frequency_rank: 0, difficulty_tier: 1,
  })));
}

async function offlineWordTab(wordId, tab) {
  const word = await idbGet('dictionary', parseInt(wordId));
  if (!word || !word.tabs || !word.tabs[tab]) return jsonResponse({});
  return jsonResponse(word.tabs[tab]);
}

async function offlineAsset(pathname) {
  const match = pathname.match(/\/_offline_(audio|image|explain)_(\d+)/);
  if (!match) return null;
  const [, type, id] = match;
  const item = await idbGet('drill_items', parseInt(id));
  if (!item) return null;
  if (type === 'audio' && item.audio_b64)
    return new Response(b64toBlob(item.audio_b64, 'audio/mpeg'), { headers: { 'Content-Type': 'audio/mpeg' } });
  if (type === 'image' && item.image_b64)
    return new Response(b64toBlob(item.image_b64, 'image/png'), { headers: { 'Content-Type': 'image/png' } });
  if (type === 'explain' && item.explain_audio_b64)
    return new Response(b64toBlob(item.explain_audio_b64, 'audio/mpeg'), { headers: { 'Content-Type': 'audio/mpeg' } });
  return null;
}

async function offlineApiCache(url) {
  const cached = await idbGet('api_cache', url);
  if (cached) return jsonResponse(cached.data);
  return null;
}

// ── Fetch handler ──────────────────────────────────────

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const path = url.pathname;

  // ── POST requests ──
  if (e.request.method === 'POST') {
    if (path === '/api/drill/complete') {
      e.respondWith(
        fetch(e.request.clone()).catch(() =>
          e.request.clone().json().then(body => offlineDrillComplete(body))
        )
      );
      return;
    }
    if (path === '/api/drill/explain') {
      e.respondWith(
        fetch(e.request.clone()).catch(() =>
          e.request.clone().json().then(body => offlineDrillExplain(url.href + '?word=' + (body.word || '')))
        )
      );
      return;
    }
    // Other POSTs: queue for later if offline
    if (path.startsWith('/api/')) {
      e.respondWith(
        fetch(e.request.clone()).catch(() => jsonResponse({ offline: true, queued: true }))
      );
      return;
    }
    return;
  }

  // ── Offline virtual assets ──
  if (path.startsWith('/_offline_')) {
    e.respondWith(offlineAsset(path).then(r => r || new Response('', { status: 404 })));
    return;
  }

  // ── GET API routing: network-first, offline fallback ──
  if (path.startsWith('/api/')) {
    e.respondWith((async () => {
      // Try network first
      try {
        const resp = await fetch(e.request);
        // Cache successful GET responses
        const clone = resp.clone();
        const data = await clone.json().catch(() => null);
        if (data) {
          idbPut('api_cache', { url: e.request.url, data: data, ts: Date.now() }).catch(() => {});
        }
        return resp;
      } catch (err) {
        // Offline — route to specific handlers
        if (path === '/api/drill/next') return offlineDrillNext();
        if (path.startsWith('/api/drill/explain')) return offlineDrillExplain(e.request.url);
        if (path === '/api/home-stats') return offlineHomeStats();
        if (path === '/api/dashboard') return offlineDashboard();
        if (path === '/api/speech/stage') return offlineSpeechStage();
        if (path === '/api/levels') return offlineLevels();
        if (path.match(/^\/api\/stories/)) {
          const level = url.searchParams.get('level');
          return offlineStoryList(level);
        }
        if (path.match(/^\/api\/story\/\d+$/)) {
          const id = path.split('/')[3];
          return offlineStory(id);
        }
        if (path === '/api/search') {
          const q = url.searchParams.get('q');
          return offlineSearch(q);
        }
        if (path.match(/^\/api\/word\/\d+\/(definition|examples|pronunciation|expressions|conjugation|synonyms|chunks)$/)) {
          const parts = path.split('/');
          return offlineWordTab(parts[3], parts[4]);
        }
        if (path.match(/^\/api\/search\/word\/\d+$/)) {
          const wid = parseInt(path.split('/')[4]);
          const word = await offlineWordData(wid);
          if (word) return jsonResponse({ word: word.word, word_id: word.word_id, difficulty_tier: 1 });
          return jsonResponse({}, 404);
        }
        // Generic: try api_cache
        const cached = await offlineApiCache(e.request.url);
        if (cached) return cached;
        return jsonResponse({ offline: true, error: 'no cached data' }, 503);
      }
    })());
    return;
  }

  // ── Audio & images: cache-first ──
  if (path.startsWith('/audio/') || path.startsWith('/image/')) {
    e.respondWith(
      caches.match(e.request).then(cached => {
        if (cached) return cached;
        return fetch(e.request).then(resp => {
          const clone = resp.clone();
          caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
          return resp;
        }).catch(() => new Response('', { status: 404 }));
      })
    );
    return;
  }

  // ── HTML pages: network-first, cache fallback ──
  e.respondWith(
    fetch(e.request).then(resp => {
      const clone = resp.clone();
      caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
      return resp;
    }).catch(() => caches.match(e.request))
  );
});

// ── Background Sync ────────────────────────────────────

self.addEventListener('sync', e => {
  if (e.tag === 'sync-reviews') e.waitUntil(uploadPendingReviews());
});

async function uploadPendingReviews() {
  const reviews = await idbGetAll('pending_reviews');
  if (!reviews.length) return;
  try {
    const resp = await fetch('/api/sync/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reviews }),
    });
    if (resp.ok) {
      await idbClear('pending_reviews');
      const clients = await self.clients.matchAll();
      clients.forEach(c => c.postMessage({ type: 'sync-complete', count: reviews.length }));
    }
  } catch (e) { /* retry on next sync */ }
}

self.addEventListener('message', e => {
  if (e.data === 'sync-reviews') uploadPendingReviews();
});
