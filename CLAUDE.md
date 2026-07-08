# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Automated Clash rule-set publisher. `scripts/build.py` reads `config.yaml`, downloads upstream rule sources, normalizes/validates/deduplicates them with a domain suffix tree, enforces a routing partition, and emits `final_<cat>.yaml` rule-provider files (behavior: `domain`, plus `final_<cat>_ipcidr.yaml` when a category has IP rules). Products are published to the **`release` branch** (not `main`); `main` holds only source and maintenance inputs.

> **Migration in progress.** A legacy bash pipeline (`.github/scripts/build-rules.sh` + `.github/workflows/build-rules.yml`) still builds and commits `final_*.yaml` to `main`. It will be removed once the new pipeline's `release` branch is verified. Prefer the Python pipeline for all changes; do not extend the bash script.

## Commands

```bash
pip install -r scripts/requirements.txt          # PyYAML only

python scripts/build.py build --out dist          # build all products into dist/
python scripts/build.py build --out dist --previous release   # + shrink gate vs last release
python scripts/build.py lint                       # validate manual/ files
python scripts/build.py readme --check             # verify README table is in sync
python -m unittest discover -s tests               # run unit tests
```

`build` fetches from the network. `SOURCE_DATE_EPOCH` fixes the product timestamp (used by golden output). Requires Python 3.11+.

## Maintenance: only edit config.yaml and manual/

Products are generated — never hand-edit `final_*.yaml`. Change inputs instead:

- **`config.yaml`** — the single source of truth: categories, source URLs, `priority` order, thresholds. Adding a category / changing a source / adjusting priority all happen here. The README subscription table is generated from it (`readme` subcommand).
- **`manual/<cat>.txt`** — domains manually added to a category. Every entry should carry a `# reason + date` comment.
- **`manual/<cat>-exclude.txt`** — domains removed from a category's product without rerouting them (e.g. an ad source's false positive).

To **force a domain to a policy** (e.g. always direct), add it to `manual/<target-cat>.txt` — one file. See the partition rule below for why this suffices.

## Architecture: the routing partition

The routing categories listed in `config.yaml`'s `priority` (`microsoft, apple, icloud, proxy, direct`) form a **partition**: every domain lands in at most one of them. This is a correctness requirement, not a size optimization — subscribers order their `RULE-SET` lines arbitrarily, so routing determinism must live in the product content, not in config ordering.

Two things decide a domain's category, **manual assignment winning over priority**:

1. A domain in `manual/<cat>.txt` is *pinned* to `<cat>`: forced into it and removed from every other routing category, even a higher-priority one. This is why forcing a policy is a single-file edit.
2. Otherwise the domain goes to the highest-priority category it appears in and is removed from the rest.

`reject` is a policy overlay and does not participate in the partition. The suffix-tree engine (`DomainSet` in `scripts/build.py`) does the covering-relation work: `compress()` removes entries covered by an ancestor suffix, `subtract()` does priority/manual exclusion and reports `Conflict`s when a narrow rule can't be removed from a broader suffix (surfaced in the build report, not fatal).

Because domain-behavior format cannot trim a subdomain out of a `+.suffix`, the partition is not 100% clean: when a lower/other category carries a broad suffix (e.g. proxy's `+.mzstatic.com`) that covers a higher category's specific domain (apple's `a1.mzstatic.com`), both match it and routing becomes order-dependent for that domain (~tens of cases, all in the conflict report). The README's recommended RULE-SET order is the build's `priority` order, which resolves them correctly — so keep those two in sync.

## Safety mechanisms

- **Shrink gate** (`check_gate`): if any product shrinks more than `max-shrink-percent` (default 30) vs the last release, the build fails and publishes nothing — the previous release stays live. Skipped on first publish. This is why a dead upstream source degrades gracefully instead of shipping a truncated list.
- **Input validation**: illegal lines (bad wildcards like `*cdn.x`, bare IPs, `DOMAIN-KEYWORD`) are dropped and counted, never emitted.
- **CI**: `check.yml` runs lint + tests + a dry-run build on every PR/push; `publish.yml` builds, gates, and pushes to `release` daily (cron `0 21 * * *` UTC = 05:00 Beijing) and on manual dispatch.

## Conventions

- README is written in Chinese; keep user-facing docs in Chinese.
- Adding a category: add it to `config.yaml` (and to `priority` if it should participate in the partition); the README table follows automatically.
