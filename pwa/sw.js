const CACHE_NAME = 'oxe-v2';
const DB_NAME = 'oxe-offline';
const DB_VERSION = 1;

// Shell pages to pre-cache on install
const SHELL = [
  '/',
  '/search',
  '/train',
  '/drill',
  '/library',
  '/conversa',
  '/plan',
  '/chunks',
  '/assembly',
  '/shadowing',
  '/speech',
];

// ── IndexedDB helpers ──────────────────────────────────

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = e => {
      const db = e.target.result;
      if (!db.objectStoreNames.contains('drill_items')) {
        db.createObjectStore('drill_items', { keyPath: 'chunk_id' });
      }
      if (!db.objectStoreNames.contains('pending_reviews')) {
        db.createObjectStore('pending_reviews', { autoIncrement: true });
      }
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

// ── Install ────────────────────────────────────────────

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL))
  );
  self.skipWaiting();
});

// ── Activate ───────────────────────────────────────────

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// ── Offline drill: get next item from IndexedDB ────────

async function offlineDrillNext() {
  const items = await idbGetAll('drill_items');
  if (!items.length) {
    return new Response(JSON.stringify({ error: 'Nenhum chunk offline' }), {
      status: 404, headers: { 'Content-Type': 'application/json' }
    });
  }
  // Pick first item (already sorted by due date from server)
  const item = items[0];
  const remaining = items.length;
  return new Response(JSON.stringify({
    chunk_id: item.chunk_id,
    word: item.word,
    word_id: item.word_id,
    target_chunk: item.target_chunk,
    carrier_sentence: item.carrier_sentence,
    current_pass: item.current_pass,
    audio_file: '_offline_audio_' + item.chunk_id,
    image_file: item.image_b64 ? ('_offline_image_' + item.chunk_id) : null,
    tier: item.tier,
    due_count: remaining,
    offline: true,
  }), { headers: { 'Content-Type': 'application/json' } });
}

// ── Offline drill: complete review → queue for sync ────

async function offlineDrillComplete(body) {
  const chunkId = body.chunk_id;
  // Queue the review for later sync
  await idbPut('pending_reviews', {
    chunk_id: chunkId,
    rating: body.rating || 3,
    latency_ms: body.latency_ms,
    biometric_score: body.biometric_score || null,
    timestamp: new Date().toISOString(),
  });
  // Remove from offline drill queue
  await idbDelete('drill_items', chunkId);
  const remaining = await idbCount('drill_items');
  return new Response(JSON.stringify({
    rating: body.rating || 3,
    rating_name: ['', 'De novo', 'Difícil', 'Bom', 'Fácil'][body.rating || 3],
    new_mastery: 0,
    latency_downgraded: false,
    offline: true,
    remaining: remaining,
  }), { headers: { 'Content-Type': 'application/json' } });
}

// ── Offline drill: explain from pre-cached data ────────

async function offlineDrillExplain(url) {
  const word = new URL(url).searchParams.get('word') || '';
  const items = await idbGetAll('drill_items');
  const item = items.find(i => i.target_chunk === word || i.word === word);
  if (item && item.explain_text) {
    return new Response(JSON.stringify({
      explanation: item.explain_text,
      audio_file: item.explain_audio_b64 ? ('_offline_explain_' + item.chunk_id) : null,
      offline: true,
    }), { headers: { 'Content-Type': 'application/json' } });
  }
  return new Response(JSON.stringify({ explanation: '', audio_file: null }), {
    headers: { 'Content-Type': 'application/json' }
  });
}

// ── Offline audio/image from IndexedDB blobs ───────────

async function offlineAsset(pathname) {
  // Pattern: _offline_audio_<chunk_id>, _offline_image_<chunk_id>, _offline_explain_<chunk_id>
  const match = pathname.match(/\/_offline_(audio|image|explain)_(\d+)/);
  if (!match) return null;
  const [, type, id] = match;
  const item = await idbGet('drill_items', parseInt(id));
  // Item might already be deleted after review — check pending too
  if (!item) {
    // Try to find in a temporary cache of served items
    return null;
  }
  if (type === 'audio' && item.audio_b64) {
    return new Response(b64toBlob(item.audio_b64, 'audio/mpeg'), {
      headers: { 'Content-Type': 'audio/mpeg' }
    });
  }
  if (type === 'image' && item.image_b64) {
    return new Response(b64toBlob(item.image_b64, 'image/png'), {
      headers: { 'Content-Type': 'image/png' }
    });
  }
  if (type === 'explain' && item.explain_audio_b64) {
    return new Response(b64toBlob(item.explain_audio_b64, 'audio/mpeg'), {
      headers: { 'Content-Type': 'audio/mpeg' }
    });
  }
  return null;
}

// ── Fetch handler ──────────────────────────────────────

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const path = url.pathname;

  // Skip non-GET for caching (but intercept POST for offline drill)
  if (e.request.method === 'POST') {
    // Offline drill complete
    if (path === '/api/drill/complete') {
      e.respondWith(
        fetch(e.request.clone()).catch(() =>
          e.request.clone().json().then(body => offlineDrillComplete(body))
        )
      );
      return;
    }
    // Offline drill explain (POST version)
    if (path === '/api/drill/explain') {
      e.respondWith(
        fetch(e.request.clone()).catch(() =>
          e.request.clone().json().then(body => offlineDrillExplain(url.href + '?word=' + (body.word || '')))
        )
      );
      return;
    }
    return;
  }

  // Offline audio/image assets (virtual URLs from IndexedDB)
  if (path.startsWith('/_offline_')) {
    e.respondWith(offlineAsset(path).then(r => r || new Response('', { status: 404 })));
    return;
  }

  // Drill API: network-first, offline fallback to IndexedDB
  if (path === '/api/drill/next') {
    e.respondWith(
      fetch(e.request).then(resp => {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
        return resp;
      }).catch(() => offlineDrillNext())
    );
    return;
  }

  if (path.startsWith('/api/drill/explain')) {
    e.respondWith(
      fetch(e.request).then(resp => {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
        return resp;
      }).catch(() => offlineDrillExplain(e.request.url))
    );
    return;
  }

  // Other API: network-first with cache fallback
  if (path.startsWith('/api/')) {
    e.respondWith(
      fetch(e.request).then(resp => {
        const clone = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
        return resp;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // Audio & images: cache-first, then network
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

  // HTML pages: network-first, cache fallback
  e.respondWith(
    fetch(e.request).then(resp => {
      const clone = resp.clone();
      caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
      return resp;
    }).catch(() => caches.match(e.request))
  );
});

// ── Background Sync: upload pending reviews when online ─

self.addEventListener('sync', e => {
  if (e.tag === 'sync-reviews') {
    e.waitUntil(uploadPendingReviews());
  }
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
      // Notify all clients
      const clients = await self.clients.matchAll();
      clients.forEach(c => c.postMessage({ type: 'sync-complete', count: reviews.length }));
    }
  } catch (e) {
    // Will retry on next sync event
  }
}

// ── Message handler for manual sync trigger ────────────

self.addEventListener('message', e => {
  if (e.data === 'sync-reviews') {
    uploadPendingReviews();
  }
});
