# Feature 12: Public Trust Center

**Date:** 2026-07-20
**Author:** Claude (agent)
**Type:** feat

## Summary

Twelfth phase of the platform pivot (umbrella issue #5, PR #6). An
unauthenticated `/trust-center` route showing only explicitly published,
public content, plus a gated public policy-download path. This is one
half of "checkpoint 6" (Public Trust Center and final hardening) — the
checkpoint comment for that group is posted once Feature 13 also lands.

## Files Changed

- `app/routers/trust_center_public.py` — `public_trust_center`,
  `public_policy_download`.
- `app/templates/trust_center/public.html` — standalone page (not
  `base.html`), meta description + Open Graph tags.
- `app/main.py` — wired the new router.
- `tests/test_trust_center_public.py` (11).

## Verification

- [x] Tests pass (`pytest` — 324 passed, 1 skipped)
- [x] Lint/format clean
- [x] Browser-verified end to end against a live dev server: set a
      section's visibility to "public" via the admin grid, published
      it, confirmed it appeared immediately on `/trust-center` with
      correctly rendered Markdown and no authenticated-shell chrome
      (no sidebar/nav); confirmed `Cache-Control: no-store` via
      `curl -D -`; no browser console errors.
- [x] Automated tests directly cover the leakage boundaries a browser
      session can't easily exercise: draft-vs-published content,
      internal-vs-public visibility, and all four policy-download
      gating combinations (approved+linked, not approved, not linked,
      linked-but-not-public/published).

## Decisions & Alternatives Rejected

- **Single stable public route, not per-section URLs.** The spec asks
  for "a stable public route" (singular); sections render as anchored
  `<section id="section-{id}">` blocks on one page rather than each
  getting its own URL — simpler, and matches how the admin UI already
  treats sections as an ordered list on one page.
- **No pinned policy version at publish time.** The download route
  always serves a linked policy's current `latest_version`, not a
  version snapshotted when the section was published. Pinning would
  require sections to store a `linked_policy_version_id` and a second
  publish-time snapshot path — deferred until an actual need for
  "the public download shouldn't change until I republish" appears
  (see Known Gaps).
- **Policy download requires both `status == "approved"` AND being
  currently linked from a public+published section** — either check
  alone is insufficient. Approved-only would make every approved
  policy's PDF fetchable by anyone who guesses/enumerates a policy id;
  linked-only would let an admin accidentally expose a draft policy.
  Both together keep the public surface exactly the curated subset an
  admin explicitly chose to publish.
- **`Cache-Control: no-store`** on both the page and the download
  response. This app has no reverse-proxy/CDN cache in its own
  architecture, so "cache invalidation" here means never letting an
  intermediary or browser serve a stale published/unpublished view
  from its own cache — cheaper to just disable caching than to build
  invalidation for a cache that doesn't exist yet.
- **No `noindex` meta tag when enabled.** An organization that
  explicitly enables its Trust Center most likely wants it
  discoverable; when disabled, the route 404s, which is itself a
  correct "nothing to index" signal to crawlers without adding
  separate robots configuration.
- **Restricted visibility still never renders publicly** — this
  route's query is `visibility == "public"` only; `"restricted"`
  sections behave exactly like `"internal"` ones here (invisible),
  consistent with the Feature 11 decision that "restricted" is a
  reserved schema value with no access-control workflow behind it yet.

## Known Gaps / Follow-ups

- No pinned per-publish policy version (see above) — a policy update
  after linking immediately changes what the public download serves.
- No Restricted-visibility gated access (NDA/customer login) — out of
  scope for this PR per the Feature 12 spec.
- No custom-domain support — architecture doesn't preclude it later,
  but nothing here builds toward it yet beyond being a plain route.
- Feature 13 (final hardening) still needs to run a comprehensive
  security/leakage review pass across this and every prior feature
  before the "Public Trust Center and final hardening" checkpoint is
  posted.
