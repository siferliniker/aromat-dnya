// The small UI files are embedded by cloud/build.mjs. Original raster files
// live in the Sites-provisioned R2 binding so they do not consume Worker code quota.
const SECURITY_HEADERS = {
  "Content-Security-Policy": "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; img-src 'self' https://www.acquadiparma.com https://us.afnan.com https://acquadiparma.jp https://scentgrail.com https://armaf.com https://us.alharamainperfumes.com; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors https://web.telegram.org https://*.web.telegram.org",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
};

export function secureResponse(response, request) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) headers.set(name, value);
  headers.delete("Access-Control-Allow-Origin");
  headers.delete("Access-Control-Allow-Credentials");
  if (!headers.has("Cache-Control")) headers.set("Cache-Control", "no-store");
  return new Response(request.method === "HEAD" ? null : response.body, {
    status: response.status, statusText: response.statusText, headers,
  });
}

function assetError(status, message) {
  return new Response(message, { status, headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" } });
}

export async function serveAsset(request, env, assets) {
  const path = new URL(request.url).pathname;
  const name = path === "/" ? "index.html" : path.startsWith("/static/") ? path.slice(8) : null;
  const asset = name === null ? null : assets[name];
  if (!asset || !Object.hasOwn(assets, name)) return null;
  if (!["GET", "HEAD"].includes(request.method)) {
    return new Response(null, { status: 405, headers: { Allow: "GET, HEAD", "Cache-Control": "no-store" } });
  }
  let body = asset.text;
  if (asset.key) {
    if (!env.BUCKET) return assetError(503, "Assets storage is unavailable");
    const object = request.method === "HEAD" ? await env.BUCKET.head(asset.key) : await env.BUCKET.get(asset.key);
    body = object ? object.body : await hydrateAsset(env, asset);
  }
  const headers = new Headers({
    "Content-Type": asset.type,
    "ETag": `"${asset.sha256}"`,
    "Cache-Control": name === "index.html" ? "no-store" : "public, max-age=3600, must-revalidate",
  });
  const matches = request.headers.get("If-None-Match")?.split(",").map(value => value.trim());
  if (matches?.includes(`"${asset.sha256}"`) || matches?.includes(`W/"${asset.sha256}"`) || matches?.includes("*")) {
    if (body?.cancel) await body.cancel();
    return new Response(null, { status: 304, headers });
  }
  headers.set("Content-Length", String(asset.size));
  return new Response(request.method === "HEAD" ? null : body, { headers });
}

const pendingAssets = new Map();

// The build pins a public repository commit and exact original PNG digest.
// Visitors cannot provide URLs or upload bytes. A missing R2 object repairs itself.
async function hydrateAsset(env, asset) {
  if (pendingAssets.has(asset.key)) return pendingAssets.get(asset.key);
  const pending = (async () => {
    const controller = new AbortController();
    const deadline = setTimeout(() => controller.abort(), 10_000);
    let stage = "source-fetch";
    try {
      const response = await fetch(asset.source, { signal: controller.signal, redirect: "error" });
      if (!response.ok || !response.body) throw new Error("AssetSourceUnavailable");
      const length = response.headers.get("Content-Length");
      if (length !== null && Number(length) !== asset.size) {
        await response.body.cancel();
        throw new Error("AssetSizeMismatch");
      }
      stage = "source-read";
      const reader = response.body.getReader();
      const bytes = new Uint8Array(asset.size);
      let offset = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        if (offset + value.byteLength > asset.size) {
          await reader.cancel();
          throw new Error("AssetSizeMismatch");
        }
        bytes.set(value, offset);
        offset += value.byteLength;
      }
      if (offset !== asset.size) throw new Error("AssetSizeMismatch");
      stage = "digest";
      const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
      const hash = Array.from(digest, value => value.toString(16).padStart(2, "0")).join("");
      if (hash !== asset.sha256) throw new Error("AssetDigestMismatch");
      stage = "storage-write";
      await env.BUCKET.put(asset.key, bytes, {
        httpMetadata: { contentType: asset.type, cacheControl: "public, max-age=31536000, immutable" },
        customMetadata: { sha256: hash },
      });
      return bytes;
    } catch (error) {
      // Fixed phase labels diagnose hosting failures without logging URLs or payload.
      const failure = new Error("AssetUnavailable");
      failure.assetStage = stage;
      failure.causeType = error?.name || "Error";
      throw failure;
    } finally {
      clearTimeout(deadline);
    }
  })();
  pendingAssets.set(asset.key, pending);
  try { return await pending; } finally { pendingAssets.delete(asset.key); }
}

export async function warmAssets(env, assets) {
  if (!env.BUCKET) return;
  const results = await Promise.allSettled(Object.values(assets).filter(asset => asset.key).map(async asset => {
    let stored;
    try { stored = await env.BUCKET.head(asset.key); }
    catch (error) {
      const failure = new Error("AssetStorageUnavailable");
      failure.assetStage = "storage-head";
      failure.causeType = error?.name || "Error";
      throw failure;
    }
    if (!stored) await hydrateAsset(env, asset);
  }));
  for (const result of results) {
    if (result.status === "rejected") console.error("Asset warm failed", result.reason?.assetStage || "unknown", result.reason?.causeType || result.reason?.name || "Error");
  }
}
