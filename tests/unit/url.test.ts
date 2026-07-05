/**
 * safeUrl guards every href sink that renders machine-written, third-party-
 * derived URLs (picks.json / spotlight.json). A hostile feed must never get a
 * clickable javascript:/data:/vbscript: link onto eclecta.co.
 */
import { describe, expect, it } from 'vitest';
import { safeUrl } from '../../src/lib/url';

describe('safeUrl', () => {
  it('passes through http(s) absolute URLs unchanged', () => {
    for (const u of [
      'https://example.com/a?b=c#d',
      'http://example.com',
      'HTTPS://Example.com/Path',
    ]) {
      expect(safeUrl(u)).toBe(u);
    }
  });

  it('passes through site-relative paths and fragments', () => {
    expect(safeUrl('/ai/')).toBe('/ai/');
    expect(safeUrl('#p-12')).toBe('#p-12');
  });

  it('neutralizes hostile schemes to undefined', () => {
    for (const u of [
      'javascript:alert(1)',
      'JavaScript:alert(1)',
      'java\tscript:alert(1)', // browsers strip the tab; URL parser does too
      'java\nscript:alert(1)',
      ' javascript:alert(1)',
      'data:text/html,<script>alert(1)</script>',
      'vbscript:msgbox(1)',
      'file:///etc/passwd',
      'blob:https://evil/x',
    ]) {
      expect(safeUrl(u), u).toBeUndefined();
    }
  });

  it('treats protocol-relative and malformed URLs as unsafe', () => {
    expect(safeUrl('//evil.com')).toBeUndefined();
    expect(safeUrl('not a url')).toBeUndefined();
    expect(safeUrl('')).toBeUndefined();
    expect(safeUrl('   ')).toBeUndefined();
    expect(safeUrl(null)).toBeUndefined();
    expect(safeUrl(undefined)).toBeUndefined();
  });
});
