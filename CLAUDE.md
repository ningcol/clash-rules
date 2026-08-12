# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Automated Clash rule-set publisher. `scripts/build.py` reads `config.yaml`, downloads upstream rule sources, normalizes/validates/deduplicates them with a domain suffix tree, enforces a routing partition, and emits `final_<cat>.yaml` rule-provider files (behavior: `domain`, plus `final_<cat>_ipcidr.yaml` when a category has IP rules). Products are published to the **`release` branch** (not `main`); `main` holds only source and maintenance inputs. Subscription URLs point at `@release`.

## Commands

```bash
pip install -r scripts/requirements.txt          # PyYAML only

python scripts/build.py build --out dist          # build all products into dist/
python scripts/build.py build --out dist --previous release   # + all five gates vs last release
python scripts/build.py lint                       # validate manual/ files (+ reroute notes)
python scripts/build.py readme --check             # verify README table is in sync
python -m unittest discover -s tests               # run unit tests
```

Run the tests through `discover`, not `python tests/test_build.py`: the file used to
call `unittest.main()` from the middle of the module, so direct execution silently ran
only the classes defined above that line. Fixed, but `discover` is still the documented
path and the one CI uses.

`build` fetches from the network. `SOURCE_DATE_EPOCH` fixes the product timestamp (used by golden output). Requires Python 3.11+.

## Maintenance: only edit config.yaml and manual/

Products are generated — never hand-edit `final_*.yaml`. Every maintenance task is one of:

| Task | Edit |
|------|------|
| Add / replace an upstream source | `config.yaml` → the category's `sources` (url + note) |
| Add domains to a category | `manual/<cat>.txt` |
| Force a domain to a policy (e.g. always direct) | `manual/<target-cat>.txt` — **one file only** |
| Remove a domain from a product | `manual/<cat>-exclude.txt` — read the reroute warning below |
| Add a category / change priority / change a threshold | `config.yaml` |

Rules to follow:

- **One file for forced routing.** Pinning a domain to a category auto-removes it from every other routing category (see the partition below), so never mirror-edit another category's exclude file — that legacy double-write is exactly what this design eliminated.
- **`-exclude.txt` REROUTES unless the category is last in `priority`.** Exclusion runs *before* the partition, so dropping a domain from a high-priority category releases its claim and the next category that also carries it takes over — the traffic moves. Only the last routing category (`direct` today) excludes without that risk, because nothing below it can inherit. `lint` prints a note for every non-last category with a non-empty exclude file; the note is not an error, because whether the reroute is wanted depends on upstream content nobody has fetched at lint time.
  - Used deliberately this is the right tool for *releasing a bad claim*: microsoft's upstreams carry shared-infrastructure suffixes that are not Microsoft services (`+.edgesuite.net`, `+.akadns.net`, `+.trafficmanager.net`, `+.cloudapp.net`, `+.azureedge.net` — Akamai and Azure address space anyone can rent). Excluding those from microsoft hands them back to proxy/direct, which is what you want.
  - To send a domain to a *specific* policy, still pin it via `manual/<cat>.txt` — an exclude only says "not me", never "them".
- **Every manual entry needs a `# reason + date` comment** (batch imports: also the source URL). Lint checks syntax / duplicates / trailing newline / cross-category double-pins, but not intent — that comment is the only record of why a line exists. The comment must be on **its own line**: the parser reads any non-`#` line as a rule, so an inline `domain # reason` is parsed as an illegal rule and silently dropped.
- **Prefer the broadest correct suffix.** A specific host is redundant when a `+.suffix` already covers it (e.g. `y298.kdltps.com` under `+.kdltps.com`) — `compress()` drops it anyway. For a provider whose subdomains rotate (proxy tunnels, CDNs), pin the `+.suffix` once instead of chasing individual hosts.
- **`config.yaml` is the single source of truth.** Categories, sources, `priority`, and thresholds all live there; the README subscription table is generated from it (`readme` subcommand) — don't hand-edit that table.
- **Before pushing**, run `lint`, the unit tests, and `readme --check` (CI enforces all three on PR/push).
- **Pushing is publishing.** A push to `main` touching `config.yaml`, `manual/`, or `scripts/` auto-runs `publish.yml` → and, **when the built rules differ from the current release**, a new `release` commit + jsDelivr purge within minutes (no separate step; an unchanged rebuild is a no-op). The daily cron only exists to pick up upstream changes on days you don't push. Products go live to real subscribers, so treat a push as a release.

## Architecture: the routing partition

The routing categories listed in `config.yaml`'s `priority` (`microsoft, apple, icloud, proxy, direct`) form a **partition**: every domain lands in at most one of them. This is a correctness requirement, not a size optimization — subscribers order their `RULE-SET` lines arbitrarily, so routing determinism must live in the product content, not in config ordering.

Two things decide a domain's category, **manual assignment winning over priority**:

1. A domain in `manual/<cat>.txt` is *pinned* to `<cat>`: forced into it and removed from every other routing category, even a higher-priority one. This is why forcing a policy is a single-file edit.
2. Otherwise the domain goes to the highest-priority category it appears in and is removed from the rest.

`reject` is a policy overlay and does not participate in the partition. The suffix-tree engine (`DomainSet` in `scripts/build.py`) does the covering-relation work: `compress()` removes entries covered by an ancestor suffix, `subtract()` does priority/manual exclusion and reports `Conflict`s when a narrow rule can't be removed from a broader suffix (surfaced in the build report, not fatal).

Because domain-behavior format cannot trim a subdomain out of a `+.suffix`, the partition is not 100% clean, and it fails in **two** directions that the report keeps separate:

- **Exclusion impossible** → `## conflicts`. One category carries a broad suffix (direct's `+.mi.com`, proxy's shared-CA `+.digicert.com`) covering another category's specific domain, so the narrow rule cannot be subtracted out. Both rule sets match it and routing becomes order-dependent (~330 cases). The README's recommended RULE-SET order is the build's `priority` order, which resolves them correctly — keep the two in sync; `tests/test_build.py::TestRealConfig` pins it.
- **Exclusion succeeded** → `## partition transfers`. The broad suffix wins outright and the specific hosts are *deleted* from the losing category. This is the expensive direction and it used to be entirely unreported. Measured 2026-08: microsoft took 137 hosts from proxy (largely via Akamai/Azure suffixes it should not own), and proxy took 403 from direct — among them China-region endpoints of foreign vendors (`account-cn.alibabacloud.com`, `images-cn.ssl-images-amazon.com`, `ea2cn-prod-outlet.dell.com`) that upstream listed in direct precisely so they would *not* go out over a proxy. Watch the totals in the report; a suffix in that list that the winner does not actually own belongs in its `-exclude.txt`.

**apple and icloud are effectively manual-only today.** Their upstream contributes 0 and 2 net entries respectively — `manual/apple.txt`'s broad suffixes (`+.apple.com`, `+.mzstatic.com`, …) compress away everything the source provides. Consequence: a new Apple domain that those suffixes do not cover (the `akadns.net` / `edgekey.net` families) will **not** arrive on its own; it has to be added by hand.

## Safety mechanisms

- **Shrink gate** (`check_gate`): if any product shrinks more than `max-shrink-percent` (default 30) vs the last release — or a product that existed last release disappears entirely (a removed category, or a category whose IP rules vanished upstream) — the build fails and publishes nothing; the previous release stays live. Skipped on first publish. It is the coarsest of the five gates: it sees a category collapsing, and nothing finer.
- **Input validation**: illegal lines (bad wildcards like `*cdn.x`, bare IPs, `DOMAIN-KEYWORD`) are dropped and counted, never emitted. A **bare TLD is legal for a suffix rule** (`+.cn`, `+.icbc`, punycode `+.xn--fiqs8s`) and illegal for an exact one — upstream direct lists carry `+.cn` as the single line covering the whole ccTLD and list almost no individual `.cn` domains, so rejecting it drops the entire top-level domain out of direct rather than one rule. Equally, the leading-dot form must not be gated on "next char is not a digit": that rejects every domain whose first label is numeric.
- **Invalid-drop gate** (`check_invalid_gate`): a category whose upstream sources lose more than `max-invalid` lines fails the build and publishes nothing; the error carries the dropped samples. It is an **absolute count, defaulting to 0** — the `+.cn` regression was 1 line in 111,516, so any percentage threshold would have passed it while the whole `.cn` TLD leaked to the proxy. Breadth is unrelated to count, so routing categories get no slack. `reject` is the one exception (`max-invalid: 20`), and the reason is **not** "a dropped ad rule is harmless" — gate errors are collected globally and any one of them refuses the whole publish, so an over-limit reject freezes `direct` and `proxy` too. The reason is that reject's upstreams are hand-edited ad lists whose baseline noise (a constant 2 lines) the routing sources do not have; the allowance keeps that noise from taking the publish down. When rejected lines outnumber accepted ones the error says so explicitly — that is a format change or a missing entry in `UNSUPPORTED_TYPES`, not junk, and it needs a different fix. Raising a limit means reading the samples in the report and deciding they are genuine junk.
- **Bare-TLD gate** (`check_tld_gate`): a single-label suffix rule (`+.cn`, `+.icbc`, `+.xn--fiqs8s`) that was in the last release and is not in this one fails the build. The three gates above all count entries, and losing a whole TLD costs one entry — 0.0009% of `direct`. This one is the only thing standing between an upstream deletion of `+.cn` and a repeat of 2026-08-11. Measured: `direct` carries 50, `proxy` 71, every other category none, and zero churn across the last 12 releases, so it has no realistic false-positive rate. Escape hatch: `defaults.allow-tld-removal: true` for one run.
- **Per-source gate** (`check_source_gate`): a single upstream source whose parsed rule count collapses fails the build, even when its category barely moves. Baseline is `sources.json`, published to the release branch alongside the products. Two rules: parsed >0 last time and 0 now is always fatal; a percentage drop is only judged above 20 rules (ACL4SSR Bing contributes 3 — judging that by percentage would cry wolf on every one-line change). Measured 2026-08 by blanking each source in turn: **7 of the 10 sources could return an empty payload for under 8% category shrink**, and apple's only source could die with the product not changing by a single entry, because `manual/apple.txt` carries all of it. If the baseline file is missing the gate is skipped and the build forces one publish to create it; if the file exists but is unreadable that is an error, never a skip.
- **Failure is announced** — this is not decoration. A blocked publish is invisible from the subscriber's side: the previous release stays on the branch and every client keeps pulling it on schedule. `publish.yml` opens (or comments on) an issue whenever the run fails, and `heartbeat.yml` runs weekly to check how old the newest release commit is, covering the case where the publish workflow does not run at all. Neither can detect its own scheduled trigger being disabled — GitHub disables cron triggers after 60 days of repository inactivity, and the only recovery is re-enabling them by hand in the Actions tab.
- **CI**: `check.yml` validates on every PR/push (lint + tests + `readme --check` + a full validation build; never publishes). `publish.yml` builds → shrink-gates → publishes to the branch named by `defaults.publish-branch` (resolved from config, not hardcoded) → purges every product's jsDelivr cache. It fires three ways: daily cron `0 21 * * *` UTC = 05:00 Beijing (catches upstream drift), a push to `main` touching build inputs (`config.yaml`, `manual/`, `scripts/`, or `publish.yml`), and manual dispatch. The commit + purge happen **only when the built rules actually differ** from the current release: `build` writes `changed.txt` (a payload-only comparison that ignores the volatile timestamp header) and the publish step skips everything when it is `false`, so idle days are true no-ops. The purge is best-effort — it never fails the run. The push trigger is scoped to `branches: [main]` and the workflow only pushes the publish branch, so the release auto-commit cannot re-trigger it.

## Conventions

- README is written in Chinese; keep user-facing docs in Chinese.
- Adding a category: add it to `config.yaml` (and to `priority` if it should participate in the partition); the README table follows automatically.
