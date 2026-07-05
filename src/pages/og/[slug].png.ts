/**
 * Share cards (og:image), generated at BUILD time: a typographic card in the
 * publication's own system: ground field, nameplate + the mark, headline,
 * mono footer. Type and rules only; the design's no-images rule governs the
 * site, not link-preview furniture. One card per digest + the default.
 *
 * satori renders the object tree to SVG; resvg rasterizes. Fonts are the
 * repo's own @fontsource packages (WOFF builds — satori cannot read woff2).
 */
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import type { APIRoute } from 'astro';
import { getCollection } from 'astro:content';
import satori from 'satori';
import { Resvg } from '@resvg/resvg-js';
import { site, KIND_LABEL } from '../../site';

const require = createRequire(import.meta.url);
const schibsted = readFileSync(
  require.resolve('@fontsource/schibsted-grotesk/files/schibsted-grotesk-latin-800-normal.woff')
);
const plexMono = readFileSync(
  require.resolve('@fontsource/ibm-plex-mono/files/ibm-plex-mono-latin-500-normal.woff')
);

const INK = '#16181d';
const GROUND = '#f1f2f0';
const ACCENT = '#e8451f';
const SOFT = '#5b5f66';

export async function getStaticPaths() {
  const digests = await getCollection('digests');
  return [
    { params: { slug: 'default' }, props: { headline: site.tagline ?? site.description, kicker: null } },
    ...digests.map((d) => ({
      params: { slug: d.id.replaceAll('/', '-') },
      props: {
        headline: d.data.title,
        kicker: `${KIND_LABEL[d.data.kind]}  ${d.data.period}`.toUpperCase(),
      },
    })),
  ];
}

/** satori element helper */
const h = (type: string, style: Record<string, unknown>, children?: unknown) => ({
  type,
  props: { style, children },
});

export const GET: APIRoute = async ({ props }) => {
  const { headline, kicker } = props as { headline: string; kicker: string | null };

  const tree = h(
    'div',
    {
      width: '100%',
      height: '100%',
      display: 'flex',
      flexDirection: 'column',
      background: GROUND,
      color: INK,
      padding: '64px 72px',
      fontFamily: 'Schibsted Grotesk',
    },
    [
      // nameplate: wordmark + the mark, over the 4px rule
      h(
        'div',
        {
          display: 'flex',
          alignItems: 'flex-end',
          borderBottom: `5px solid ${INK}`,
          paddingBottom: '18px',
        },
        [
          h('div', { fontSize: 64, fontWeight: 800, letterSpacing: '-0.045em' }, 'ECLECTA'),
          h('div', { width: 20, height: 20, background: ACCENT, marginLeft: 6, marginBottom: 8 }),
          kicker
            ? h(
                'div',
                {
                  marginLeft: 'auto',
                  fontFamily: 'IBM Plex Mono',
                  fontSize: 22,
                  letterSpacing: '0.14em',
                  color: SOFT,
                  paddingBottom: '10px',
                },
                kicker
              )
            : h('div', {}),
        ]
      ),
      // headline
      h(
        'div',
        {
          display: 'flex',
          flexGrow: 1,
          alignItems: 'center',
          fontSize: headline.length > 70 ? 52 : 62,
          fontWeight: 800,
          letterSpacing: '-0.02em',
          lineHeight: 1.08,
          paddingRight: 40,
        },
        headline
      ),
      // mono footer
      h(
        'div',
        {
          display: 'flex',
          fontFamily: 'IBM Plex Mono',
          fontSize: 22,
          letterSpacing: '0.14em',
          color: SOFT,
          borderTop: '1px solid #bfc2bc',
          paddingTop: '18px',
        },
        'ECLECTA.CO'
      ),
    ]
  );

  const svg = await satori(tree as Parameters<typeof satori>[0], {
    width: 1200,
    height: 630,
    fonts: [
      { name: 'Schibsted Grotesk', data: schibsted, weight: 800, style: 'normal' },
      { name: 'IBM Plex Mono', data: plexMono, weight: 500, style: 'normal' },
    ],
  });

  const png = new Resvg(svg, { fitTo: { mode: 'width', value: 1200 } }).render().asPng();
  return new Response(new Uint8Array(png), {
    headers: { 'Content-Type': 'image/png' },
  });
};
