/**
 * The design doc and the CSS may not drift: every token declared in :root
 * must be documented in docs/design-language.md, every token the doc names
 * must exist, and the color table's light/dark values must match the
 * light-dark() pairs exactly. See design-language.md §8 Governance.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const css = readFileSync(join(__dirname, '../../src/styles/global.css'), 'utf8');
const doc = readFileSync(join(__dirname, '../../docs/design-language.md'), 'utf8');

/** :root { ... } — first block only (component rules never declare tokens). */
function rootBlock(source: string): string {
  const start = source.indexOf(':root {');
  const end = source.indexOf('\n}', start);
  return source.slice(start, end);
}

const root = rootBlock(css);
const declared = new Map<string, string>();
for (const m of root.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
  declared.set(m[1], m[2].trim());
}

/** Normalize numeric formatting so 2.00vw == 2vw and spacing is ignored. */
const norm = (s: string) =>
  s
    .replace(/(\d*\.?\d+)/g, (n) => String(parseFloat(n)))
    .replace(/\s+/g, ' ')
    .trim();

describe('design tokens ↔ design-language.md', () => {
  it(':root declares a sane number of tokens', () => {
    expect(declared.size).toBeGreaterThan(30);
  });

  it('every :root token is documented', () => {
    const undocumented = [...declared.keys()].filter((t) => !doc.includes(t));
    expect(undocumented).toEqual([]);
  });

  it('every token the Tokens section names exists in :root', () => {
    // Scan §2 only — later sections name class MODIFIERS (--faint, --page…)
    // in backticks, which are not custom properties.
    const tokensSection = doc.slice(doc.indexOf('## 2. Tokens'), doc.indexOf('## 3.'));
    const ghosts = [
      ...new Set([...tokensSection.matchAll(/`(--[a-z0-9-]+)`/g)].map((m) => m[1])),
    ].filter((t) => !declared.has(t));
    expect(ghosts).toEqual([]);
  });

  const colorRows = [...doc.matchAll(/^\|\s*`(--[a-z0-9-]+)`\s*\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|/gm)];
  const colorTokens = new Set(colorRows.map((m) => m[1]));

  it('the color table matches the light-dark() pairs', () => {
    // 8 base tokens; --accent/--accent-ink are documented derivations of the
    // --acc-* pairs (their own table, different row shape).
    expect(colorRows.length).toBeGreaterThanOrEqual(8);
    for (const [, token, light, dark] of colorRows) {
      const value = declared.get(token);
      expect(value, `${token} missing from :root`).toBeDefined();
      expect(norm(value!), `${token} value drifted`).toBe(norm(`light-dark(${light}, ${dark})`));
    }
  });

  it('single-value doc rows match :root values', () => {
    // Doc rows: | `--token` | value | role — color rows are checked above.
    const rows = [...doc.matchAll(/^\|\s*`(--[a-z0-9-]+)`\s*\|\s*`?([^|`]+?)`?\s*\|/gm)];
    const checked: string[] = [];
    for (const [, token, rawValue] of rows) {
      if (colorTokens.has(token)) continue;
      const value = declared.get(token);
      if (value === undefined) continue; // grouped rows handled by the mention checks
      expect(norm(value), `${token} value drifted from the doc`).toBe(norm(rawValue));
      checked.push(token);
    }
    expect(checked.length).toBeGreaterThanOrEqual(10); // scale + mono + spacing rows
  });
});
