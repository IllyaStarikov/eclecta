import { describe, expect, it } from 'vitest';
import {
  CATEGORIES,
  CHANNEL_TO_CATEGORY,
  categoryBySlug,
  categoryName,
  deriveCategory,
} from '../../src/lib/taxonomy';

// Handy sets/maps derived from the source of truth.
const CATEGORY_SLUGS = new Set(CATEGORIES.map((c) => c.slug));
const SUBS_BY_CATEGORY = new Map(
  CATEGORIES.map((c) => [c.slug, new Set(c.subcategories.map((s) => s.slug))]),
);

describe('deriveCategory() — single category', () => {
  it('derives one category with its single matching subcategory', () => {
    // "agentic" + "orchestrat" both live only in ai/agents; no ai category-match word.
    expect(deriveCategory('agentic orchestration')).toEqual({
      category: 'ai',
      subcategories: ['agents'],
    });
  });

  it('a category-match word with no subcategory hit yields empty subcategories', () => {
    // "silicon" is a hardware category-match word but is NOT in any hardware
    // subcategory match list, so score is 1 (category only) and subs are empty.
    expect(deriveCategory('pure silicon dreams')).toEqual({
      category: 'hardware',
      subcategories: [],
    });
  });

  it('channels default to [] when only a title is passed', () => {
    // Omitting the channels arg must behave identically to passing [].
    expect(deriveCategory('agentic orchestration')).toEqual({
      category: 'ai',
      subcategories: ['agents'],
    });
    expect(deriveCategory('agentic orchestration')).toEqual(
      deriveCategory('agentic orchestration', []),
    );
  });
});

describe('deriveCategory() — scoring (subs*2 + category*1)', () => {
  it('sums multiple subcategory hits plus the category-match bonus', () => {
    // ai: models(frontier model)+agents(agent)+evals(benchmark)=3 subs *2 = 6,
    // plus the "openai" category-match word = 7. Nothing else scores.
    expect(deriveCategory('OpenAI frontier model agent benchmark')).toEqual({
      category: 'ai',
      subcategories: ['models', 'agents', 'evals'],
    });
  });

  it('two subcategory hits (score 4) outrank a rival category-match + one sub (score 3)', () => {
    // ai: models+agents = 4 (no ai category word).
    // industry: funding sub (2) + "funding" category word (1) = 3.
    const res = deriveCategory('frontier model agent funding');
    expect(res.category).toBe('ai');
    expect(res.subcategories).toEqual(['models', 'agents']);
  });

  it('preserves subcategory order as declared on the category', () => {
    // models before agents before evals in CATEGORIES.ai.subcategories.
    const res = deriveCategory('OpenAI frontier model agent benchmark');
    expect(res.subcategories).toEqual(['models', 'agents', 'evals']);
  });
});

describe('deriveCategory() — subcategories capped at 3', () => {
  it('returns at most three subcategories even when five match', () => {
    // frontier model(models) agent(agents) benchmark(evals) alignment(safety) copilot(apps)
    const res = deriveCategory(
      'frontier model agent benchmark alignment copilot',
    );
    expect(res.category).toBe('ai');
    expect(res.subcategories).toHaveLength(3);
    expect(res.subcategories).toEqual(['models', 'agents', 'evals']);
  });
});

describe('deriveCategory() — channel fallback (CHANNEL_TO_CATEGORY)', () => {
  it('routes a no-lexicon title by its channel, with empty subcategories', () => {
    expect(deriveCategory('hello world', ['security'])).toEqual({
      category: 'security',
      subcategories: [],
    });
  });

  it('a matching channel adds +1 to an existing lexicon score without changing the pick', () => {
    // ai already wins on lexicon (score 2); the ai channel just reinforces it.
    expect(deriveCategory('agentic orchestration', ['ai'])).toEqual({
      category: 'ai',
      subcategories: ['agents'],
    });
  });

  it('maps every channel to its category for a neutral title', () => {
    for (const [channel, expected] of Object.entries(CHANNEL_TO_CATEGORY)) {
      expect(deriveCategory('lorem ipsum dolor', [channel]).category).toBe(
        expected,
      );
    }
  });

  it('routes the aliasing channels to their shared category', () => {
    // Both "science" and "ml-research" fold into research.
    expect(deriveCategory('lorem ipsum', ['science']).category).toBe('research');
    expect(deriveCategory('lorem ipsum', ['ml-research']).category).toBe(
      'research',
    );
  });
});

describe('deriveCategory() — no match / industry fallback', () => {
  it('falls back to industry for an empty title and no channels', () => {
    expect(deriveCategory('')).toEqual({ category: 'industry', subcategories: [] });
  });

  it('falls back to industry when nothing in the title matches', () => {
    expect(deriveCategory('qwerty zxcvbn plugh').category).toBe('industry');
  });

  it('ignores channels that are not in CHANNEL_TO_CATEGORY', () => {
    expect(deriveCategory('qwerty', ['not-a-real-channel']).category).toBe(
      'industry',
    );
  });
});

describe('deriveCategory() — PRIORITY tie-breaks', () => {
  it('security beats ai on an equal score even though ai is scored first', () => {
    // ai: agents(agentic) = 2. security: research(fuzzing) = 2 (no security
    // category word). PRIORITY ranks security above ai, so security wins.
    expect(deriveCategory('agentic fuzzing')).toEqual({
      category: 'security',
      subcategories: ['research'],
    });
  });

  it('hardware beats research on an equal score despite research scoring first', () => {
    // research: ml(transformer)=2. hardware: devices(robot)=2. PRIORITY ranks
    // hardware above research.
    expect(deriveCategory('transformer robot')).toEqual({
      category: 'hardware',
      subcategories: ['devices'],
    });
  });

  it('a channel can tip the winner to a category with no subcategory hits', () => {
    // Title scores ai:2 (agents); two security channels push security to 2.
    // PRIORITY breaks the tie for security, whose subHits are empty.
    expect(deriveCategory('agentic orchestration', ['security', 'security'])).toEqual({
      category: 'security',
      subcategories: [],
    });
  });
});

describe('deriveCategory() — case insensitivity', () => {
  it('lowercases the title before matching', () => {
    // Pin the concrete result: a broken lowercase path must not be able to pass
    // by returning the same (wrong) value for both the upper- and lower-cased call.
    expect(deriveCategory('AGENTIC ORCHESTRATION')).toEqual({
      category: 'ai',
      subcategories: ['agents'],
    });
  });

  it('matches an uppercase category-match word', () => {
    // "openai" is an ai category-match word; no ai subcategory matches, so subs = [].
    expect(deriveCategory('OPENAI')).toEqual({ category: 'ai', subcategories: [] });
  });
});

describe('deriveCategory() — leading/trailing space padding', () => {
  it('the " ${title} " padding lets a bare "ai" token match " ai "', () => {
    expect(deriveCategory('AI')).toEqual({ category: 'ai', subcategories: [] });
  });

  it('does not treat "aid" as a match for " ai " (space-bounded)', () => {
    const res = deriveCategory('aid workers deliver relief');
    expect(res.category).not.toBe('ai');
    expect(res.category).toBe('industry');
  });

  it('matches a trailing space-bounded token at the end of the title', () => {
    // software category-match word is " api " (spaces both sides); the trailing
    // pad supplies the closing space. No software subcategory matches, so subs = [].
    expect(deriveCategory('the api')).toEqual({
      category: 'software',
      subcategories: [],
    });
  });
});

describe('categoryBySlug()', () => {
  it('returns the matching category object for every known slug', () => {
    for (const cat of CATEGORIES) {
      const found = categoryBySlug(cat.slug);
      expect(found).toBeDefined();
      expect(found?.slug).toBe(cat.slug);
      expect(found?.name).toBe(cat.name);
    }
  });

  it('returns undefined for an unknown slug', () => {
    expect(categoryBySlug('does-not-exist')).toBeUndefined();
  });
});

describe('categoryName()', () => {
  it.each([
    ['ai', 'AI'],
    ['research', 'Research'],
    ['software', 'Software'],
    ['security', 'Security'],
    ['hardware', 'Hardware'],
    ['industry', 'Industry'],
  ])('names known slug %s -> %s', (slug, name) => {
    expect(categoryName(slug)).toBe(name);
  });

  it('returns the slug unchanged for an unknown slug', () => {
    expect(categoryName('totally-unknown')).toBe('totally-unknown');
  });
});

describe('taxonomy structural invariants', () => {
  it('has unique category slugs', () => {
    const slugs = CATEGORIES.map((c) => c.slug);
    expect(new Set(slugs).size).toBe(slugs.length);
  });

  it('has unique subcategory slugs within each category', () => {
    for (const cat of CATEGORIES) {
      const subSlugs = cat.subcategories.map((s) => s.slug);
      expect(new Set(subSlugs).size).toBe(subSlugs.length);
    }
  });

  it('every category has a non-empty, all-lowercase match array', () => {
    for (const cat of CATEGORIES) {
      expect(cat.match.length).toBeGreaterThan(0);
      expect(cat.name.length).toBeGreaterThan(0);
      expect(cat.blurb.length).toBeGreaterThan(0);
      for (const m of cat.match) {
        expect(m.length).toBeGreaterThan(0);
        expect(m).toBe(m.toLowerCase());
      }
    }
  });

  it('every subcategory has a non-empty, all-lowercase match array', () => {
    for (const cat of CATEGORIES) {
      expect(cat.subcategories.length).toBeGreaterThan(0);
      for (const sub of cat.subcategories) {
        expect(sub.slug.length).toBeGreaterThan(0);
        expect(sub.name.length).toBeGreaterThan(0);
        expect(sub.blurb.length).toBeGreaterThan(0);
        expect(sub.match.length).toBeGreaterThan(0);
        for (const m of sub.match) {
          expect(m.length).toBeGreaterThan(0);
          expect(m).toBe(m.toLowerCase());
        }
      }
    }
  });

  it('CHANNEL_TO_CATEGORY values are all real category slugs', () => {
    for (const value of Object.values(CHANNEL_TO_CATEGORY)) {
      expect(CATEGORY_SLUGS.has(value)).toBe(true);
    }
  });

  it('deriveCategory always returns a valid category and valid subcategories', () => {
    const titles = [
      '',
      'qwerty',
      'agentic orchestration',
      'OpenAI frontier model agent benchmark',
      'agentic fuzzing',
      'transformer robot',
      'pure silicon dreams',
      'the api',
      'AI',
      'series a funding round raises billion',
      'kubernetes docker cloud deployment',
      'quantum physics protein fusion',
    ];
    for (const title of titles) {
      const res = deriveCategory(title, ['ai', 'security', 'not-real']);
      expect(CATEGORY_SLUGS.has(res.category)).toBe(true);
      expect(res.subcategories.length).toBeLessThanOrEqual(3);
      const validSubs = SUBS_BY_CATEGORY.get(res.category)!;
      for (const s of res.subcategories) {
        expect(validSubs.has(s)).toBe(true);
      }
    }
  });
});
