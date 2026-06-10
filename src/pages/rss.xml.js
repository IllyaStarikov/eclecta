import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import picks from '../data/picks.json';

function itemHtml(p) {
  const notes = p.notes && p.notes.length
    ? `<p><strong>Notes</strong></p><ul>${p.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`
    : '';
  const surf = p.surfaces && p.surfaces.length
    ? `<p><strong>Surfaced on</strong> ${p.surfaces
        .map((s) => `<a href="${s.url}">${esc(s.name)}${s.points ? ` (${s.points})` : ''}</a>`)
        .join(' · ')}</p>`
    : '';
  return `${p.why ? `<p><strong>Why it matters:</strong> ${esc(p.why)}</p>` : ''}${notes}${
    p.summary ? `<p>${esc(p.summary)}</p>` : ''
  }${surf}`;
}
function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export async function GET(context) {
  const digests = await getCollection('digests');
  const items = [
    ...digests.map((d) => ({
      title: `Digest · ${d.data.title}`,
      link: `/digests/${d.id}/`,
      pubDate: d.data.date,
      description: d.data.blurb,
    })),
    ...picks.map((p) => ({
      title: p.title,
      link: p.link || '#',
      pubDate: p.curated_at ? new Date(p.curated_at) : new Date('2026-06-09'),
      description: p.why || '',
      content: itemHtml(p),
    })),
  ];
  return rss({
    title: 'Lede — technology & AI',
    description: 'The best of technology and AI, curated and explained.',
    site: context.site,
    items,
    customData: '<language>en-us</language>',
  });
}
