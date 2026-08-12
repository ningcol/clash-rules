#!/usr/bin/env python3
"""Clash rule-set builder.

Pipeline: fetch upstream sources -> parse/normalize/validate -> per-category
suffix-tree dedup -> manual pinning + partition across routing categories ->
shrink gate -> emit final_<cat>.yaml. Config lives entirely in config.yaml.

Subcommands:
  build   --out DIR [--previous DIR]               build and write products
  lint                                             validate manual/ files
  readme  [--check]                                regenerate README table

Routing categories (those listed in `priority`) form a partition: every domain
lands in at most one of them, so routing is deterministic regardless of how a
subscriber orders their RULE-SET lines. Manual assignment (manual/<cat>.txt)
overrides priority: a domain there is pinned to <cat> and removed from all other
routing categories. reject is a policy overlay and does not participate.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

import yaml

# ---------------------------------------------------------------------------
# Constants / validation
# ---------------------------------------------------------------------------
# Labels: allow underscore (some upstream lists use it); 1-63 chars, no leading/
# trailing hyphen. A domain is >=2 labels.
_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
DOMAIN_RE = re.compile(rf"^(?:{_LABEL}\.)+{_LABEL}$")
# A suffix rule may be a bare TLD (`+.cn`, `+.icbc`, punycode `+.xn--fiqs8s`).
# Upstream direct lists carry `+.cn` as ONE line covering the whole ccTLD and
# therefore list almost no individual .cn domains — so requiring >=2 labels here
# does not drop one rule, it drops the entire .cn top-level domain from direct
# and every Chinese .cn site falls through to the catch-all group (measured:
# 12306.cn / gov.cn / edu.cn / quark.cn / 189.cn all leaked to the proxy, which
# is how ~57 GiB of domestic cloud-drive traffic went out over a paid VPS).
# The drop is silent: it is counted as `dropped_invalid` and never fails a build.
# Bare TLDs stay illegal for EXACT rules — `DOMAIN,cn` matches nothing real.
TLD_RE = re.compile(rf"^{_LABEL}$")
ASN_RE = re.compile(r"^as[0-9]+$")
# Rule types this builder turns into output. The dispatch in parse_line reads
# this set — do not keep a second hardcoded copy: two lists that must agree,
# with only one of them live, is the same silent-drift shape as everything else
# guarded in this file.
HANDLED_TYPES = {"DOMAIN", "DOMAIN-SUFFIX", "IP-CIDR", "IP-CIDR6", "IP-ASN"}
# Real Clash/mihomo/Surge rule types we knowingly do not emit. Skipping is only
# safe for names on THIS list: matching "anything that looks like a type token"
# instead would swallow a typo — `DOMAIN-SUFIX,cn` would be dropped with no
# signal at all, re-opening the very silent-drop hole the invalid gate exists to
# close, just through a new door. An unrecognised all-caps token is therefore
# 'invalid' (loud) rather than 'unsupported' (quiet): a genuinely new upstream
# type costs one line here, a typo costs a real rule.
UNSUPPORTED_TYPES = {
    # domain matchers we cannot express in a `behavior: domain` payload
    "DOMAIN-REGEX", "DOMAIN-WILDCARD", "HOST", "HOST-SUFFIX", "HOST-KEYWORD",
    "USER-AGENT", "URL-REGEX", "GEOSITE", "DOMAIN-SET",
    # ip matchers outside our two cidr kinds
    "GEOIP", "SRC-GEOIP", "IP-SUFFIX", "SRC-IP-CIDR", "SRC-IP-SUFFIX",
    "IP6-CIDR", "SRC-IP", "IP-CIDR-SET", "SRC-IP-ASN", "SUBNET",
    # connection / process / port / misc matchers
    "SRC-PORT", "DST-PORT", "DEST-PORT", "IN-PORT", "IN-TYPE", "IN-USER",
    "IN-NAME", "NETWORK", "PROTOCOL", "DSCP", "UID", "PROCESS-NAME",
    "PROCESS-PATH", "PROCESS-NAME-REGEX", "PROCESS-PATH-REGEX", "SCRIPT",
    "CELLULAR-RADIO", "DEVICE-NAME",
    # composition / control
    "AND", "OR", "NOT", "RULE-SET", "SUB-RULE", "MATCH", "FINAL",
}

ROUTING_HEADER = "# 说明: 本文件为自动生成的 Clash {up} 规则（behavior: domain）。"
IP_HEADER = "# 说明: 本文件为自动生成的 Clash {up} IP规则（behavior: ipcidr）。"


@dataclass(frozen=True)
class Rule:
    kind: str   # exact | suffix | ip-cidr | ip-cidr6 | ip-asn
    value: str


# ---------------------------------------------------------------------------
# Parsing / normalization
# ---------------------------------------------------------------------------
def _normalize_domain(v: str, *, allow_tld: bool = False) -> str | None:
    """Lowercase, strip trailing dot, IDNA-encode non-ASCII. Return None if invalid.

    allow_tld admits a single-label value (a bare TLD). Only suffix rules may set
    it; see TLD_RE above for what breaks when they cannot.
    """
    v = v.strip().rstrip(".").lower()
    if not v or "*" in v or ":" in v or "@" in v or "/" in v:
        return None
    if not v.isascii():
        # Punycode non-ASCII labels. NOTE: the stdlib 'idna' codec is IDNA2003;
        # a few characters (ß, ς, ZWJ/ZWNJ) map differently than IDNA2008/UTS46.
        # Upstream lists are ASCII so this path is effectively unused, and we
        # keep deps to PyYAML only rather than pull in the `idna` package.
        try:
            v = v.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            return None
    if not (DOMAIN_RE.match(v) or (allow_tld and TLD_RE.match(v))):
        return None
    # Reject bare IPs (no mask): a real TLD is never all-numeric.
    if v.rsplit(".", 1)[-1].isdigit():
        return None
    return v


def classify_value(v: str) -> Rule | None:
    """Turn one bare token into a Rule, or None if unrecognized/invalid."""
    v = v.strip()
    if not v:
        return None
    low = v.lower()

    # IP-ASN
    if ASN_RE.match(low):
        return Rule("ip-asn", low.upper())

    # CIDR (has a slash) — validate and normalize.
    if "/" in v:
        try:
            import ipaddress
            net = ipaddress.ip_network(v, strict=False)
        except ValueError:
            return None
        kind = "ip-cidr6" if net.version == 6 else "ip-cidr"
        return Rule(kind, str(net))

    # Suffix forms: +.x  *.x  .x
    if low.startswith("+."):
        d = _normalize_domain(low[2:], allow_tld=True)
        return Rule("suffix", d) if d else None
    if low.startswith("*."):
        d = _normalize_domain(low[2:], allow_tld=True)
        return Rule("suffix", d) if d else None
    # Leading-dot suffix form. Do NOT gate this on "second char is not a digit":
    # that guard was meant to keep `.1.2.3` from becoming a suffix rule, but it
    # also rejects every domain whose first label starts with a digit, and those
    # are common in Chinese lists (numeric brand names). _normalize_domain
    # already rejects bare IPs via its all-numeric-last-label check, so the
    # guard bought nothing and silently dropped ~9.5k rules from one candidate
    # source.
    if low.startswith("."):
        d = _normalize_domain(low[1:], allow_tld=True)
        return Rule("suffix", d) if d else None

    # Bare domain.
    d = _normalize_domain(low)
    return Rule("exact", d) if d else None


@dataclass
class ParseStats:
    total: int = 0
    parsed: int = 0
    dropped_invalid: int = 0
    dropped_keyword: int = 0
    dropped_unsupported: int = 0
    invalid_samples: list[str] = field(default_factory=list)
    unsupported_types: set[str] = field(default_factory=set)


def parse_line(line: str) -> tuple[str, Rule | None]:
    """Return ('ok'|'skip'|'keyword'|'unsupported'|'invalid', rule).

    'unsupported' means a well-formed rule of a type this builder does not
    emit (regex/process/port/logic rules...). It is NOT an error — see
    UNSUPPORTED_TYPES for why conflating it with 'invalid' takes the site down.
    """
    # Strip a UTF-8 BOM before anything else. fetch_url decodes as plain utf-8,
    # so a BOM stays glued to the first character; the first line of three of
    # the microsoft sources is a `#` comment, and `﻿#...` is neither a
    # comment nor a rule -> 'invalid' -> with the gate at 0, one upstream edit
    # from a Windows editor takes the whole publish down.
    s = line.lstrip("\ufeff").rstrip("\r\n").strip()
    # Trailing comment after a rule (`DOMAIN-SUFFIX,cn # China`) is common in
    # .list files; without this the whole line is judged as one token.
    if "#" in s:
        head = s.split("#", 1)[0].rstrip()
        if head:
            s = head
    if not s or s.startswith("#") or s.startswith("!"):
        return "skip", None
    # YAML scaffolding: document markers, directives, empty payload.
    if s in ("---", "...", "payload: []", "payload: {}") or s.startswith("%YAML"):
        return "skip", None
    if s == "payload:" or s.rstrip().endswith("payload:"):
        return "skip", None

    # YAML list item:  - 'x'   or   - x
    if s.startswith("- "):
        s = s[2:].strip().strip("'\"")

    # Clash text form:  TYPE,value[,extra]
    if "," in s:
        parts = [p.strip() for p in s.split(",")]
        t = parts[0].upper()
        if t == "DOMAIN-KEYWORD":
            return "keyword", None
        if t in HANDLED_TYPES:
            val = parts[1] if len(parts) > 1 else ""
            if t == "DOMAIN":
                d = _normalize_domain(val)
                return ("ok", Rule("exact", d)) if d else ("invalid", None)
            if t == "DOMAIN-SUFFIX":
                d = _normalize_domain(val, allow_tld=True)
                return ("ok", Rule("suffix", d)) if d else ("invalid", None)
            if t in ("IP-CIDR", "IP-CIDR6"):
                # Validate against the DECLARED type instead of re-sniffing the
                # value. Delegating to classify_value made `IP-CIDR,+.foo.com`
                # parse as a domain suffix rule and land in the category's
                # DOMAIN payload — a malformed IP line silently routing a whole
                # suffix, past a green gate.
                if "/" not in val:
                    return "invalid", None
                r = classify_value(val)
                ok = r is not None and r.kind in ("ip-cidr", "ip-cidr6")
                return ("ok", r) if ok else ("invalid", None)
            if t == "IP-ASN":
                # Clash's text form carries the bare number (`IP-ASN,13335`);
                # only the standalone token form is written `AS13335`. Sending
                # the bare number through classify_value made the canonical
                # spelling parse as a malformed domain -> 'invalid' -> with the
                # gate at 0, one such upstream line fails the whole build.
                low = val.lower()
                if low.isdigit():
                    return "ok", Rule("ip-asn", f"AS{low}")
                if ASN_RE.match(low):
                    return "ok", Rule("ip-asn", low.upper())
                return "invalid", None
        # A rule type we knowingly do not emit. Match on the UPPERCASED token:
        # the handled-type dispatch above is case-insensitive, so testing the
        # raw token here would make the builder lenient for the six types it
        # parses and strict for every type it skips — one lowercase
        # `process-name,...` line upstream and the whole build fails.
        if t in UNSUPPORTED_TYPES:
            return "unsupported", None
        # An all-caps token we do not recognise is a typo until proven
        # otherwise — see UNSUPPORTED_TYPES. Fall through: 'invalid', loudly.

    r = classify_value(s)
    return ("ok", r) if r else ("invalid", None)


def parse_text(text: str) -> tuple[list[Rule], ParseStats]:
    rules: list[Rule] = []
    st = ParseStats()
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        st.total += 1
        status, rule = parse_line(line)
        if status == "ok" and rule is not None:
            rules.append(rule)
            st.parsed += 1
        elif status == "keyword":
            st.dropped_keyword += 1
        elif status == "unsupported":
            st.dropped_unsupported += 1
            head = line.strip().lstrip("- ").strip("'\"").split(",", 1)[0].upper()
            st.unsupported_types.add(head)
        elif status == "invalid":
            st.dropped_invalid += 1
            if len(st.invalid_samples) < 10:
                st.invalid_samples.append(line.strip())
        # 'skip' lines (payload:, yaml scalars) are not counted as content.
    return rules, st


# ---------------------------------------------------------------------------
# DomainSet — reverse-label trie with exact/suffix flags
# ---------------------------------------------------------------------------
class _Node:
    __slots__ = ("children", "exact", "suffix")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.exact = False
        self.suffix = False


@dataclass
class Conflict:
    detail: str


class DomainSet:
    def __init__(self) -> None:
        self.root = _Node()

    @staticmethod
    def _labels(domain: str) -> list[str]:
        return domain.split(".")[::-1]

    def add(self, rule: Rule) -> None:
        node = self.root
        for lbl in self._labels(rule.value):
            node = node.children.setdefault(lbl, _Node())
        if rule.kind == "suffix":
            node.suffix = True
        else:
            node.exact = True

    @classmethod
    def from_rules(cls, rules: Iterable[Rule]) -> "DomainSet":
        ds = cls()
        for r in rules:
            if r.kind in ("exact", "suffix"):
                ds.add(r)
        return ds

    def __len__(self) -> int:
        return sum(1 for _ in self.iter_rules())

    def iter_rules(self) -> Iterator[Rule]:
        def walk(node: _Node, labels: list[str]) -> Iterator[Rule]:
            if labels:
                dom = ".".join(labels[::-1])
                if node.suffix:
                    yield Rule("suffix", dom)
                if node.exact:
                    yield Rule("exact", dom)
            for lbl, child in node.children.items():
                yield from walk(child, labels + [lbl])
        yield from walk(self.root, [])

    def _count_subtree(self, node: _Node, include_self_exact: bool) -> int:
        n = 0
        if include_self_exact and node.exact:
            n += 1
        for child in node.children.values():
            if child.exact:
                n += 1
            if child.suffix:
                n += 1
            n += self._count_subtree(child, include_self_exact=False)
        return n

    def compress(self) -> int:
        """Remove entries covered by an ancestor suffix. Return removed count."""
        removed = 0

        def dfs(node: _Node) -> None:
            nonlocal removed
            if node.suffix:
                # This suffix covers its own exact and the whole subtree.
                removed += self._count_subtree(node, include_self_exact=True)
                node.exact = False
                node.children = {}
                return
            for child in node.children.values():
                dfs(child)

        dfs(self.root)
        return removed

    def _find(self, domain: str) -> _Node | None:
        node = self.root
        for lbl in self._labels(domain):
            node = node.children.get(lbl)
            if node is None:
                return None
        return node

    def _ancestor_suffix(self, domain: str, strict: bool) -> bool:
        """True if a (strict) ancestor node on the path carries suffix=True."""
        node = self.root
        labels = self._labels(domain)
        last = len(labels) - 1
        for i, lbl in enumerate(labels):
            node = node.children.get(lbl)
            if node is None:
                return False
            if node.suffix and (i < last if strict else True):
                return True
        return False

    def covering_suffix(self, domain: str) -> str | None:
        """The most specific suffix rule in this set that covers `domain`.

        Used only for reporting: when the partition takes a domain away from a
        category, the useful thing to print is not the domain but the one broad
        suffix that swallowed it — a single `+.kaspersky.com` accounts for
        dozens of lost hosts, and that suffix is what you would put in an
        exclude file.
        """
        node = self.root
        labels = self._labels(domain)
        best = None
        for i, lbl in enumerate(labels):
            node = node.children.get(lbl)
            if node is None:
                break
            if node.suffix:
                best = ".".join(labels[:i + 1][::-1])
        return best

    def covered(self, rule: Rule) -> bool:
        """Is this rule already implied by the set (ancestor/own suffix, or exact)?"""
        if self._ancestor_suffix(rule.value, strict=False):
            return True
        node = self._find(rule.value)
        if node is None:
            return False
        return node.exact if rule.kind == "exact" else node.suffix

    def subtract(self, other: "DomainSet") -> tuple[int, list[Conflict]]:
        """Remove other's rules from self. Return (removed, conflicts).

        `other` is left untouched: compression happens on a throwaway copy.
        It used to compress `other` in place, which quietly mutated the
        caller's set — apply_partition reuses one accumulating `claimed` set
        across every category, so a side effect there is a landmine for anyone
        later reading that set expecting what they put in.
        """
        removed = 0
        conflicts: list[Conflict] = []
        other = DomainSet.from_rules(other.iter_rules())
        other.compress()
        for r in list(other.iter_rules()):
            if r.kind == "suffix":
                if self._ancestor_suffix(r.value, strict=True):
                    conflicts.append(Conflict(
                        f"cannot exclude +.{r.value}: a broader suffix already covers it"))
                    continue
                node = self._find(r.value)
                if node is not None:
                    removed += self._count_subtree(node, include_self_exact=True) + (
                        1 if node.suffix else 0)
                    node.exact = False
                    node.suffix = False
                    node.children = {}
            else:  # exact
                if self._ancestor_suffix(r.value, strict=False):
                    conflicts.append(Conflict(
                        f"cannot exclude {r.value}: a suffix rule already covers it"))
                    continue
                node = self._find(r.value)
                if node is not None and node.exact:
                    node.exact = False
                    removed += 1
        return removed, conflicts

    def merge(self, other: "DomainSet") -> None:
        for r in other.iter_rules():
            self.add(r)

    def to_payload(self) -> list[str]:
        out = []
        for r in self.iter_rules():
            out.append(f"+.{r.value}" if r.kind == "suffix" else r.value)
        return sorted(set(out))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
@dataclass
class Source:
    url: str
    note: str = ""


@dataclass
class Category:
    name: str
    description: str
    sources: list[Source]
    max_shrink: int
    max_invalid: int


@dataclass
class Config:
    timeout: int
    retries: int
    default_max_shrink: int
    default_max_invalid: int
    default_max_source_shrink: int
    allow_product_removal: bool
    allow_tld_removal: bool
    publish_branch: str
    priority: list[str]
    categories: dict[str, Category]

    def routing(self) -> list[str]:
        return list(self.priority)


def load_config(path: Path) -> Config:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    d = data.get("defaults", {})
    default_shrink = int(d.get("max-shrink-percent", 30))
    default_invalid = int(d.get("max-invalid", 0))
    default_source_shrink = int(d.get("max-source-shrink-percent", 30))
    allow_removal = bool(d.get("allow-product-removal", False))
    allow_tld_removal = bool(d.get("allow-tld-removal", False))
    priority = list(data.get("priority", []))
    cats: dict[str, Category] = {}
    for name, c in (data.get("categories") or {}).items():
        srcs = [Source(s["url"], s.get("note", "")) for s in (c.get("sources") or [])]
        cats[name] = Category(
            name=name,
            description=c.get("description", name),
            sources=srcs,
            max_shrink=int(c.get("max-shrink-percent", default_shrink)),
            max_invalid=int(c.get("max-invalid", default_invalid)),
        )
    for p in priority:
        if p not in cats:
            raise SystemExit(f"config error: priority category '{p}' is not defined")
    return Config(
        timeout=int(d.get("timeout-seconds", 30)),
        retries=int(d.get("retries", 3)),
        default_max_shrink=default_shrink,
        default_max_invalid=default_invalid,
        default_max_source_shrink=default_source_shrink,
        allow_product_removal=allow_removal,
        allow_tld_removal=allow_tld_removal,
        publish_branch=d.get("publish-branch", "release"),
        priority=priority,
        categories=cats,
    )


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
class FetchError(Exception):
    pass


def fetch_url(url: str, timeout: int, retries: int) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "clash-rules-builder"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise FetchError(f"HTTP {resp.status}")
                body = resp.read().decode("utf-8", errors="replace")
            if not body.strip():
                raise FetchError("empty body")
            return body
        # OSError covers URLError/HTTPError (both subclass it), TimeoutError,
        # and the connection-reset family. HTTPException covers read-phase
        # failures like IncompleteRead. Neither of the latter two used to be
        # caught, so a blip while reading the body escaped the retry loop and
        # killed the run instead of retrying it.
        except (OSError, FetchError, http.client.HTTPException) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise FetchError(f"failed to fetch {url}: {last}")


# ---------------------------------------------------------------------------
# Build pipeline
# ---------------------------------------------------------------------------
@dataclass
class CatResult:
    name: str
    domains: DomainSet
    ips: list[Rule]
    dedup_removed: int
    manual_covered: int
    conflicts: list[Conflict]
    source_notes: list[str]
    invalid_total: int = 0
    invalid_samples: list[str] = field(default_factory=list)
    exclude_noop: list[str] = field(default_factory=list)
    # url -> rules this source contributed on THIS run. Published alongside the
    # products and used next run as the per-source gate's baseline.
    source_counts: dict[str, int] = field(default_factory=dict)
    # Sources that rejected more lines than they accepted — a format change,
    # not junk. Judged per source; see check_invalid_gate.
    format_suspects: list[str] = field(default_factory=list)


def _read_manual(manual_dir: Path, name: str) -> list[Rule]:
    f = manual_dir / f"{name}.txt"
    if not f.exists():
        return []
    rules, _ = parse_text(f.read_text(encoding="utf-8"))
    return rules


def build_category(cat: Category, cfg: Config, manual_dir: Path,
                   fetcher: Callable[[str, int, int], str]) -> CatResult:
    domains = DomainSet()
    ips: dict[str, Rule] = {}
    notes: list[str] = []

    def ingest(rules: list[Rule]) -> None:
        for r in rules:
            if r.kind in ("exact", "suffix"):
                domains.add(r)
            else:
                ips[f"{r.kind},{r.value}"] = r

    invalid_total = 0
    source_counts: dict[str, int] = {}
    format_suspects: list[str] = []
    invalid_samples: list[str] = []
    for src in cat.sources:
        body = fetcher(src.url, cfg.timeout, cfg.retries)
        rules, st = parse_text(body)
        ingest(rules)
        source_counts[src.url] = st.parsed
        invalid_total += st.dropped_invalid
        if st.dropped_invalid > max(20, st.parsed):
            format_suspects.append(src.note or src.url)
        for s in st.invalid_samples:
            if len(invalid_samples) < 10:
                invalid_samples.append(f"{src.note or src.url}: {s}")
        unsup = ""
        if st.dropped_unsupported:
            unsup = (f", {st.dropped_unsupported} unsupported "
                     f"({','.join(sorted(st.unsupported_types))})")
        notes.append(f"{src.note or src.url}: {st.parsed} rules, "
                     f"{st.dropped_invalid} invalid, "
                     f"{st.dropped_keyword} keyword{unsup}")

    manual = _read_manual(manual_dir, cat.name)
    covered = sum(1 for r in manual if r.kind in ("exact", "suffix") and domains.covered(r))
    ingest(manual)

    dedup = domains.compress()

    # Which exclude entries actually matched anything? An exclude that removes
    # nothing is indistinguishable from one that worked — upstream renames a
    # domain and the false positive you "un-blocked" quietly comes back. Check
    # coverage before subtracting; report the misses.
    excl_rules = [r for r in _read_manual(manual_dir, f"{cat.name}-exclude")
                  if r.kind in ("exact", "suffix")]
    excl_noop = [r.value for r in excl_rules if not domains.covered(r)]
    excl = DomainSet.from_rules(excl_rules)
    _, conflicts = domains.subtract(excl)

    return CatResult(cat.name, domains, list(ips.values()), dedup, covered, conflicts,
                     notes, invalid_total, invalid_samples, excl_noop,
                     source_counts, format_suspects)


def apply_partition(cfg: Config, results: dict[str, CatResult],
                    manual_dir: Path) -> tuple[list[Conflict], dict[str, list[tuple[str, str]]]]:
    """Enforce the routing partition with manual pinning overriding priority.

    Returns (conflicts, taken). `taken` maps a category to the entries it lost
    and who took each one — the transfers that actually happened, as opposed to
    `conflicts`, which are the ones that could NOT happen.

    Reporting the transfers matters because they are the silent half. A broad
    upstream suffix in a high-priority category swallows whole families of
    specific hosts out of a lower one (measured 2026-08: microsoft's
    `+.edgesuite.net` / `+.cloudapp.net` — Akamai and Azure address space, not
    Microsoft services — took 137 hosts away from proxy, and proxy's
    `+.kaspersky.com` / `+.dell.com` took 403 China-region hosts away from
    direct). Every one of those is a routing decision nobody made on purpose,
    and until this report existed there was no number to watch.
    """
    routing = cfg.routing()
    pins: dict[str, DomainSet] = {}     # cat -> domains pinned to it
    for name in routing:
        pins[name] = DomainSet.from_rules(
            r for r in _read_manual(manual_dir, name) if r.kind in ("exact", "suffix"))

    conflicts: list[Conflict] = []
    taken: dict[str, list[tuple[str, str]]] = {}
    claimed = DomainSet()
    for idx, name in enumerate(routing):   # high priority first
        ds = results[name].domains
        before = set(ds.to_payload())
        pins_other = DomainSet()
        for other, pset in pins.items():
            if other != name:
                pins_other.merge(pset)
        _, c1 = ds.subtract(pins_other)   # drop domains pinned to other cats
        _, c2 = ds.subtract(claimed)      # drop domains claimed by higher cats
        conflicts.extend(c1 + c2)
        lost = before - set(ds.to_payload())
        if lost:
            # Attribute each loss. Pins are checked first because a pin
            # overrides priority; the already-finalized higher categories are
            # the only other thing that can have taken it.
            for entry in sorted(lost):
                rule = (Rule("suffix", entry[2:]) if entry.startswith("+.")
                        else Rule("exact", entry))
                by = next((o for o in routing
                           if o != name and pins[o].covered(rule)), None)
                if by is None:
                    by = next((o for o in routing[:idx]
                               if results[o].domains.covered(rule)), "?")
                taken.setdefault(name, []).append((entry, by))
        claimed.merge(ds)
    return conflicts, taken


class GateError(Exception):
    pass


def check_invalid_gate(cat: str, n_invalid: int, format_suspects: list[str],
                       samples: list[str], limit: int) -> None:
    """Fail the build when a category's upstream sources lose too many lines.

    An ABSOLUTE count, not a percentage — and the difference is the whole point.
    The `+.cn` regression was ONE dropped line out of 111,516 (0.0009%); any
    percentage threshold worth having would have waved it through, yet that line
    was the entire .cn top-level domain and every Chinese .cn site leaked to the
    proxy for as long as it was gone. A dropped rule's breadth has nothing to do
    with how many of them there are, so the only safe limit for a routing
    category is zero: every new drop has to be looked at by a human.

    `reject` is the one category allowed slack. Note the reason is NOT "a
    reject failure is harmless" — gate errors are collected globally and any
    one of them refuses the whole publish, so an over-limit reject freezes
    direct and proxy too. The reason is that reject's upstreams are ad lists
    with hand-edited junk lines, so its baseline is noisy in a way the routing
    sources are not; the allowance is headroom so that noise cannot take the
    publish down. Raising any limit means reading the samples in report.md
    first and deciding they are genuine junk.
    """
    if n_invalid > limit:
        detail = "; ".join(samples[:5]) or "(no samples captured)"
        # Distinguish "upstream added a junk line" from "upstream changed
        # format wholesale". Both arrive as the same counter, but the fix is
        # completely different — one is a line to look at, the other is a
        # source that has to be re-pointed or a rule type to add to
        # UNSUPPORTED_TYPES. Without this the five samples read as ordinary
        # garbage and the real cause takes a rebuild to spot.
        #
        # The ratio is judged PER SOURCE, not per category. reject pulls 163897
        # good lines from its first source, so the second one switching to hosts
        # syntax (17223 lines, all rejected) still leaves the category-wide ratio
        # looking healthy — the signal is diluted exactly in the multi-source
        # case where you cannot tell which source broke.
        hint = ""
        if format_suspects:
            hint = (f" — NOTE: {', '.join(format_suspects)} rejected more lines "
                    f"than it accepted; that source has most likely changed format "
                    f"(hosts / AdGuard syntax) or introduced a rule type missing "
                    f"from UNSUPPORTED_TYPES, rather than added junk")
        raise GateError(
            f"{cat}: {n_invalid} upstream line(s) dropped as invalid, limit {limit} — "
            f"{detail}{hint}")


def _bare_tlds(payload: list[str]) -> set[str]:
    """Suffix rules that are a single label — `+.cn`, `+.icbc`, `+.xn--fiqs8s`."""
    return {p[2:] for p in payload if p.startswith("+.") and "." not in p[2:]}


def check_tld_gate(filename: str, new_payload: list[str],
                   old_payload: list[str] | None) -> None:
    """Fail when a bare top-level-domain suffix rule disappears.

    The invalid gate only sees lines WE drop. This one sees lines the upstream
    drops, and it exists because the two failures are indistinguishable from
    the outside and equally catastrophic. `+.cn` is one line out of 110757: if
    it goes away upstream, the product shrinks 0.0009%, the shrink gate is
    silent, the invalid gate is silent, the product-disappeared gate is silent
    — and every Chinese .cn site falls through to the catch-all group and goes
    out over a metered VPS. That is exactly the shape of the 2026-08-11
    incident, only sourced one step further upstream.

    Counting is useless here (110609 of direct's 110757 entries are suffix
    rules); breadth is what matters, and a single label is as broad as a rule
    can get. Measured over the last 12 releases: direct carries 50 of these and
    proxy 71, with zero churn between builds, so this gate has no realistic
    false-positive rate. apple / icloud / microsoft / reject carry none, so it
    is a no-op for them.
    """
    if old_payload is None:
        return
    gone = sorted(_bare_tlds(old_payload) - _bare_tlds(new_payload))
    if gone:
        shown = ", ".join(f"+.{t}" for t in gone[:8])
        more = f" (+{len(gone) - 8} more)" if len(gone) > 8 else ""
        raise GateError(
            f"{filename}: {len(gone)} bare top-level-domain suffix rule(s) "
            f"disappeared: {shown}{more} — each one is an ENTIRE TLD, not one "
            f"rule; if the removal is intended, set defaults.allow-tld-removal: "
            f"true for one run")


def check_source_gate(cat: str, new_counts: dict[str, int],
                      old_counts: dict[str, int] | None,
                      max_shrink: int, floor: int = 20) -> None:
    """Fail when one upstream source of a category collapses.

    The shrink gate measures a whole category, so a multi-source category hides
    the death of any single source behind the others. Measured 2026-08 by
    blanking each source in turn: 7 of the 10 configured sources could return an
    empty payload with the product shrinking less than 8%, and apple's ONLY
    source could die with the product not changing by a single entry — its
    content is carried entirely by manual/apple.txt, so the category-level gate
    is structurally incapable of noticing.

    Two rules, because sources differ in size by four orders of magnitude:
      * any source that parsed >0 last time and 0 now is fatal, always. That is
        the dead-source case and it needs no threshold.
      * a percentage drop is only meaningful above `floor` rules. ACL4SSR Bing
        contributes 3; gating it on percentage would fail the build every time
        it moved by one line.

    A source absent from the baseline is new (or the baseline predates it) and
    is skipped; a source in the baseline but no longer in config was removed on
    purpose and is skipped too.
    """
    if not old_counts:
        return
    bad: list[str] = []
    for url, old_n in sorted(old_counts.items()):
        if url not in new_counts:
            continue
        new_n = new_counts[url]
        if old_n > 0 and new_n == 0:
            bad.append(f"{url}: parsed 0 rules (was {old_n}) — source is dead or "
                       f"now returns an empty payload")
            continue
        if old_n >= floor:
            shrink = (old_n - new_n) / old_n * 100
            if shrink > max_shrink:
                bad.append(f"{url}: {shrink:.0f}% fewer rules ({old_n} -> {new_n}), "
                           f"limit {max_shrink}%")
    if bad:
        raise GateError(f"{cat}: upstream source collapsed — " + "; ".join(bad))


def read_source_counts(d: Path) -> dict[str, dict[str, int]] | None:
    """Per-source baseline from a previous release, or None if it has none yet.

    A file that exists but cannot be read is an ERROR, not a missing baseline:
    falling back to None there would disable the per-source gate for exactly as
    long as the file stayed corrupt, and nothing else would report it.
    """
    f = d / "sources.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise GateError(f"{f}: unreadable per-source baseline ({e}); refusing to "
                        f"build with the per-source gate silently off")
    if not isinstance(data, dict):
        raise GateError(f"{f}: per-source baseline is not an object")
    return data


def check_gate(filename: str, new_n: int, old_n: int | None, max_shrink: int) -> None:
    if old_n is None:
        return  # first publish for this file
    if new_n == 0 and old_n > 0:
        # This failure is self-perpetuating and needs a documented way out.
        # Creating a product is ungated, destroying one is fatal: an upstream
        # that ships a single IP rule for one day creates final_<cat>_ipcidr.yaml,
        # and the day it goes away EVERY subsequent run fails identically — the
        # file is still on the release branch, so the next build sees it vanish
        # again, and the daily cron never publishes anything again. Name the
        # escape hatch in the message so recovery does not require reading this
        # file or hand-deleting from the release branch.
        raise GateError(
            f"{filename}: dropped to 0 entries (was {old_n}); if the removal is "
            f"intended, set defaults.allow-product-removal: true for one run")
    if old_n > 0:
        shrink = (old_n - new_n) / old_n * 100
        if shrink > max_shrink:
            raise GateError(
                f"{filename}: shrank {shrink:.0f}% ({old_n} -> {new_n}), "
                f"limit {max_shrink}%")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def _now() -> datetime:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    return datetime.now(timezone.utc)


def _header(cat_upper: str, template: str) -> list[str]:
    ts = _now().strftime("%Y-%m-%dT%H:%M:%SZ")
    return [
        "#########################################",
        "# 作者: ningcol",
        "# 项目地址: https://github.com/ningcol/clash-rules",
        f"# 更新时间: {ts}",
        template.format(up=cat_upper),
        "#########################################",
        "payload:",
    ]


def write_yaml(path: Path, payload: list[str], cat: str, ip: bool) -> None:
    up = cat.upper()
    lines = _header(up, IP_HEADER if ip else ROUTING_HEADER)
    lines += [f"  - '{p}'" for p in payload]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_payload(path: Path) -> list[str] | None:
    """Sorted payload entries of an existing product file, or None if absent.
    Header comments (including the volatile timestamp) are ignored, so this
    reflects rule content only — used to decide whether a rebuild actually
    changed anything."""
    if not path.exists():
        return None
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.lstrip()
        if s.startswith("- "):
            out.append(s[2:].strip().strip("'\""))
    return sorted(out)


def count_payload(path: Path) -> int | None:
    p = read_payload(path)
    return None if p is None else len(p)


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------
BEGIN = "<!-- BUILD:SUBSCRIPTIONS:BEGIN -->"
END = "<!-- BUILD:SUBSCRIPTIONS:END -->"


def render_readme_table(cfg: Config) -> str:
    br = cfg.publish_branch
    raw = f"https://raw.githubusercontent.com/ningcol/clash-rules/{br}"
    jsd = f"https://cdn.jsdelivr.net/gh/ningcol/clash-rules@{br}"
    rows = ["| 规则类型 | 说明 | raw 订阅 | jsDelivr 订阅（国内更稳） |",
            "|---------|------|----------|--------------------------|"]
    order = cfg.priority + [c for c in cfg.categories if c not in cfg.priority]
    for name in order:
        cat = cfg.categories[name]
        f = f"final_{name}.yaml"
        rows.append(f"| {name.upper()} | {cat.description} | "
                    f"[raw]({raw}/{f}) | [jsDelivr]({jsd}/{f}) |")
    return "\n".join(rows)


def update_readme(readme: Path, cfg: Config, check_only: bool) -> bool:
    text = readme.read_text(encoding="utf-8")
    if BEGIN not in text or END not in text:
        raise SystemExit(f"README missing {BEGIN} / {END} markers")
    pre, rest = text.split(BEGIN, 1)
    _, post = rest.split(END, 1)
    new = f"{pre}{BEGIN}\n{render_readme_table(cfg)}\n{END}{post}"
    if new == text:
        return False
    if not check_only:
        readme.write_text(new, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Lint
# ---------------------------------------------------------------------------
def lint(cfg: Config, root: Path) -> list[str]:
    manual_dir = root / "manual"
    errors: list[str] = []
    pin_owner: dict[str, str] = {}

    # Only these filenames are ever read. A typo (`dirct.txt`, `Direct.txt`,
    # `direct_exclude.txt`) lints clean and is never opened — the whole edit
    # silently does nothing, which is the failure shape this repo exists to
    # avoid. Enumerate the directory rather than only the names we expect.
    expected = {f"{n}.txt" for n in cfg.categories}
    expected |= {f"{n}-exclude.txt" for n in cfg.categories}
    if manual_dir.is_dir():
        for f in sorted(manual_dir.iterdir()):
            if f.is_file() and f.name not in expected:
                errors.append(
                    f"manual/{f.name}: not a recognised manual file; it is "
                    f"never read. Expected <category>.txt or "
                    f"<category>-exclude.txt")
    for name in cfg.categories:
        add_file = manual_dir / f"{name}.txt"
        excl_file = manual_dir / f"{name}-exclude.txt"
        add_vals: set[str] = set()
        for f, label in ((add_file, "add"), (excl_file, "exclude")):
            if not f.exists():
                continue
            raw = f.read_text(encoding="utf-8")
            if raw and not raw.endswith("\n"):
                errors.append(f"{f.relative_to(root)}: missing trailing newline")
            seen: set[str] = set()
            for i, line in enumerate(raw.splitlines(), 1):
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                status, rule = parse_line(line)
                if status == "invalid":
                    errors.append(f"{f.relative_to(root)}:{i}: invalid rule '{s}'")
                    continue
                if status == "unsupported":
                    # 'unsupported' is the right verdict for upstream files we
                    # do not control; in a hand-written manual file it means the
                    # line you added does nothing. Skipping it here would be a
                    # regression: before the unsupported bucket existed these
                    # landed in 'invalid' and lint caught them.
                    errors.append(
                        f"{f.relative_to(root)}:{i}: '{s}' is a rule type this "
                        f"builder does not emit; it would be silently ignored")
                    continue
                if status == "keyword":
                    errors.append(
                        f"{f.relative_to(root)}:{i}: '{s}' — DOMAIN-KEYWORD "
                        f"cannot be expressed in a behavior:domain payload")
                    continue
                if status != "ok" or rule is None:
                    continue
                # An exclude file can only remove domains: the subtraction runs
                # on the domain suffix tree, IP rules never reach it. Writing an
                # IP/CIDR/ASN there parses fine, lints clean, builds clean — and
                # the rule you meant to remove is still in the product. Refuse it
                # instead of dropping it on the floor.
                if label == "exclude" and rule.kind not in ("exact", "suffix"):
                    errors.append(
                        f"{f.relative_to(root)}:{i}: '{s}' is an IP rule; "
                        f"exclude files only remove domains")
                    continue
                key = f"{rule.kind},{rule.value}"
                if key in seen:
                    errors.append(f"{f.relative_to(root)}:{i}: duplicate '{s}'")
                seen.add(key)
                if label == "add":
                    add_vals.add(key)
        # add ∩ exclude = ∅
        if excl_file.exists():
            for line in excl_file.read_text(encoding="utf-8").splitlines():
                status, rule = parse_line(line)
                if status == "ok" and rule and f"{rule.kind},{rule.value}" in add_vals:
                    errors.append(f"manual/{name}: '{rule.value}' in both add and exclude")
        # cross-category double-pin (routing cats only)
        if name in cfg.priority and add_file.exists():
            for line in add_file.read_text(encoding="utf-8").splitlines():
                status, rule = parse_line(line)
                if status == "ok" and rule and rule.kind in ("exact", "suffix"):
                    prev = pin_owner.get(rule.value)
                    if prev and prev != name:
                        errors.append(
                            f"'{rule.value}' pinned to both {prev} and {name}")
                    pin_owner[rule.value] = name
    return errors


def lint_notes(cfg: Config, root: Path) -> list[str]:
    """Non-fatal observations. Printed by `lint`; never fail the build.

    Excluding from a routing category is NOT the routing-neutral deletion the
    docs used to describe. Exclusion runs before the partition, so dropping a
    domain from a high-priority category releases its claim and the next
    category that also carries it takes over — the traffic reroutes. Only the
    LAST routing category can exclude without that risk, because nothing is
    below it to inherit.

    This cannot be decided at lint time (it depends on upstream content nobody
    has fetched yet), so it is a note rather than an error: name the categories
    that could inherit and let the author confirm.
    """
    notes: list[str] = []
    routing = cfg.priority
    for i, name in enumerate(routing[:-1]):
        f = root / "manual" / f"{name}-exclude.txt"
        if not f.exists():
            continue
        vals = []
        for line in f.read_text(encoding="utf-8").splitlines():
            status, rule = parse_line(line)
            if status == "ok" and rule and rule.kind in ("exact", "suffix"):
                vals.append(rule.value)
        if not vals:
            continue
        heirs = ", ".join(routing[i + 1:])
        notes.append(
            f"manual/{name}-exclude.txt: {len(vals)} entr(ies). Excluding from "
            f"'{name}' releases the claim — if {heirs} also carries the domain, "
            f"that category takes it over and the traffic REROUTES. Confirm that "
            f"is intended: {', '.join(vals[:5])}"
            + (" …" if len(vals) > 5 else ""))
    return notes


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_build(cfg: Config, root: Path, out: Path, previous: Path | None,
              fetcher: Callable[[str, int, int], str]) -> int:
    manual_dir = root / "manual"
    order = cfg.priority + [c for c in cfg.categories if c not in cfg.priority]
    results: dict[str, CatResult] = {}
    for name in order:
        print(f"[build] {name}", file=sys.stderr)
        results[name] = build_category(cfg.categories[name], cfg, manual_dir, fetcher)

    conflicts, taken = apply_partition(cfg, results, manual_dir)
    for name in order:
        conflicts += results[name].conflicts

    # Plan every product + gate (before writing anything).
    out.mkdir(parents=True, exist_ok=True)
    planned: list[tuple[Path, list[str], str, bool]] = []
    gate_errors: list[str] = []
    prev_sources = read_source_counts(previous) if previous else None
    if previous is not None and prev_sources is None:
        print("[gate] no sources.json in the baseline; per-source gate is off for "
              "this run and armed from the next publish onwards", file=sys.stderr)
    for name in order:
        res = results[name]
        try:
            check_invalid_gate(name, res.invalid_total, res.format_suspects,
                               res.invalid_samples, cfg.categories[name].max_invalid)
        except GateError as e:
            gate_errors.append(str(e))
        try:
            check_source_gate(name, res.source_counts,
                              (prev_sources or {}).get(name),
                              cfg.default_max_source_shrink)
        except GateError as e:
            gate_errors.append(str(e))
        dpay = res.domains.to_payload()
        dpath = out / f"final_{name}.yaml"
        old_pay = read_payload(previous / dpath.name) if previous else None
        try:
            check_gate(dpath.name, len(dpay),
                       None if old_pay is None else len(old_pay),
                       cfg.categories[name].max_shrink)
        except GateError as e:
            gate_errors.append(str(e))
        if not cfg.allow_tld_removal:
            try:
                check_tld_gate(dpath.name, dpay, old_pay)
            except GateError as e:
                gate_errors.append(str(e))
        planned.append((dpath, dpay, name, False))
        if res.ips:
            # `behavior: ipcidr` payloads carry CIDRs only; an `AS####` entry
            # makes mihomo reject the whole provider. Keep them out, and say so
            # in the report rather than dropping them on the floor.
            ippay = sorted({r.value for r in res.ips if r.kind != "ip-asn"})
            if not ippay:
                planned_skip_asn = sum(1 for r in res.ips if r.kind == "ip-asn")
                if planned_skip_asn:
                    print(f"  [skip] {name}: {planned_skip_asn} ASN rule(s) have "
                          f"no ipcidr-compatible product", file=sys.stderr)
                continue
            ippath = out / f"final_{name}_ipcidr.yaml"
            old_ip = count_payload(previous / ippath.name) if previous else None
            try:
                check_gate(ippath.name, len(ippay), old_ip, cfg.categories[name].max_shrink)
            except GateError as e:
                gate_errors.append(str(e))
            planned.append((ippath, ippay, name, True))

    planned_names = {p.name for p, _, _, _ in planned}
    # Gate products that existed in the last release but are gone now — a removed
    # category, or a category whose IP rules vanished upstream. Without this a
    # disappearing product would slip past the shrink gate and be deleted ungated.
    if previous and not cfg.allow_product_removal:
        for prev_file in sorted(previous.glob("final_*.yaml")):
            if prev_file.name not in planned_names:
                try:
                    check_gate(prev_file.name, 0, count_payload(prev_file),
                               cfg.default_max_shrink)
                except GateError as e:
                    gate_errors.append(f"{e} [product disappeared]")

    # Write the report BEFORE deciding to bail. A gate failure is exactly when
    # someone needs the per-source counts, the dropped samples and the conflict
    # list — and it used to be the one path that produced no report at all,
    # leaving an empty output directory and only the five samples in the error
    # line. Products are still not written, so nothing can be published.
    _write_report(order, results, conflicts, out, gate_errors, taken)

    if gate_errors:
        for e in gate_errors:
            print(f"[gate] FAIL {e}", file=sys.stderr)
        print("[gate] refusing to publish; last release stays live", file=sys.stderr)
        print(f"[gate] see {out / 'report.md'} for per-source counts",
              file=sys.stderr)
        return 1

    # Did the built rules actually differ from the current release? Compare
    # payloads only (the header carries a live timestamp that changes every run),
    # so the publisher can skip no-op commits and CDN purges.
    changed = True
    if previous is not None:
        prev_names = {f.name for f in previous.glob("final_*.yaml")}
        changed = planned_names != prev_names or any(
            payload != (read_payload(previous / path.name) or [])
            for path, payload, _, _ in planned)
        if prev_sources is None:
            # Bootstrap: the per-source gate needs its baseline ON the release
            # branch, and publishing only happens when `changed` is true. Left
            # alone, a day where the rules happen not to move would skip the
            # publish, sources.json would never land, and the gate would stay
            # off forever — armed in the code, absent in practice. Force one
            # publish to lay the baseline down.
            changed = True
            print("[build] forcing a publish to lay down the per-source baseline",
                  file=sys.stderr)

    for path, payload, name, ip in planned:
        write_yaml(path, payload, name, ip)
        print(f"  wrote {path.name} ({len(payload)})", file=sys.stderr)

    # Ships with the products so the next run has a per-source baseline. It is
    # written only on the success path, for the same reason the products are:
    # a baseline that recorded a build nobody published would gate the next run
    # against numbers that never went live.
    (out / "sources.json").write_text(
        json.dumps({n: results[n].source_counts for n in order},
                   indent=2, sort_keys=True) + "\n", encoding="utf-8")

    (out / "changed.txt").write_text("true\n" if changed else "false\n", encoding="utf-8")
    print(f"[build] changed={'true' if changed else 'false'}", file=sys.stderr)
    return 0


def _report_transfers(lines: list[str], results: dict[str, CatResult],
                      taken: dict[str, list[tuple[str, str]]]) -> None:
    """Per category: what the partition took away, and which broad suffix took it.

    Grouped by the covering suffix rather than listed domain by domain — one
    `+.kaspersky.com` accounts for 81 lost hosts, and that suffix is the thing
    you would act on. Listing 403 hostnames would just be noise.
    """
    if not taken:
        return
    lines += ["", "## partition transfers (domains this category lost, and to whom)",
              "",
              "Not conflicts — these succeeded. A broad suffix in the winning "
              "category swallowed specific hosts out of this one, which is a "
              "routing decision nobody made explicitly. Watch the totals; "
              "investigate suffixes that are not actually owned by the winner.",
              ""]
    for name, entries in taken.items():
        by_cat: dict[str, int] = {}
        for _, who in entries:
            by_cat[who] = by_cat.get(who, 0) + 1
        share = ", ".join(f"{w} {n}" for w, n in
                          sorted(by_cat.items(), key=lambda kv: -kv[1]))
        noun = "entry" if len(entries) == 1 else "entries"
        lines.append(f"- **{name}**: lost {len(entries)} {noun} ({share})")
        groups: dict[tuple[str, str], int] = {}
        for entry, who in entries:
            dom = entry[2:] if entry.startswith("+.") else entry
            winner = results.get(who)
            suf = winner.domains.covering_suffix(dom) if winner else None
            key = (f"+.{suf}" if suf else entry, who)
            groups[key] = groups.get(key, 0) + 1
        # Only the suffixes that swallowed SEVERAL hosts are worth naming — those
        # are the ones a single exclude line would undo. One-for-one transfers
        # are the partition doing exactly its job; listing them buries the rest.
        bulk = sorted(((k, n) for k, n in groups.items() if n > 1),
                      key=lambda kv: -kv[1])[:8]
        for (suf, who), n in bulk:
            lines.append(f"  - `{suf}` ({who}) took {n}")
        ones = sum(n for n in groups.values() if n == 1)
        if ones:
            lines.append(f"  - plus {ones} one-for-one transfer(s)")


def _write_report(order: list[str], results: dict[str, CatResult],
                  conflicts: list[Conflict], out: Path,
                  gate_errors: list[str] | None = None,
                  taken: dict[str, list[tuple[str, str]]] | None = None) -> None:
    lines = ["# build report", ""]
    if gate_errors:
        lines += ["## GATE FAILED — nothing published", ""]
        lines += [f"- {e}" for e in gate_errors]
        lines.append("")
    msg_lines = []
    for name in order:
        r = results[name]
        n = len(r.domains.to_payload())
        lines.append(f"## {name}: {n} domains")
        lines.append(f"- dedup removed: {r.dedup_removed}, manual already covered: "
                     f"{r.manual_covered}")
        if r.exclude_noop:
            lines.append(f"- **exclude entries that matched nothing** "
                         f"(upstream renamed or removed them?): "
                         f"{', '.join(sorted(r.exclude_noop))}")
        for note in r.source_notes:
            lines.append(f"- {note}")
        msg_lines.append(f"{name}: {n} domains")
    _report_transfers(lines, results, taken or {})
    if conflicts:
        lines += ["", "## conflicts (exclusions that could NOT be applied)"]
        lines += [f"- {c.detail}" for c in conflicts]
    report = "\n".join(lines) + "\n"
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "commit-msg.txt").write_text(
        "chore(release): update rule sets\n\n" + "\n".join(msg_lines) + "\n",
        encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # APPEND. The step summary is a shared, workflow-wide buffer: writing it
        # truncates whatever an earlier step put there. Nothing else writes to it
        # today, which is precisely why an overwrite would go unnoticed until the
        # day something does.
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(report)
    if conflicts:
        print(f"[warn] {len(conflicts)} conflict(s); see report.md", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Clash rule-set builder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--root", default=".")

    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    b.add_argument("--previous")

    sub.add_parser("lint")

    r = sub.add_parser("readme")
    r.add_argument("--check", action="store_true")

    args = ap.parse_args(argv)
    root = Path(args.root)
    cfg = load_config(root / args.config)

    if args.cmd == "build":
        prev = Path(args.previous) if args.previous else None
        if prev is not None and not prev.exists():
            # Silently treating a bad --previous as "first ever publish" turns
            # every gate off at once: a typo in the workflow, or a failed
            # checkout of the release branch, and a gutted product ships with
            # rc=0 and no warning anywhere. Asking for a baseline that is not
            # there is an error, not a mode.
            print(f"--previous {prev} does not exist; refusing to build with "
                  f"all gates disabled. Omit --previous for a first publish.",
                  file=sys.stderr)
            return 1
        try:
            return cmd_build(cfg, root, Path(args.out), prev, fetch_url)
        except (FetchError, GateError) as e:
            # An unreachable source after all retries used to escape as a bare
            # traceback. The outcome was right (nothing published) but the
            # message was not: it named a urllib exception, not the category or
            # the URL, and it looked like a crash rather than a refusal.
            print(f"[build] FAIL {e}", file=sys.stderr)
            print("[build] refusing to publish; last release stays live",
                  file=sys.stderr)
            return 1

    if args.cmd == "lint":
        errors = lint(cfg, root)
        for n in lint_notes(cfg, root):
            print(f"[lint] note: {n}", file=sys.stderr)
        for e in errors:
            print(f"[lint] {e}", file=sys.stderr)
        if errors:
            print(f"[lint] {len(errors)} error(s)", file=sys.stderr)
            return 1
        print("[lint] ok", file=sys.stderr)
        return 0

    if args.cmd == "readme":
        changed = update_readme(root / "README.md", cfg, args.check)
        if args.check and changed:
            print("[readme] out of date; run: python scripts/build.py readme",
                  file=sys.stderr)
            return 1
        print("[readme] updated" if changed else "[readme] up to date", file=sys.stderr)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
