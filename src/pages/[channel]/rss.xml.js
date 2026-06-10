import rss from '@astrojs/rss';
import picks from '../../data/picks.json';
import channels from '../../data/channels.json';

export function getStaticPaths() {
  return channels.map((c) => ({ params: { channel: c.slug }, props: { channel: c } }));
}

function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function itemHtml(p) {
  const notes = p.notes && p.notes.length
    ? `<p><strong>Notes</strong></p><ul>${p.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>`
    : '';
  return `${p.why ? `<p><strong>Why it matters:</strong> ${esc(p.why)}</p>` : ''}${notes}${
    p.summary ? `<p>${esc(p.summary)}</p>` : ''
  }`;
}

export function GET(context) {
  const { channel } = context.props;
  const list = picks.filter((p) => p.channels.includes(channel.slug));
  return rss({
    title: `Lede · ${channel.name}`,
    description: channel.blurb,
    site: context.site,
    items: list.map((p) => ({
      title: p.title,
      link: p.link || '#',
      pubDate: p.curated_at ? new Date(p.curated_at) : new Date('2026-06-09'),
      description: p.why || '',
      content: itemHtml(p),
    })),
    customData: '<language>en-us</language>',
  });
}
