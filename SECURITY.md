# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 1.1.x | ✅ current line |
| 1.0.x | see below |

The engine uses a hard-pinned version model: one release freezes (engine code, db format,
package format) together, and hosts depend on the exact version they installed. There is
no cross-version migration path, so security fixes are published as a new patch release
of the current line.

**How long an older line keeps getting fixes has not been decided yet.** Staying on an
old major to keep existing worlds alive would eventually mean running an unpatched
engine, and no one chose that — it fell out of two separately reasonable decisions. The
open question is tracked in [#8](https://github.com/aubrey-anima/core/issues/8); until it
is answered, treat only the current line as guaranteed. What does and does not survive a
major boundary is documented in `docs/REFERENCE.md` §2.12.

## Reporting a vulnerability

Email **aubrey@animametaverse.com** with the details. Please do not open a public issue
for a vulnerability.

Include what you can: the version, what an attacker can do, and a reproduction. You
should get an acknowledgement within a few days.

## What this engine does and does not protect

Worth being explicit, because the trust model is unusual for a package that looks like a
service.

**The trust boundary is the process boundary.** The engine is a library with no HTTP
layer and no authentication. `player_id` is just a parameter — the engine believes
whatever the caller says. Verifying that a request really comes from the user it claims
to be is **the host application's job**, not the engine's. If you expose a world over a
network, you wrap it yourself.

**`world.db.key` decrypts the API key.** The engine encrypts `llm.api_key` at rest with
Fernet, and the key material lives beside the database in `<db>.key` (mode 0600). This
protects the secret if the database alone leaks — a backup, a copied file, a shared
snapshot. It protects nothing if both files leak together.

Practical consequences:

- **Never attach `world.db.key` to a bug report, issue, or pull request.**
- A `.cyberworld` snapshot package contains a `world.db`. Treat it as sensitive if a real
  API key was ever configured in it.
- `world.db` and `world.db.key` are both excluded from the source distribution and from
  git (`MANIFEST.in`, `.gitignore`), but check what you are attaching anyway.

**LLM output is not sanitized.** Narration, chat replies, and planner output come back
from a model and are stored and returned as-is. If your application renders them in a
browser, escape them like any other untrusted string. The engine does not know what your
output medium is and does not guess.

**Seed and beat-script files are code-adjacent.** They define characters, locations, and
scripted events for a world. Validation checks structure, not intent — treat a
`.cyberworld` package from an untrusted source the way you would treat any untrusted data
file, and inspect it (`anima-world world import` lands it on disk without running it)
before pointing a world at it.

## Out of scope

- Denial of service from a locally-driven simulation (you control the tick loop).
- Anything requiring an attacker who already has read access to both `world.db` and
  `world.db.key` — that is equivalent to having the API key.
- Cost overruns from LLM usage. Use `--llm mock` or `needs.enabled false` style flags to
  bound spend; `anima-world doctor` reports what is configured.
