# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Automated Clash rule-set publisher. `scripts/build.py` reads `config.yaml`, downloads upstream rule sources, normalizes/validates/deduplicates them with a domain suffix tree, enforces a routing partition, and emits `final_<cat>.yaml` rule-provider files (behavior: `domain`, plus `final_<cat>_ipcidr.yaml` when a category has IP rules). Products are published to the **`release` branch** (not `main`); `main` holds only source and maintenance inputs. Subscription URLs point at `@release`.

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

Products are generated — never hand-edit `final_*.yaml`. Every maintenance task is one of:

| Task | Edit |
|------|------|
| Add / replace an upstream source | `config.yaml` → the category's `sources` (url + note) |
| Add domains to a category | `manual/<cat>.txt` |
| Force a domain to a policy (e.g. always direct) | `manual/<target-cat>.txt` — **one file only** |
| Remove a domain from a product without rerouting it | `manual/<cat>-exclude.txt` |
| Add a category / change priority / change a threshold | `config.yaml` |

Rules to follow:

- **One file for forced routing.** Pinning a domain to a category auto-removes it from every other routing category (see the partition below), so never mirror-edit another category's exclude file — that legacy double-write is exactly what this design eliminated.
- **`-exclude.txt` means "delete without reroute" only** — e.g. a false positive from an ad source you want to stop blocking. To send a domain to a different policy, pin it via `manual/<cat>.txt` instead.
- **Every manual entry needs a `# reason + date` comment** (batch imports: also the source URL). Lint checks syntax / duplicates / trailing newline / cross-category double-pins, but not intent — that comment is the only record of why a line exists. The comment must be on **its own line**: the parser reads any non-`#` line as a rule, so an inline `domain # reason` is parsed as an illegal rule and silently dropped.
- **Prefer the broadest correct suffix.** A specific host is redundant when a `+.suffix` already covers it (e.g. `y298.kdltps.com` under `+.kdltps.com`) — `compress()` drops it anyway. For a provider whose subdomains rotate (proxy tunnels, CDNs), pin the `+.suffix` once instead of chasing individual hosts.
- **`config.yaml` is the single source of truth.** Categories, sources, `priority`, and thresholds all live there; the README subscription table is generated from it (`readme` subcommand) — don't hand-edit that table.
- **Before pushing**, run `lint`, the unit tests, and `readme --check` (CI enforces all three on PR/push).
- **Pushing is publishing.** A push to `main` touching `config.yaml`, `manual/`, or `scripts/` auto-runs `publish.yml` → a new `release` commit + jsDelivr purge within minutes (no separate step). The daily cron only exists to pick up upstream changes on days you don't push. Products go live to real subscribers, so treat a push as a release.

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
- **CI**: `check.yml` validates on every PR/push (lint + tests + dry-run build; never publishes). `publish.yml` builds → shrink-gates → pushes to `release` → purges jsDelivr for each changed product. It fires three ways: daily cron `0 21 * * *` UTC = 05:00 Beijing (catches upstream drift), a push to `main` touching build inputs (`config.yaml`, `manual/`, `scripts/`, or `publish.yml`), and manual dispatch. The jsDelivr purge is gated on an actual product change (the publish step's `published` output) and is best-effort — it never fails the run. The push trigger is scoped to `branches: [main]` and `publish.yml` only pushes the `release` branch, so the release auto-commit cannot re-trigger the workflow.

## Conventions

- README is written in Chinese; keep user-facing docs in Chinese.
- Adding a category: add it to `config.yaml` (and to `priority` if it should participate in the partition); the README table follows automatically.
