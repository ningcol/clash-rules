#!/usr/bin/env python3
"""Clash rule-set builder.

Pipeline: fetch upstream sources -> parse/normalize/validate -> per-category
suffix-tree dedup -> manual pinning + partition across routing categories ->
shrink gate -> emit final_<cat>.yaml. Config lives entirely in config.yaml.

Subcommands:
  build   --out DIR [--previous DIR] [--dry-run]   build and write products
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
ASN_RE = re.compile(r"^as[0-9]+$")

ROUTING_HEADER = "# 说明: 本文件为自动生成的 Clash {up} 规则（behavior: domain）。"
IP_HEADER = "# 说明: 本文件为自动生成的 Clash {up} IP规则（behavior: ipcidr）。"


@dataclass(frozen=True)
class Rule:
    kind: str   # exact | suffix | ip-cidr | ip-cidr6 | ip-asn
    value: str


# ---------------------------------------------------------------------------
# Parsing / normalization
# ---------------------------------------------------------------------------
def _normalize_domain(v: str) -> str | None:
    """Lowercase, strip trailing dot, IDNA-encode. Return None if invalid."""
    v = v.strip().rstrip(".").lower()
    if not v or "*" in v or ":" in v or "@" in v or "/" in v:
        return None
    try:
        v = v.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        # Already-ASCII names with underscores can trip idna; accept if they
        # match our permissive domain shape.
        pass
    if not DOMAIN_RE.match(v):
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
        d = _normalize_domain(low[2:])
        return Rule("suffix", d) if d else None
    if low.startswith("*."):
        d = _normalize_domain(low[2:])
        return Rule("suffix", d) if d else None
    if low.startswith(".") and not low[1:2].isdigit():
        d = _normalize_domain(low[1:])
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
    invalid_samples: list[str] = field(default_factory=list)


def parse_line(line: str) -> tuple[str, Rule | None]:
    """Return ('ok'|'skip'|'keyword'|'invalid', rule). rule set only for 'ok'."""
    s = line.rstrip("\r\n").strip()
    if not s or s.startswith("#") or s.startswith("!"):
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
        mapping = {
            "DOMAIN": "exact", "DOMAIN-SUFFIX": "suffix",
            "IP-CIDR": None, "IP-CIDR6": None, "IP-ASN": None,
        }
        if t in mapping:
            val = parts[1] if len(parts) > 1 else ""
            if t == "DOMAIN":
                d = _normalize_domain(val)
                return ("ok", Rule("exact", d)) if d else ("invalid", None)
            if t == "DOMAIN-SUFFIX":
                d = _normalize_domain(val)
                return ("ok", Rule("suffix", d)) if d else ("invalid", None)
            r = classify_value(val)  # IP-CIDR/6/ASN
            return ("ok", r) if r else ("invalid", None)
        # Unknown TYPE,value — fall through to classify the whole token.

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

    def covered(self, rule: Rule) -> bool:
        """Is this rule already implied by the set (ancestor/own suffix, or exact)?"""
        if self._ancestor_suffix(rule.value, strict=False):
            return True
        node = self._find(rule.value)
        if node is None:
            return False
        return node.exact if rule.kind == "exact" else node.suffix

    def subtract(self, other: "DomainSet") -> tuple[int, list[Conflict]]:
        """Remove other's rules from self. Return (removed, conflicts)."""
        removed = 0
        conflicts: list[Conflict] = []
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


@dataclass
class Config:
    timeout: int
    retries: int
    default_max_shrink: int
    publish_branch: str
    priority: list[str]
    categories: dict[str, Category]

    def routing(self) -> list[str]:
        return list(self.priority)


def load_config(path: Path) -> Config:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    d = data.get("defaults", {})
    default_shrink = int(d.get("max-shrink-percent", 30))
    priority = list(data.get("priority", []))
    cats: dict[str, Category] = {}
    for name, c in (data.get("categories") or {}).items():
        srcs = [Source(s["url"], s.get("note", "")) for s in (c.get("sources") or [])]
        cats[name] = Category(
            name=name,
            description=c.get("description", name),
            sources=srcs,
            max_shrink=int(c.get("max-shrink-percent", default_shrink)),
        )
    for p in priority:
        if p not in cats:
            raise SystemExit(f"config error: priority category '{p}' is not defined")
    return Config(
        timeout=int(d.get("timeout-seconds", 30)),
        retries=int(d.get("retries", 3)),
        default_max_shrink=default_shrink,
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
        except (urllib.error.URLError, FetchError, TimeoutError) as e:
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

    for src in cat.sources:
        body = fetcher(src.url, cfg.timeout, cfg.retries)
        rules, st = parse_text(body)
        ingest(rules)
        notes.append(f"{src.note or src.url}: {st.parsed} rules, "
                     f"{st.dropped_invalid} invalid, {st.dropped_keyword} keyword")

    manual = _read_manual(manual_dir, cat.name)
    covered = sum(1 for r in manual if r.kind in ("exact", "suffix") and domains.covered(r))
    ingest(manual)

    dedup = domains.compress()

    excl = DomainSet.from_rules(_read_manual(manual_dir, f"{cat.name}-exclude"))
    _, conflicts = domains.subtract(excl)

    return CatResult(cat.name, domains, list(ips.values()), dedup, covered, conflicts, notes)


def apply_partition(cfg: Config, results: dict[str, CatResult],
                    manual_dir: Path) -> list[Conflict]:
    """Enforce the routing partition with manual pinning overriding priority."""
    routing = cfg.routing()
    pins: dict[str, DomainSet] = {}     # cat -> domains pinned to it
    for name in routing:
        pins[name] = DomainSet.from_rules(
            r for r in _read_manual(manual_dir, name) if r.kind in ("exact", "suffix"))

    conflicts: list[Conflict] = []
    claimed = DomainSet()
    for name in routing:               # high priority first
        ds = results[name].domains
        pins_other = DomainSet()
        for other, pset in pins.items():
            if other != name:
                pins_other.merge(pset)
        _, c1 = ds.subtract(pins_other)   # drop domains pinned to other cats
        _, c2 = ds.subtract(claimed)      # drop domains claimed by higher cats
        conflicts.extend(c1 + c2)
        claimed.merge(ds)
    return conflicts


class GateError(Exception):
    pass


def check_gate(filename: str, new_n: int, old_n: int | None, max_shrink: int) -> None:
    if old_n is None:
        return  # first publish for this file
    if new_n == 0 and old_n > 0:
        raise GateError(f"{filename}: dropped to 0 entries (was {old_n})")
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


def count_payload(path: Path) -> int | None:
    if not path.exists():
        return None
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("- "):
            n += 1
    return n


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------
BEGIN = "<!-- BUILD:SUBSCRIPTIONS:BEGIN -->"
END = "<!-- BUILD:SUBSCRIPTIONS:END -->"


def render_readme_table(cfg: Config) -> str:
    base = (f"https://raw.githubusercontent.com/ningcol/clash-rules/"
            f"{cfg.publish_branch}")
    rows = ["| 规则类型 | 说明 | 订阅链接 |", "|---------|------|----------|"]
    order = cfg.priority + [c for c in cfg.categories if c not in cfg.priority]
    for name in order:
        cat = cfg.categories[name]
        url = f"{base}/final_{name}.yaml"
        rows.append(f"| {name.upper()} | {cat.description} | [{name}]({url}) |")
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
                if status != "ok" or rule is None:
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


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_build(cfg: Config, root: Path, out: Path, previous: Path | None,
              dry_run: bool, fetcher: Callable[[str, int, int], str]) -> int:
    manual_dir = root / "manual"
    order = cfg.priority + [c for c in cfg.categories if c not in cfg.priority]
    results: dict[str, CatResult] = {}
    for name in order:
        print(f"[build] {name}", file=sys.stderr)
        results[name] = build_category(cfg.categories[name], cfg, manual_dir, fetcher)

    conflicts = apply_partition(cfg, results, manual_dir)
    for name in order:
        conflicts += results[name].conflicts

    # Gate check (before writing anything).
    out.mkdir(parents=True, exist_ok=True)
    planned: list[tuple[Path, list[str], str, bool]] = []
    gate_errors: list[str] = []
    for name in order:
        res = results[name]
        dpay = res.domains.to_payload()
        dpath = out / f"final_{name}.yaml"
        old = count_payload(previous / f"final_{name}.yaml") if previous else None
        try:
            check_gate(dpath.name, len(dpay), old, cfg.categories[name].max_shrink)
        except GateError as e:
            gate_errors.append(str(e))
        planned.append((dpath, dpay, name, False))
        if res.ips:
            ippay = sorted({r.value for r in res.ips})
            ippath = out / f"final_{name}_ipcidr.yaml"
            old_ip = count_payload(previous / ippath.name) if previous else None
            try:
                check_gate(ippath.name, len(ippay), old_ip, cfg.categories[name].max_shrink)
            except GateError as e:
                gate_errors.append(str(e))
            planned.append((ippath, ippay, name, True))

    if gate_errors:
        for e in gate_errors:
            print(f"[gate] FAIL {e}", file=sys.stderr)
        print("[gate] refusing to publish; last release stays live", file=sys.stderr)
        return 1

    for path, payload, name, ip in planned:
        write_yaml(path, payload, name, ip)
        print(f"  wrote {path.name} ({len(payload)})", file=sys.stderr)

    _write_report(order, results, conflicts, out, dry_run)
    return 0


def _write_report(order: list[str], results: dict[str, CatResult],
                  conflicts: list[Conflict], out: Path, dry_run: bool) -> None:
    lines = ["# build report", ""]
    msg_lines = []
    for name in order:
        r = results[name]
        n = len(r.domains.to_payload())
        lines.append(f"## {name}: {n} domains")
        lines.append(f"- dedup removed: {r.dedup_removed}, manual already covered: "
                     f"{r.manual_covered}")
        for note in r.source_notes:
            lines.append(f"- {note}")
        msg_lines.append(f"{name}: {n} domains")
    if conflicts:
        lines += ["", "## conflicts"]
        lines += [f"- {c.detail}" for c in conflicts]
    report = "\n".join(lines) + "\n"
    (out / "report.md").write_text(report, encoding="utf-8")
    (out / "commit-msg.txt").write_text(
        "chore(release): update rule sets\n\n" + "\n".join(msg_lines) + "\n",
        encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        Path(summary).write_text(report, encoding="utf-8")
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
    b.add_argument("--dry-run", action="store_true")

    sub.add_parser("lint")

    r = sub.add_parser("readme")
    r.add_argument("--check", action="store_true")

    args = ap.parse_args(argv)
    root = Path(args.root)
    cfg = load_config(root / args.config)

    if args.cmd == "build":
        prev = Path(args.previous) if args.previous else None
        if prev and not prev.exists():
            prev = None
        return cmd_build(cfg, root, Path(args.out), prev, args.dry_run, fetch_url)

    if args.cmd == "lint":
        errors = lint(cfg, root)
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
