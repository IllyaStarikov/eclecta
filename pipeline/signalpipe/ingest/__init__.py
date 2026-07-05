"""Ingestion layer: source registry, polite HTTP, per-source fetchers, and the
orchestrating pipeline. Each fetcher returns normalized raw-item dicts:

    {
      "guid": str,            # source-native stable id
      "raw_url": str,         # link as the source gave it
      "title": str,
      "author": Optional[str],
      "published_at": Optional[str],   # ISO-8601 UTC
      "points": Optional[int],
      "comments": Optional[int],
      "extra": dict,          # per-source signals (discussion_url, tags, ...)
    }
"""
