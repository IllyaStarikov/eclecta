<?xml version="1.0" encoding="UTF-8"?>
<!--
  Eclecta feed stylesheet. Turns any of the site's raw RSS feeds into a
  human-readable "subscribe" page when opened in a browser, while leaving the
  feed itself 100% valid RSS for reader apps. Applied via the `stylesheet`
  option in every src/pages/**/rss.xml.js (see FEED_STYLESHEET in src/lib/feeds.ts).
  Standalone document: all CSS/JS is inlined, colours mirror src/styles/global.css.
  The feed's own URL is unknown to XSLT, so it is filled in client-side from
  window.location.href (also powers the Feedly / Inoreader quick-subscribe links).
-->
<xsl:stylesheet version="1.0"
  xmlns:xsl="http://www.w3.org/1999/XSL/Transform"
  xmlns:atom="http://www.w3.org/2005/Atom"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <xsl:output method="html" version="1.0" encoding="UTF-8" indent="yes"
    doctype-system="about:legacy-compat"/>

  <xsl:template match="/">
    <html lang="en">
      <head>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <meta name="color-scheme" content="light dark"/>
        <meta name="robots" content="noindex"/>
        <title><xsl:value-of select="/rss/channel/title"/> · web feed</title>
        <style><![CDATA[
          :root{
            --ground:#f1f2f0; --ground-2:#e9eae7; --ink:#16181d; --ink-soft:#5b5f66;
            --ink-faint:#757980; --hairline:#d5d7d2; --hairline-bold:#bfc2bc;
            --accent:#e8451f; --accent-ink:#c63a18;
            --sans:"Schibsted Grotesk Variable",system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
            --serif:"Source Serif 4 Variable",Georgia,"Times New Roman",serif;
            --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
          }
          @media (prefers-color-scheme: dark){
            :root{
              --ground:#101214; --ground-2:#16181b; --ink:#ecece8; --ink-soft:#9ca0a6;
              --ink-faint:#888d96; --hairline:#2a2d31; --hairline-bold:#3a3e43;
              --accent:#ff6a3d; --accent-ink:#ff8a63;
            }
          }
          *{ box-sizing:border-box; }
          body{
            margin:0; background:var(--ground); color:var(--ink);
            font-family:var(--serif); font-size:1.06rem; line-height:1.6;
            -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
          }
          .wrap{ max-width:44rem; margin:0 auto; padding:0 1.25rem; }
          a{ color:var(--accent-ink); text-decoration:none;
             text-underline-offset:2px; }
          a:hover{ text-decoration:underline; }
          .masthead{
            display:flex; align-items:baseline; justify-content:space-between;
            gap:1rem; padding:1.5rem 0 1.25rem; border-bottom:1px solid var(--hairline-bold);
          }
          .brand{
            font-family:var(--sans); font-weight:700; font-size:1.25rem;
            letter-spacing:-0.015em; color:var(--ink);
          }
          .masthead .kicker{
            font-family:var(--mono); font-size:0.68rem; letter-spacing:0.14em;
            text-transform:uppercase; color:var(--ink-faint);
          }
          main{ padding:2.4rem 0 1rem; }
          .tag{
            font-family:var(--mono); font-size:0.72rem; letter-spacing:0.14em;
            text-transform:uppercase; color:var(--accent-ink); margin:0 0 0.75rem;
          }
          h1{
            font-family:var(--sans); font-weight:700; letter-spacing:-0.02em;
            line-height:1.12; font-size:clamp(1.9rem,1.4rem+2vw,2.6rem); margin:0;
          }
          .lede{ color:var(--ink-soft); font-style:italic; margin:0.75rem 0 0; max-width:36ch; }
          section{ margin-top:2.6rem; }
          h2{
            font-family:var(--sans); font-weight:700; font-size:1.15rem;
            letter-spacing:-0.01em; margin:0 0 0.4rem; padding-bottom:0.4rem;
            border-bottom:1px solid var(--hairline-bold);
          }
          .note{ color:var(--ink-soft); margin:0.6rem 0 1.1rem; font-size:0.98rem; max-width:52ch; }
          .url-row{ display:flex; gap:0.6rem; align-items:stretch; flex-wrap:wrap; }
          .url{
            flex:1 1 18rem; min-width:0; font-family:var(--mono); font-size:0.78rem;
            letter-spacing:0.01em; color:var(--ink); background:var(--ground-2);
            border:1px solid var(--hairline); padding:0.6rem 0.7rem; user-select:all;
            overflow-wrap:anywhere; display:flex; align-items:center;
          }
          .btn{
            font-family:var(--mono); font-size:0.72rem; letter-spacing:0.08em;
            text-transform:uppercase; color:var(--ink); background:transparent;
            border:1px solid var(--hairline-bold); padding:0.6rem 0.95rem;
            cursor:pointer; white-space:nowrap; display:inline-flex; align-items:center;
          }
          .btn:hover{ border-color:var(--accent); color:var(--accent-ink); text-decoration:none; }
          .readers{ display:flex; gap:0.6rem; margin-top:0.6rem; flex-wrap:wrap; }
          .hint{ color:var(--ink-faint); font-size:0.9rem; margin:1.1rem 0 0; }
          ol.items{ list-style:none; margin:1.2rem 0 0; padding:0; }
          ol.items li{
            padding:0.85rem 0; border-bottom:1px solid var(--hairline);
            display:flex; gap:1rem; align-items:baseline; justify-content:space-between;
          }
          .item-title{
            font-family:var(--sans); font-weight:600; font-size:1.02rem;
            letter-spacing:-0.01em; color:var(--ink);
          }
          .item-title:hover{ color:var(--accent-ink); }
          .item-date{
            font-family:var(--mono); font-size:0.68rem; letter-spacing:0.06em;
            color:var(--ink-faint); white-space:nowrap;
          }
          .foot{
            margin:3rem 0 2.5rem; padding-top:1.25rem; border-top:1px solid var(--hairline-bold);
            font-family:var(--mono); font-size:0.72rem; letter-spacing:0.06em;
            text-transform:uppercase; color:var(--ink-faint);
          }
          .foot a{ color:var(--ink-soft); }
        ]]></style>
      </head>
      <body>
        <div class="wrap">
          <header class="masthead">
            <a class="brand"><xsl:attribute name="href"><xsl:value-of select="/rss/channel/link"/></xsl:attribute>Eclecta</a>
            <span class="kicker">Web feed · RSS</span>
          </header>

          <main>
            <p class="tag">This is an RSS feed</p>
            <h1><xsl:value-of select="/rss/channel/title"/></h1>
            <p class="lede"><xsl:value-of select="/rss/channel/description"/></p>

            <section>
              <h2>Subscribe in your reader</h2>
              <p class="note">A web feed updates automatically inside a feed reader,
              so new picks and digests come to you. Paste this URL into any reader,
              or use one of the buttons.</p>
              <div class="url-row">
                <code class="url" id="feed-url">&#8230;</code>
                <button class="btn" id="btn-copy" type="button">Copy</button>
              </div>
              <div class="readers">
                <a class="btn" id="btn-feedly" target="_blank" rel="noopener" href="https://feedly.com/">Add to Feedly</a>
                <a class="btn" id="btn-inoreader" target="_blank" rel="noopener" href="https://www.inoreader.com/">Add to Inoreader</a>
              </div>
              <p class="hint">New to feeds? <a href="https://aboutfeeds.com/" target="_blank" rel="noopener">How feed readers work.</a></p>
            </section>

            <section>
              <h2>Latest</h2>
              <ol class="items">
                <xsl:for-each select="/rss/channel/item[position() &lt;= 15]">
                  <li>
                    <a class="item-title">
                      <xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute>
                      <xsl:value-of select="title"/>
                    </a>
                    <span class="item-date"><xsl:value-of select="substring(pubDate,1,16)"/></span>
                  </li>
                </xsl:for-each>
              </ol>
            </section>
          </main>

          <footer class="foot">
            <a><xsl:attribute name="href"><xsl:value-of select="/rss/channel/link"/></xsl:attribute>eclecta.co</a>
            &#160;·&#160;
            <a><xsl:attribute name="href"><xsl:value-of select="concat(/rss/channel/link,'feeds/')"/></xsl:attribute>All feeds</a>
          </footer>
        </div>

        <script><![CDATA[
          (function () {
            var url = window.location.href;
            var box = document.getElementById('feed-url');
            if (box) box.textContent = url;

            var feedly = document.getElementById('btn-feedly');
            if (feedly) feedly.href = 'https://feedly.com/i/subscribe/' + url;

            var ino = document.getElementById('btn-inoreader');
            if (ino) ino.href = 'https://www.inoreader.com/?add_feed=' + encodeURIComponent(url);

            var copy = document.getElementById('btn-copy');
            if (copy) {
              copy.addEventListener('click', function () {
                var done = function () {
                  copy.textContent = 'Copied';
                  setTimeout(function () { copy.textContent = 'Copy'; }, 1600);
                };
                if (navigator.clipboard && navigator.clipboard.writeText) {
                  navigator.clipboard.writeText(url).then(done, selectUrl);
                } else {
                  selectUrl();
                }
              });
            }

            function selectUrl() {
              if (!box) return;
              var range = document.createRange();
              range.selectNodeContents(box);
              var sel = window.getSelection();
              sel.removeAllRanges();
              sel.addRange(range);
            }
          })();
        ]]></script>
      </body>
    </html>
  </xsl:template>
</xsl:stylesheet>
