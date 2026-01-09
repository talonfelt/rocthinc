# rocthinc — MVP

- Safari PWA (manifest + service worker)
- Notion-like page with anchored blocks: `code:thinc`, `code:py`, `table`, `console`, `glyph:var`
- Pyodide worker (Python in browser): INPUT/OUTPUT bridge
- `thinc` DSL with `roc target.port(value)` to route values
- Theme editor (every color editable)
- Supabase auth (magic link)
- Square Subscriptions $7/mo hosted checkout
- License check; **code run + team editing are behind paywall**

See `/app/page.tsx` for the main canvas. Add env vars from `.env.example` on Vercel and deploy.
