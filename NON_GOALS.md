# Non-goals

What Rekoll deliberately will **not** do. This list exists so we can say "no"
in one link and keep the project focused and trustworthy. (A popular memory tool
that tries to be everything ends up trusted by no one.)

- **No required cloud.** Rekoll always runs fully on your own infrastructure.
  A hosted convenience tier may exist *later*, but it will never be required and
  will never gate features (see the "open seam, nothing gated" design).
- **No telemetry / phone-home.** Not in the default install, not ever by default.
  Any future metric is strictly opt-in and locally visible.
- **No gated/"open-core" features.** Everything in this repo is MIT and complete.
  We are not crippling the free version to upsell.
- **No locking you in.** Your data is verbatim, exportable, and lives in a
  database you control. There is always a documented way out.
- **Not an agent framework.** Rekoll is a memory *layer* you drop into your own
  agent — not a platform you build inside. It augments; it doesn't take over.
- **Not a vector database.** It uses one (yours, or a bundled local one); it is
  the memory logic on top, not a competitor to Postgres/Qdrant/etc.
- **No heavy built-in UI in v1.** The product is a library + MCP server + small
  CLI. Dashboards can come later or live elsewhere.
- **We will not claim "unhackable."** We ship defenses, measure them, and are
  honest about what they do and don't cover.

If a request falls under a non-goal, we'll likely decline and link here — kindly.
