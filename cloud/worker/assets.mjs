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
    if (!object) return assetError(503, "Asset is awaiting initialization");
    body = object.body;
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

function tokenMatches(actual, expected) {
  if (typeof expected !== "string" || expected.length < 32 || actual.length !== expected.length) return false;
  let difference = 0;
  for (let index = 0; index < actual.length; index++) difference |= actual.charCodeAt(index) ^ expected.charCodeAt(index);
  return difference === 0;
}

// Provision only exact build-time public raster bytes. Disable after initial
// upload by removing ASSET_UPLOAD_TOKEN from runtime secrets.
export async function bootstrapAsset(request, env, assets) {
  const path = new URL(request.url).pathname;
  if (!path.startsWith("/_bootstrap/assets/")) return null;
  const authorization = request.headers.get("Authorization") || "";
  if (!authorization.startsWith("Bearer ") || !tokenMatches(authorization.slice(7), env.ASSET_UPLOAD_TOKEN)) {
    return assetError(404, "Not found");
  }
  if (request.method !== "PUT") return assetError(405, "PUT required");
  const name = path.slice("/_bootstrap/assets/".length);
  const asset = Object.hasOwn(assets, name) ? assets[name] : null;
  if (!asset?.key) return assetError(404, "Not found");
  if (!env.BUCKET) return assetError(503, "Assets storage is unavailable");
  const length = request.headers.get("Content-Length");
  if (length !== null && Number(length) !== asset.size) return assetError(400, "Invalid asset size");
  if (!request.body) return assetError(400, "Asset bytes required");
  const reader = request.body.getReader();
  const bytes = new Uint8Array(asset.size);
  let offset = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    if (offset + value.byteLength > asset.size) {
      await reader.cancel();
      return assetError(413, "Invalid asset size");
    }
    bytes.set(value, offset);
    offset += value.byteLength;
  }
  if (offset !== asset.size) return assetError(400, "Invalid asset size");
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  const hash = Array.from(digest, value => value.toString(16).padStart(2, "0")).join("");
  if (hash !== asset.sha256) return assetError(400, "Invalid asset digest");
  await env.BUCKET.put(asset.key, bytes, {
    httpMetadata: { contentType: asset.type, cacheControl: "public, max-age=31536000, immutable" },
    customMetadata: { sha256: hash },
  });
  return Response.json({ status: "stored", name, sha256: hash }, { headers: { "Cache-Control": "no-store" } });
}
