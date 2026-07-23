# What this changes

<!-- What was broken or missing, and what you did about it. -->

## Why

<!-- The reasoning. If this fixes a bug, what was the actual failure? -->

## Invariants

Check any this change touches, and explain in the section above
(see [CONTRIBUTING.md](../CONTRIBUTING.md) for what each one means):

- [ ] The single `RLock` in `scheduler.py`
- [ ] LLM calls staying off the tick thread
- [ ] The event log as the only source of truth for "what happened"
- [ ] Authored beat scripts / seeds failing at load rather than degrading
- [ ] Degradation never being silent
- [ ] `api.py`'s `World` facade (add-only — removals break every host)
- [ ] The db format version, and therefore the major version
- [ ] A cross-repo file format (`world_package.py`, `world_seed.py`, `beats.py`)
- [ ] None of the above

## Testing

- [ ] `python -m pytest` passes
- [ ] Added a regression test, and **confirmed it fails without the fix**
- [ ] If this touched the CLI, packaged data, or a file format: verified against a built
      wheel, not just the working tree (`python -m build && pip install --force-reinstall dist/*.whl`)

<!--
On confirming a test fails first: this is not ceremony. A regression test written after
the fix and never seen red may be testing nothing. One of the 1.0.0 tests passed about
half the time on its first draft because world event density varies run to run — it would
have "verified" a fix that did nothing.
-->
