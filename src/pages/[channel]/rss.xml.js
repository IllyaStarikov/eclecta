import rss from '@astrojs/rss';
import picks from '../../data/picks.json';
import channels from '../../data/channels.json';
import { absUrl } from '../../site';
import { getFeed, pickItemHtml, pickPrimaryLink } from '../../lib/feeds';

export function getStaticPaths() {
  return channels.map((c) => ({ params: { channel: c.slug }, props: { channel: c } }));
}

export function GET(context) {
  const { channel } = context.props;
  const feed = getFeed(`channel-${channel.slug}`);
  const list = picks.filter((p) => p.channels.includes(channel.slug));
  return rss({
    title: feed.title,
    description: feed.description,
    site: context.site,
    items: list.map((p) => ({
      title: p.title,
      link: pickPrimaryLink(p),
      pubDate: p.curated_at ? new Date(p.curated_at) : undefined,
      description: p.why || '',
      content: pickItemHtml(p),
    })),
    customData:
      '<language>en-us</language>' +
      `<docs>${absUrl(`/${channel.slug}/`, context.site)}</docs>`,
  });
}
