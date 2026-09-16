# Readable covers release 2.1.1

User authorization: add book-cover thumbnails, enlarge text and expose size controls, publish to GitHub and make the update available automatically.

- [x] Larger default18px, title20.25px, metadata18px; five persisted size steps16–24 with A−/A+.
- [x] ISBN-validated lazy thumbnails with equal-size accessible missing-cover placeholders.
- [x] Bounded provider lookup/cache and actual-cover validation; credits to source providers.
- [x] UI, backend, accessibility and browser checks; source/release review.
- [x] Build and verify Windows installer and portable archive with exact asset names and SHA256.
- [ ] Publish source/tag/stable GitHub release only after verification; no forced replacement of remote history.
- [ ] Check official latest metadata from older clients, download/hash verification, and queue update in the running installed app. Installation follows the existing normal-exit protocol.

Existing 2.1.0 changes were local only and must ship in this cumulative release from public2.0.3. Keep existing saved display and automatic-update preferences. Review/test data remains separate from the live library database.
