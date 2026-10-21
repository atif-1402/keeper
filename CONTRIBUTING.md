# Contributing to Keeper

Thanks for wanting to make Keeper better. It's a small tool with a strict
personality — a few principles keep it that way:

## The rules Keeper will never break

1. **Dry-run by default.** Nothing moves without `--apply`.
2. **Never deletes.** Losers are quarantined, not removed, and `--restore`
   must bring everything back byte-for-byte.
3. **Nothing leaves the machine.** No telemetry, no cloud calls, no API keys.
   The only network hop is `localhost:11434` (your own Ollama).
4. **Honest fallbacks.** When the model is down or unsure, say so — the rule
   engine takes over and the table explains why.

If your change needs Keeper to break one of these, it's probably a different
tool.

## Getting started

```bash
git clone https://github.com/atif-1402/keeper
cd keeper
uv sync
uv run pytest -q
```

No `uv`? A plain `python -m venv .venv && pip install -e . pytest` works too.

## Making a change

- **One idea per pull request.** Small PRs review fast and merge faster.
- **Tests come with the change.** `uv run pytest -q` must stay green; new
  behavior needs new tests in `tests/`.
- **Keep dependencies light.** Keeper ships as a single binary. Adding a
  dependency needs a good reason (it must earn its megabytes).
- **Commit style:** imperative mood, lowercase, no period —
  `add perceptual image hashing`, not "Added perceptual image hashing."
- **The model is replaceable.** Anything Keeper asks Gemma must degrade
  gracefully through `--no-model` and the rule engine.

## Where help is welcome

- More text extractors (`.eml`, `.epub`, `.rtf` done right)
- Windows long-path edge cases
- Better i18n for non-ASCII filenames
- Real-world cluster false-positive reports (with redacted file lists)

## Reporting a bug

Open an issue with: what you ran, what you expected, what happened, your
OS + Python version + Ollama model. If Keeper quarantined something it
shouldn't have, the `manifest.json` from that run is gold.

## Release process

Maintainers tag `vX.Y.Z`; CI builds binaries for every platform and publishes
a GitHub Release. Release names are exactly the version tag — nothing fancy.
