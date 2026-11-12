import { mkdir, readFile, writeFile, copyFile, readdir, lstat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const destination = path.join(root, '.local/vercel/aromatia');
await mkdir(path.join(destination, 'api'), { recursive: true });
await mkdir(path.join(destination, 'lib'), { recursive: true });
await mkdir(path.join(destination, 'static'), { recursive: true });
for (const name of await readdir(path.join(root, 'miniapp/static'))) {
  const source = path.join(root, 'miniapp/static', name);
  const stat = await lstat(source);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Unexpected static asset');
  await copyFile(source, path.join(destination, name === 'index.html' ? name : 'static/' + name));
}
await copyFile(path.join(root, 'cloud/vercel/api.mjs'), path.join(destination, 'api/index.mjs'));
await copyFile(path.join(root, 'cloud/vercel/blob-db.mjs'), path.join(destination, 'lib/blob-db.mjs'));
await copyFile(path.join(root, 'cloud/worker/backend.mjs'), path.join(destination, 'lib/backend.mjs'));
await copyFile(path.join(root, 'cloud/worker/assets.mjs'), path.join(destination, 'lib/security.mjs'));
await copyFile(path.join(root, 'cloud/catalogue.json'), path.join(destination, 'lib/catalogue.json'));
const security = await import('../worker/assets.mjs');
const headers = Object.fromEntries(security.secureResponse(new Response(''), new Request('https://example.test/')).headers);
delete headers['content-type'];
await writeFile(path.join(destination, 'package.json'), JSON.stringify({ name:'aromatia-miniapp', private:true, type:'module',
  engines:{node:'24.x'}, dependencies:{'@vercel/blob':'2.9.0'} }, null, 2) + '\n');
await writeFile(path.join(destination, 'vercel.json'), JSON.stringify({
  functions: {'api/index.mjs': {maxDuration:30}},
  rewrites: [{source:'/api/:path*',destination:'/api/index?__aromatia_route=:path*'}, {source:'/health',destination:'/api/index'}],
  headers: [{source:'/(.*)',headers:Object.entries(headers).map(([key,value])=>({key,value}))}],
}, null, 2) + '\n');
await writeFile(path.join(destination, '.gitignore'), '.vercel/\nnode_modules/\n.env*\n');
await writeFile(path.join(destination, '.vercelignore'), '.env*\nnode_modules/\n.vercel/\n');
const lock = path.join(root, 'cloud/vercel/package-lock.json');
if (await lstat(lock).catch(() => null)) await copyFile(lock, path.join(destination, 'package-lock.json'));
console.log('Vercel package ready at ' + destination);
