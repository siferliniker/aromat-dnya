// The existing backend owns encryption. This adapter stores its ciphertext and
// quota metadata in one PRIVATE Blob, with an ETag compare-and-swap per write.
const PATH = 'aromatia/state-v1.json';
const MAX_BYTES = 1024 * 1024;
const copy = value => value == null ? value : JSON.parse(JSON.stringify(value));
const blank = () => ({ version: 1, state: null, cache: {}, guard: null });
const normalize = sql => sql.replace(/\s+/g, ' ').trim().toLowerCase();
const conflict = error => ['BlobPreconditionFailedError', 'BlobAlreadyExistsError'].some(name =>
  error?.name === name || error?.constructor?.name === name);

export function createBlobDatabase({ get, put }) {
  async function read() {
    // Brotli delivery returns a weak W/ ETag, which If-Match correctly rejects.
    // Read the identity representation so the returned strong ETag identifies
    // exactly the bytes being updated; never strip W/ from a weak validator.
    const result = await get(PATH, { access: 'private', useCache: false, headers: { 'accept-encoding': 'identity' } });
    if (result === null) return { document: blank(), etag: null };
    if (result.statusCode !== 200 || !result.stream || !result.blob?.etag || result.blob.etag.startsWith('W/')) throw new Error('StorageReadFailed');
    const reader = result.stream.getReader();
    const chunks = []; let size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_BYTES) { await reader.cancel(); throw new Error('StorageSizeLimit'); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    const document = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
    if (document?.version !== 1 || !Object.hasOwn(document, 'state') || !Object.hasOwn(document, 'guard') ||
        !document.cache || typeof document.cache !== 'object' || Array.isArray(document.cache) || Object.keys(document.cache).length > 64) {
      throw new Error('StorageFormatInvalid');
    }
    return { document, etag: result.blob.etag };
  }

  function select(sql, values, document) {
    if (sql === 'select revision, ciphertext from private_state where id = ?') {
      return document.state?.id === values[0] ? [{ revision: document.state.revision, ciphertext: document.state.ciphertext }] : [];
    }
    if (sql === 'select key, ciphertext, expires from performance_cache where expires > ? order by expires desc limit 64') {
      return Object.values(document.cache).filter(row => row.expires > values[0]).sort((a, b) => b.expires - a.expires).slice(0, 64);
    }
    if (sql === "select searches, blocked_until from performance_guard where id = 'fragella'") {
      return document.guard ? [{ searches: document.guard.searches, blocked_until: document.guard.blocked_until }] : [];
    }
    throw new Error('UnsupportedStorageQuery');
  }

  function mutate(sql, p, document) {
    if (sql === 'insert or ignore into private_state (id, revision, ciphertext, updated_at) values (?, ?, ?, ?)') {
      if (document.state) return 0;
      if (p[0] !== 'owner' || !Number.isSafeInteger(p[1]) || p[1] < 0 || !p[2]?.startsWith('v1.')) throw new Error('InvalidStorageWrite');
      document.state = { id: p[0], revision: p[1], ciphertext: p[2], updated_at: p[3] }; return 1;
    }
    if (sql === 'update private_state set revision = ?, ciphertext = ?, updated_at = ? where id = ? and revision = ?') {
      if (!document.state || document.state.id !== p[3] || document.state.revision !== p[4]) return 0;
      if (!Number.isSafeInteger(p[0]) || p[0] < 0 || !p[1]?.startsWith('v1.')) throw new Error('InvalidStorageWrite');
      document.state = { id: p[3], revision: p[0], ciphertext: p[1], updated_at: p[2] }; return 1;
    }
    const upsert = 'insert into performance_cache (key, ciphertext, expires) values (?, ?, ?) on conflict(key) do update set ciphertext = excluded.ciphertext, expires = excluded.expires';
    if (sql === upsert || sql === upsert + ' where performance_cache.expires <= ?') {
      if (!/^[a-f0-9]{64}$/.test(p[0]) || !p[1]?.startsWith('v1.') || !Number.isSafeInteger(p[2])) throw new Error('InvalidStorageWrite');
      if (sql !== upsert && document.cache[p[0]] && document.cache[p[0]].expires > p[3]) return 0;
      document.cache[p[0]] = { key: p[0], ciphertext: p[1], expires: p[2] };
      // Preserve the new lease/result while keeping the store bounded.
      const older = Object.values(document.cache).filter(row => row.key !== p[0]).sort((a, b) => b.expires - a.expires).slice(0, 63);
      document.cache = Object.fromEntries([document.cache[p[0]], ...older].map(row => [row.key, row])); return 1;
    }
    if (sql === 'delete from performance_cache where key not in (select key from performance_cache order by expires desc limit 64)') return 0;
    if (sql === "insert or ignore into performance_guard (id, day, searches, blocked_until, lease_until, lease_token) values ('fragella', '', 0, 0, 0, '')") {
      if (document.guard) return 0;
      document.guard = { day: '', searches: 0, blocked_until: 0, lease_until: 0, lease_token: '' }; return 1;
    }
    const g = document.guard;
    if (sql === "update performance_guard set day = ?, searches = 0 where id = 'fragella' and day <> ?") {
      if (!g || g.day === p[1]) return 0; g.day = p[0]; g.searches = 0; return 1;
    }
    if (sql === "update performance_guard set blocked_until = ? where id = 'fragella'") {
      if (!g) return 0; g.blocked_until = p[0]; return 1;
    }
    if (sql === "update performance_guard set lease_until = ?, lease_token = ? where id = 'fragella' and lease_until <= ?") {
      if (!g || g.lease_until > p[2]) return 0; g.lease_until = p[0]; g.lease_token = p[1]; return 1;
    }
    if (sql === "update performance_guard set searches = searches + 1 where id = 'fragella' and day = ? and searches < 20 and blocked_until <= ?") {
      if (!g || g.day !== p[0] || g.searches >= 20 || g.blocked_until > p[1]) return 0; g.searches++; return 1;
    }
    if (sql === "update performance_guard set lease_until = 0, lease_token = '' where id = 'fragella' and lease_token = ?") {
      if (!g || g.lease_token !== p[0]) return 0; g.lease_until = 0; g.lease_token = ''; return 1;
    }
    throw new Error('UnsupportedStorageQuery');
  }

  return {
    prepare(query) {
      const sql = normalize(query);
      const statement = values => ({
        bind(...next) { return statement(next); },
        async first() { return copy(select(sql, values, (await read()).document)[0] ?? null); },
        async all() { return { results: copy(select(sql, values, (await read()).document)) }; },
        async run() {
          for (let attempt = 0; attempt < 6; attempt++) {
            const { document, etag } = await read();
            const changes = mutate(sql, values, document);
            if (!changes) return { meta: { changes: 0 } };
            const content = JSON.stringify(document);
            if (new TextEncoder().encode(content).length > MAX_BYTES) throw new Error('StorageSizeLimit');
            try {
              await put(PATH, content, { access: 'private', contentType: 'application/json', addRandomSuffix: false,
                cacheControlMaxAge: 60, allowOverwrite: etag !== null, ...(etag === null ? {} : { ifMatch: etag }) });
              return { meta: { changes } };
            } catch (error) {
              if (!conflict(error)) {
                // The SDK reports a create collision as BlobError on some API
                // versions. Re-read after a failed create, but never overwrite
                // an existing blob without the newly obtained ETag.
                if (etag !== null || (await read()).etag === null) throw error;
              }
            }
          }
          throw new Error('StorageContention');
        },
      });
      return statement([]);
    },
  };
}
