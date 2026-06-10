# Lede

A digital broadsheet for the frontier — the best of technology and AI, curated
continuously and distilled weekly. Static site (Astro), served at
**starikov.io** via GitHub Pages.

Working title; the masthead name lives in one place (`src/layouts/Base.astro`)
and is trivial to swap.

## What it is

The public face of the Signal pipeline. The pipeline (running locally) reads
1,100+ sources, dedups and scores them, curates the best with Claude, and writes
its output into this repo:

- `src/content/digests/*.md` — the weekly digest (markdown + frontmatter)
- `src/data/picks.json` — the curated items (why / notes / summary / surfaces)
- `src/data/channels.json` — channel definitions

Astro builds it into a static site with per-channel RSS feeds.

## Develop

```bash
npm install
npm run dev      # http://localhost:4321
npm run build    # -> dist/  (what GitHub Pages serves)
npm run preview  # preview the built site
```

## Structure

```
src/
  content/digests/   weekly digests (pipeline-written)
  data/              picks.json, channels.json (pipeline-written)
  layouts/Base.astro masthead + footer + fonts
  components/Pick.astro
  pages/
    index.astro            front page: lead digest + picks + subscribe
    digests/[...slug].astro full digest reading view
    [channel]/index.astro   per-channel pick lists
    [channel]/rss.xml.js     per-channel feeds
    rss.xml.js               the everything feed
    archive.astro about.astro
  styles/global.css   the editorial stylesheet
public/CNAME          starikov.io
```

## Design

Editorial broadsheet: Fraunces (display) · Newsreader (reading) · IBM Plex Mono
(technical labels). Warm paper, vermilion accent, hairline rules, sharp corners,
light + dark. Lines and type, nothing else.
