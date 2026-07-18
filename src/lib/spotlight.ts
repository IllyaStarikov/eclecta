/**
 * Spotlight: stories gaining traction across the internet right now.
 *
 * The pipeline writes src/data/spotlight.json (clusters with unusual
 * cross-surface breadth and velocity, curated or not). The file may not
 * exist yet: loading goes through import.meta.glob so absence cannot break
 * the build, and a malformed file degrades to an empty section, never a
 * crash. CI stays strict separately: when the file exists it must parse
 * (tests/unit/spotlight.test.ts).
 */
import { spotlightFileSchema, type Pick, type SpotlightItem } from './schema';

export const SPOTLIGHT_LIMIT = 8;

export type SpotlightEntry = SpotlightItem & { link: string; pick?: Pick };

export interface Spotlight {
  generatedAt: string | null;
  items: SpotlightEntry[];
}

export function loadSpotlightRaw(): unknown {
  const mods = import.meta.glob('../data/spotlight.json', { eager: true }) as Record<
    string,
    { default?: unknown }
  >;
  return Object.values(mods)[0]?.default ?? null;
}

export function normalizeSpotlight(raw: unknown): Spotlight {
  if (raw == null) return { generatedAt: null, items: [] };
  const parsed = spotlightFileSchema.safeParse(raw);
  if (!parsed.success) {
    console.warn('[spotlight] malformed spotlight.json, hiding the section:', parsed.error.issues[0]);
    return { generatedAt: null, items: [] };
  }
  const file = parsed.data;
  const list = Array.isArray(file) ? file : file.items;
  const items = list
    .map((i) => ({ ...i, link: (i.url ?? i.canonical_url)! }))
    .sort((a, b) => b.score - a.score)
    .slice(0, SPOTLIGHT_LIMIT);
  return { generatedAt: Array.isArray(file) ? null : (file.generated_at ?? null), items };
}

const fmtK = (n: number): string =>
  n >= 10_000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);

export function relativeAge(iso: string, now: Date): string {
  const h = Math.floor((now.getTime() - new Date(iso).getTime()) / 3_600_000);
  if (h < 1) return 'under 1h ago';
  if (h < 48) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** The traction line, as separate parts: spacing separates, never glyphs. */
export function tractionParts(item: SpotlightItem, now: Date): string[] {
  const parts = [`${item.surface_count} surface${item.surface_count === 1 ? '' : 's'}`];
  const pts = item.points ?? item.surfaces.reduce((a, s) => a + (s.points ?? 0), 0);
  const com = item.comments ?? item.surfaces.reduce((a, s) => a + (s.comments ?? 0), 0);
  if (pts > 0) parts.push(`${fmtK(pts)} pts`);
  if (com > 0) parts.push(`${fmtK(com)} comments`);
  if (item.first_seen) parts.push(`first seen ${relativeAge(item.first_seen, now)}`);
  return parts;
}

/** Attach the full pick to curated entries. The two files are written at
 *  different moments, so a dangling pick_id degrades to headline-only. */
export function joinCurated(items: SpotlightEntry[], picks: Pick[]): SpotlightEntry[] {
  const byId = new Map(picks.map((p) => [p.id, p]));
  return items.map((it) => {
    const pick = it.curated && it.pick_id != null ? byId.get(it.pick_id) : undefined;
    return pick ? { ...it, pick } : it;
  });
}
