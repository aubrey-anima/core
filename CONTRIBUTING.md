# Contributing to anima-world

Thanks for looking. This document covers how to get set up, how the tests are meant to
be read, and — most importantly — the handful of invariants that a change must not
break. Most of them are not obvious from reading a single file, which is exactly why
they are written down.

## Setup

```bash
git clone https://github.com/aubrey-anima/core.git anima-world
cd anima-world
pip install -e ".[dev]"      # Python 3.11+
python -m pytest             # should be all green before you change anything
```

No services to start, no fixtures to seed. Every test runs offline against a temporary
SQLite file with a Mock LLM.

## How the tests are meant to be read

**These are not unit tests.** They verify the *package* and its *build artifacts* — the
db-format interlock, what actually ships in the wheel, the `World` facade's behavior, and
whether each of the four subsystems does something observable when you switch it on.
Nothing tests a function in isolation.

Two consequences:

- Tests are excluded from the sdist (`MANIFEST.in` prunes them). They are for developing
  the engine, not for shipping with it.
- **A regression test must be verified red before you make it green.** Write the test,
  run it, watch it fail for the reason you expect, *then* fix the code. A test that was
  green before your fix was testing something else.

The second rule has teeth. While writing the clock-persistence regression in 1.0.0, the
first version of the test passed about half the time — the world's event density varies
run to run, so a fixed tick count sometimes landed on an eventful tick and sometimes did
not. If it had been written and committed without checking that it went red, it would
have "verified" a fix that did nothing. The final test constructs the failing condition
explicitly (tick until a tick produces no event) instead of hoping for it.

## Invariants a change must not break

These are load-bearing. If your change touches one, say so in the pull request.

**One lock.** `scheduler.py` holds the only `RLock` in the system; it guards the world
clock, the projection, and the mailbox. Do not introduce a second lock — two locks is
where deadlocks come from, and there is no ordering discipline written down because
there has never needed to be one.

**The LLM is never called on the tick thread.** Narration, planning, and relationship
judging each run on their own thread pool. A blocking network call inside `tick()` would
make the world's clock hostage to an API's p99. The client is injected, never constructed
inline.

**The event log is the only source of truth for "what happened."** Balances,
relationships, and locations are projections. Do not add a table that stores a value the
log already implies — two sources of truth eventually disagree, and you cannot tell which
one is right. The criterion: *"something happened"* goes in the event log, *"what is it
now"* goes in a data-plane table (see `agent_needs` and `db_meta.clock` for the pattern).

**Authored input fails at load, loudly.** A beat script or a seed file that the caller
explicitly named must raise (`BeatScriptError` / `WorldSeedError`) rather than degrade.
Only the *bundled* seed falls back to defaults, because a damaged install should still
boot. The reasoning: a seed is read once into an empty database, so a silent fallback is
unrecoverable — the user gets the built-in demo world permanently, and fixing the path
later does not help.

**Degradation is never silent.** A world with no usable API key still runs, but it must
say so: at boot, in `anima-world doctor`, and permanently in
`World.state()["runtime"]["llm"]["degraded_reason"]`. The store distinguishes "never
configured" from "configured but the keyfile is missing" because their fixes are
opposite.

**`api.py` is add-only.** The `World` facade is what host applications depend on.
Removing or changing a method is a breaking change for every repository that imports this
one.

**Version is db format.** The major version number *is* the db format version. Bump the
first digit only when the database schema changes; second for capability, third for
fixes. `tests/test_version_contract.py` enforces this mechanically.

## Cross-repository contracts

Three modules define file formats that other repositories mirror. Changing their **wire
format** is a cross-repo breaking change, not a local one:

| Module | Format | Mirrored by |
|---|---|---|
| `anima_world/world_package.py` | `.cyberworld` package | operator console `lib/worldPackage.js` |
| `anima_world/world_seed.py` | seed schema validation | operator console `lib/worldSeed.js` |
| `anima_world/beats.py` | beat-script validation | (no mirror; the authoring tool validates via CLI) |

If you change one, the mirror has to change with it. `world_seed.py` has a specific
shape worth preserving: `is_valid_world_seed` returns the bare verdict that the mirror
implements, and `world_seed_errors` only *explains* that verdict — it must never change
it. `tests/test_packaging.py` pins the two in lockstep.

Three more contracts are behavioral rather than file formats. They are load-bearing for
hosts that manage several engine versions at once, and each is now pinned by a test —
because all three used to hold by accident, where a reasonable refactor would break
already-released external tools with nothing going red here.

**`anima_world/__init__.py` and `db.py` must import only the standard library.** Version
identification runs in a throwaway `--no-deps` virtualenv, so identifying an engine costs
one download instead of a whole dependency tree:

```bash
pip install --no-deps anima-world==X.Y.Z
python -c "import anima_world; print(anima_world.__version__)"
python -c "from anima_world.db import DB_FORMAT_VERSION, MIN_SUPPORTED_DB_FORMAT"
```

Adding a convenience re-export to `__init__.py` (`from anima_world.api import World`) is
what breaks this, and it is an extremely natural thing to write.

**`DB_FORMAT_VERSION` and `MIN_SUPPORTED_DB_FORMAT` are read by external tools** at those
import paths, to decide whether a world file and an engine belong together. Do not move
them into a private module.

**`simulate --ticks 0` means "initialize and stop".** It is the only way to build a world
database headlessly — there is no `init` subcommand. A future "reject non-positive tick
counts" validation would silently remove that ability.

## Making a change

1. Open an issue first for anything beyond a bug fix. Worth knowing before you write
   code: whether the behavior is deliberate. Several things that look like bugs are
   documented decisions in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
2. Branch from `main`.
3. Add a test that goes red first, then make it green.
4. Run the full suite: `python -m pytest`.
5. If you touched the CLI, the packaged data, or a file format, also verify the built
   artifact — a wheel can differ from the working tree:
   ```bash
   python -m build && pip install --force-reinstall dist/*.whl
   ```
6. Open a pull request describing what broke, what you changed, and which invariant (if
   any) the change touches.

## Style

Match the surrounding code. A few things that are consistent throughout and worth
keeping:

- Comments explain *why*, never *what*. If a comment restates the line below it, delete
  it. The valuable ones record a constraint the code cannot show — a failure that was
  actually hit, a reason an obvious approach does not work.
- Docstrings on anything with a non-obvious contract, especially the ones that describe
  degradation behavior.
- Existing code and comments are in Chinese in places and English in others, following
  whichever the surrounding file uses. Do not translate a file as a side effect of
  editing it.

## Releasing (maintainers)

Releases are automated. There is no API token anywhere — PyPI trusts
`.github/workflows/release.yml` directly through OIDC.

1. Bump `__version__` in `anima_world/__init__.py`. It is the single version source;
   `pyproject.toml` reads it dynamically. Remember that the **major version is the db
   format version** — bump the first digit only if the database schema changed.
2. Update `CHANGELOG.md`.
3. Commit, then tag and push:

   ```bash
   git tag -a v1.0.1 -m "..." && git push origin v1.0.1
   ```

Everything reaches TestPyPI before it reaches PyPI:

```
verify → build → testpypi → smoke → pypi
```

| Stage | What it proves |
|---|---|
| `verify` | The suite passes on 3.11/3.12/3.13, and the tag agrees with `__version__` |
| `build` | The artifact builds, `twine check` passes, and the wheel runs a world in a clean venv |
| `testpypi` | The artifact uploads to a real index |
| `smoke` | `pip install anima-world==X.Y.Z` **from that index** works, and the installed package runs a world |
| `pypi` | Only now, and only the exact bytes that stage tested |

The build happens **once** and every later stage consumes that artifact. Building per
target would mean TestPyPI validated one set of bytes while PyPI received another, which
would make the staging run prove nothing.

The `smoke` stage is the one that cannot be replaced by a local check: it installs from
an index the way a user does, which is the only way to catch a package that builds fine
but cannot actually be resolved and installed. It needs `--extra-index-url` pointing at
real PyPI, because `cryptography`, `openai`, and `httpx` do not exist on TestPyPI.

This matters because **PyPI never lets a version be re-uploaded.** A bad 1.0.1 cannot be
replaced, only yanked and followed by 1.0.2. TestPyPI is the last place a mistake is
still free.

To rehearse without releasing, run the workflow manually from the Actions tab and pick
`testpypi` — it stops after `smoke`. This needs a **separate** trusted publisher
registered at [test.pypi.org](https://test.pypi.org): a different service with different
accounts, where the PyPI publisher grants nothing. Without it the run fails at the
TestPyPI step with `invalid-publisher` while every field is in fact correct.

One caveat on reruns: TestPyPI uploads use `skip-existing`, so re-running the *same*
version leaves the earlier upload in place and `smoke` then exercises those older files.
For a real release the version is always new, so this only affects repeated rehearsals.

## Reporting bugs

Include the engine version (`python -c "import anima_world; print(anima_world.__version__)"`),
Python version, and the output of `anima-world doctor --skip-probe` if the world exists.
**Never attach a `world.db.key`** — it decrypts the API key stored in the database. See
[SECURITY.md](SECURITY.md).

## Code of conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
