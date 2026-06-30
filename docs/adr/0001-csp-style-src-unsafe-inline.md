# ADR 0001 — CSP `style-src 'unsafe-inline'` retained

**Status:** Accepted
**Date:** 2026-05-18
**Round:** Audit round 3 — bug X15

## Context

The current CSP header sets:

```
style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net
```

While `script-src` uses a per-request nonce (closing the inline-JS XSS
vector), `style-src` keeps `'unsafe-inline'`. This is a known relaxation
documented inline at `app/__init__.py:637-645`.

## Why we kept `'unsafe-inline'` on style

1. **Bootstrap utility classes attached via `style=`**: the templates
   carry many `style="..."` attributes for one-off layout tweaks
   (sticky-top, fixed dimensions, gradient backgrounds). Migrating
   each to a class+stylesheet or to a nonce-protected inline block is
   a O(templates) refactor without security gain proportional to the
   cost (see §Risk below).

2. **Vendor CSS via CDN already trusted**: Bootstrap, Bootstrap-Icons,
   Quill, Tom-Select, Select2 inject their own `<style>` blocks at
   load time. Each would need its own nonce passthrough.

3. **No password-autofill CSS-keystroke leak vector**: the autofill
   leak via attribute selectors on password fields (`input[type=password][value="..."]`)
   only works if an attacker can inject `<style>`. Our XSS surface is
   bounded by the nonce-protected `script-src`; an attacker who lands
   `<style>` is one who already has full XSS, at which point CSS leaks
   are not the main concern.

## Residual risks accepted

- **UI defacement / phishing UI**: an XSS that survives script-src
  could still rewrite the page visually via injected `<style>`.
  Acceptable because (a) we have no script-src `'unsafe-inline'`, so
  the bar to land any style injection is already a non-trivial XSS,
  (b) the rest of the page chrome (URL bar, browser security UI) is
  unaffected.
- **CSS-based timing channels**: theoretical, no known practical
  payload that matters against our threat model.

## Migration path (if/when revisited)

1. Audit all `style="..."` attributes — categorise: vendor library,
   layout one-off, dynamic styling.
2. Promote layout one-offs to a single `app/static/style.css` with
   named classes.
3. Wrap remaining dynamic blocks in nonce-protected `<style nonce="…">`.
4. Drop `'unsafe-inline'` from style-src, replace with `'nonce-…'`.

Estimated effort: 1–2 days of template sweeping + visual regression
testing across all pages.

## Related

- Audit round 1 / M3 — initial nonce migration on script-src
- Audit round 3 / X13+X14 — CSP `connect-src` and `img-src` tightening
- CLAUDE.md §22 (audits exhaustifs reporting pattern)
