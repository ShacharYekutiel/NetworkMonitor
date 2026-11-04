// Copies the built SPA (frontend/dist) into backend/netmon/static for packaging.
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const dist = resolve(here, '..', 'dist');
const target = resolve(here, '..', '..', 'backend', 'netmon', 'static');

if (!existsSync(resolve(dist, 'index.html'))) {
  console.error('frontend/dist/index.html not found - run `npm run build` first');
  process.exit(1);
}
// Empty the target but keep the tracked .gitkeep (the directory is in git, its content is not).
mkdirSync(target, { recursive: true });
for (const entry of readdirSync(target)) {
  if (entry === '.gitkeep') continue;
  rmSync(resolve(target, entry), { recursive: true, force: true });
}
cpSync(dist, target, { recursive: true });
if (!existsSync(resolve(target, '.gitkeep'))) writeFileSync(resolve(target, '.gitkeep'), '');
console.log(`Copied ${dist} -> ${target}`);
