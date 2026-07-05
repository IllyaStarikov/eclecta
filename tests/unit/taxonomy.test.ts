/**
 * The categorization core: deriveCategory (title lexicon + channel fallback)
 * and resolveCategory (trust the pipeline, derive as fallback). These drive
 * sectioning, routing, feeds, and muting — the load-bearing logic of the
 * edition layout.
 */
import { describe, expect, it } from 'vitest';
import { CATEGORIES, deriveCategory, resolveCategory } from '../../src/lib/taxonomy';

describe('deriveCategory', () => {
  it('hits a category via a subcategory term', () => {
    const r = deriveCategory('New LLM agent benchmark for tool use', []);
    expect(r.category).toBe('ai');
    expect(r.subcategories).toContain('agents');
  });

  it('falls back to the channel map when the title says nothing', () => {
    expect(deriveCategory('An untitled dispatch', ['devtools']).category).toBe('software');
  });

  it('breaks ties by priority (security outranks software)', () => {
    // One subcategory hit each: 'exploit' (security/research) vs 'compiler'
    // (software/languages) → equal scores, priority decides.
    const r = deriveCategory('Compiler exploit', []);
    expect(r.category).toBe('security');
  });

  it('defaults to industry when nothing matches at all', () => {
    expect(deriveCategory('Zzz', []).category).toBe('industry');
  });

  it('caps subcategories at 3', () => {
    const r = deriveCategory(
      'LLM agents benchmark eval alignment jailbreak copilot rag prompt multimodal',
      []
    );
    expect(r.subcategories.length).toBeLessThanOrEqual(3);
  });
});

describe('resolveCategory', () => {
  it('trusts a valid pipeline-emitted category + subs', () => {
    const r = resolveCategory({
      title: 'Completely unrelated title',
      category: 'hardware',
      subcategories: ['silicon'],
    });
    expect(r).toEqual({ category: 'hardware', subcategories: ['silicon'] });
  });

  it('filters subcategory slugs that do not belong to the category', () => {
    const r = resolveCategory({
      title: 'x',
      category: 'hardware',
      subcategories: ['silicon', 'agents', 'nope'],
    });
    expect(r.subcategories).toEqual(['silicon']);
  });

  it('caps trusted subcategories at 3', () => {
    const ai = CATEGORIES.find((c) => c.slug === 'ai')!;
    const all = ai.subcategories.map((s) => s.slug);
    const r = resolveCategory({ title: 'x', category: 'ai', subcategories: all });
    expect(r.subcategories.length).toBe(3);
  });

  it('falls back to derive on an unknown category slug', () => {
    const r = resolveCategory({
      title: 'New LLM benchmark',
      channels: ['ai'],
      category: 'not-a-category',
      subcategories: ['models'],
    });
    expect(r.category).toBe('ai');
  });

  it('derives when the pipeline emitted nothing', () => {
    const r = resolveCategory({ title: 'Ransomware gang exploits a zero-day', channels: [] });
    expect(r.category).toBe('security');
  });
});
