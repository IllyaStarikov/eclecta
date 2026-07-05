/**
 * Design lint: pages and components may not step outside the system.
 * See docs/design-language.md §8 Governance.
 *
 *  1. No static style="..." attributes in templates; dynamic style={...} is
 *     allowed only for data-driven values (reveal stagger, bar widths).
 *  2. Page/component <style> blocks: no hex colors, no literal rem/em
 *     font-size or letter-spacing — token vars only. One sanctioned escape
 *     (`design-lint-allow: display-xl`, the 404 numeral) must exist exactly
 *     once in the whole tree.
 *  3. Every width-based media query uses a canonical breakpoint.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const SRC = join(__dirname, '../../src');
const BREAKPOINTS = new Set(['30rem', '40rem', '52rem', '60rem']);

function walk(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    return e.isDirectory() ? walk(p) : [p];
  });
}

const astroFiles = walk(SRC).filter((f) => f.endsWith('.astro'));
const styleSources = [...astroFiles, join(SRC, 'styles/global.css')];

describe('design lint', () => {
  it('no static inline styles in templates', () => {
    const offenders: string[] = [];
    for (const file of astroFiles) {
      const text = readFileSync(file, 'utf8');
      if (/style="/.test(text)) offenders.push(`${file}: static style=""`);
      for (const m of text.matchAll(/style=\{([^}]*)\}/g)) {
        if (!/animation-delay|width/.test(m[1])) {
          offenders.push(`${file}: dynamic style not on the allowlist (${m[1].slice(0, 40)})`);
        }
      }
    }
    expect(offenders).toEqual([]);
  });

  it('page <style> blocks stay on tokens (one sanctioned escape)', () => {
    const offenders: string[] = [];
    let escapes = 0;
    for (const file of astroFiles) {
      const text = readFileSync(file, 'utf8');
      const styles = [...text.matchAll(/<style>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
      if (!styles) continue;
      if (styles.includes('design-lint-allow: display-xl')) {
        escapes += 1;
        continue;
      }
      // Positive literals only: the mono two-trackings rule is about positive
      // spacing; negative sans display-tracking is legitimately per-component.
      for (const m of styles.matchAll(/(font-size|letter-spacing)\s*:\s*([^;]+);/g)) {
        if (/^\d*\.?\d+(rem|em|px)/.test(m[2].trim())) {
          offenders.push(`${file}: literal ${m[1]}: ${m[2].trim()}`);
        }
      }
      for (const m of styles.matchAll(/#[0-9a-fA-F]{3,8}\b/g)) {
        offenders.push(`${file}: hex color ${m[0]}`);
      }
    }
    expect(offenders).toEqual([]);
    expect(escapes, 'exactly one sanctioned display stunt (the 404 numeral)').toBe(1);
  });

  it('media queries use only canonical breakpoints', () => {
    const offenders: string[] = [];
    for (const file of styleSources) {
      const text = readFileSync(file, 'utf8');
      for (const m of text.matchAll(/@media[^{\n]*\((?:min|max)-width:\s*([\d.]+(?:rem|px|em))\)/g)) {
        if (!BREAKPOINTS.has(m[1])) offenders.push(`${file}: @media ${m[1]}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
