/**
 * Compare two screenshot runs from scripts/capture.mjs.
 *
 *   npm run shotdiff -- <before> <after>
 *
 * <before>/<after> are stamp dir names under screenshots/ (or paths).
 * Prints per-file changed-pixel % for every same-named PNG, flags files over
 * the review threshold, and lists files present in only one run. Exits 0
 * always — this is review evidence, not a gate; the changed-file list goes
 * in the commit body per docs/design-language.md §Governance.
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { PNG } from 'pngjs';
import pixelmatch from 'pixelmatch';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const THRESHOLD_PCT = 0.1; // over this, a file lands in the "review" list

function resolveRun(arg) {
  if (!arg) return null;
  for (const p of [arg, join(ROOT, 'screenshots', arg)]) {
    if (existsSync(p)) return p;
  }
  return null;
}

const [beforeArg, afterArg] = process.argv.slice(2);
const before = resolveRun(beforeArg);
const after = resolveRun(afterArg);
if (!before || !after) {
  const runs = existsSync(join(ROOT, 'screenshots'))
    ? readdirSync(join(ROOT, 'screenshots')).sort()
    : [];
  console.error('usage: npm run shotdiff -- <before> <after>');
  console.error(`available runs: ${runs.join(', ') || '(none)'}`);
  process.exit(2);
}

/** Decode a PNG, padded onto a w×h canvas (full-page heights drift when
 *  content reflows; padding keeps pixelmatch comparable). */
function decodePadded(path, w, h) {
  const src = PNG.sync.read(readFileSync(path));
  if (src.width === w && src.height === h) return src;
  const out = new PNG({ width: w, height: h });
  PNG.bitblt(src, out, 0, 0, Math.min(src.width, w), Math.min(src.height, h), 0, 0);
  return out;
}

const beforeFiles = new Set(readdirSync(before).filter((f) => f.endsWith('.png')));
const afterFiles = new Set(readdirSync(after).filter((f) => f.endsWith('.png')));
const shared = [...beforeFiles].filter((f) => afterFiles.has(f)).sort();

const results = [];
for (const file of shared) {
  const a = PNG.sync.read(readFileSync(join(before, file)));
  const b = PNG.sync.read(readFileSync(join(after, file)));
  const w = Math.max(a.width, b.width);
  const h = Math.max(a.height, b.height);
  const pa = a.width === w && a.height === h ? a : decodePadded(join(before, file), w, h);
  const pb = b.width === w && b.height === h ? b : decodePadded(join(after, file), w, h);
  const changed = pixelmatch(pa.data, pb.data, null, w, h, { threshold: 0.1 });
  const pct = (changed / (w * h)) * 100;
  const resized = a.width !== b.width || a.height !== b.height;
  results.push({ file, pct, resized });
}

results.sort((x, y) => y.pct - x.pct);
const flagged = results.filter((r) => r.pct > THRESHOLD_PCT || r.resized);
const clean = results.length - flagged.length;

for (const r of flagged) {
  console.log(
    `${r.pct.toFixed(3).padStart(8)}%  ${r.file}${r.resized ? '  [size changed]' : ''}`
  );
}
const onlyBefore = [...beforeFiles].filter((f) => !afterFiles.has(f)).sort();
const onlyAfter = [...afterFiles].filter((f) => !beforeFiles.has(f)).sort();
for (const f of onlyBefore) console.log(`  removed  ${f}`);
for (const f of onlyAfter) console.log(`    added  ${f}`);

console.log(
  `\n${results.length} compared: ${clean} ≤${THRESHOLD_PCT}%, ` +
    `${flagged.length} to review, ${onlyBefore.length} removed, ${onlyAfter.length} added`
);
