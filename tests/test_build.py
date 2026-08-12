"""Unit tests for scripts/build.py. Run: python -m unittest discover -s tests"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build  # noqa: E402
from build import (  # noqa: E402
    Rule, DomainSet, classify_value, parse_line, parse_text,
    check_gate, check_invalid_gate, check_tld_gate, check_source_gate,
    GateError,
)

REPO = Path(__file__).resolve().parent.parent


class TestClassify(unittest.TestCase):
    def test_forms(self):
        cases = {
            "example.com": Rule("exact", "example.com"),
            "+.example.com": Rule("suffix", "example.com"),
            "*.example.com": Rule("suffix", "example.com"),
            ".example.com": Rule("suffix", "example.com"),
            "Example.COM": Rule("exact", "example.com"),
            "example.com.": Rule("exact", "example.com"),
            "AS13335": Rule("ip-asn", "AS13335"),
            "1.1.1.0/24": Rule("ip-cidr", "1.1.1.0/24"),
            "1.1.1.5/24": Rule("ip-cidr", "1.1.1.0/24"),   # host bits normalized
            "2001:db8::/32": Rule("ip-cidr6", "2001:db8::/32"),
        }
        for inp, want in cases.items():
            self.assertEqual(classify_value(inp), want, inp)

    def test_invalid(self):
        for bad in ["*cdn.onenote.net", "1.2.3.4", "not a domain",
                    "foo@bar.com", "sub.*.example.com", "single", ""]:
            self.assertIsNone(classify_value(bad), bad)

    def test_bare_tld_suffix(self):
        """A suffix rule may be a bare TLD; an exact rule may not.

        Upstream direct lists carry `+.cn` as ONE line covering the whole ccTLD
        and therefore list almost no individual .cn domains. Rejecting it does
        not lose one rule — it drops the entire .cn top-level domain out of
        direct, and every Chinese .cn site then falls through to the catch-all
        group and goes out over the proxy. The failure is silent: the line is
        only counted as `dropped_invalid` and the build still succeeds.
        """
        self.assertEqual(classify_value("+.cn"), Rule("suffix", "cn"))
        self.assertEqual(classify_value("*.cn"), Rule("suffix", "cn"))
        self.assertEqual(classify_value(".icbc"), Rule("suffix", "icbc"))
        self.assertEqual(classify_value("+.xn--fiqs8s"), Rule("suffix", "xn--fiqs8s"))
        self.assertEqual(parse_line("DOMAIN-SUFFIX,cn"), ("ok", Rule("suffix", "cn")))
        # Exact form is untouched: DOMAIN,cn matches nothing real.
        self.assertIsNone(classify_value("cn"))
        self.assertEqual(parse_line("DOMAIN,cn")[0], "invalid")
        # An all-numeric single label is a bare IP fragment, not a TLD.
        self.assertIsNone(classify_value("+.123"))

    def test_leading_dot_digit_first_label(self):
        """`.95572.com` is a suffix rule, not garbage.

        The old guard skipped the leading-dot branch whenever the next character
        was a digit — meant to stop `.1.2.3` becoming a suffix, but it rejected
        every domain whose first label starts with a digit. Bare IPs are already
        caught by the all-numeric-last-label check below.
        """
        self.assertEqual(classify_value(".95572.com"), Rule("suffix", "95572.com"))
        self.assertEqual(classify_value(".360doc11.net"), Rule("suffix", "360doc11.net"))
        self.assertIsNone(classify_value(".1.2.3"))       # still a bare IP
        self.assertIsNone(classify_value("1.2.3.4"))


class TestParseLine(unittest.TestCase):
    def test_clash_and_yaml(self):
        self.assertEqual(parse_line("DOMAIN,example.com"), ("ok", Rule("exact", "example.com")))
        self.assertEqual(parse_line("DOMAIN-SUFFIX,example.com"),
                         ("ok", Rule("suffix", "example.com")))
        self.assertEqual(parse_line("  - '+.example.com'"),
                         ("ok", Rule("suffix", "example.com")))
        self.assertEqual(parse_line("DOMAIN-KEYWORD,ads")[0], "keyword")
        self.assertEqual(parse_line("payload:")[0], "skip")
        self.assertEqual(parse_line("# comment")[0], "skip")
        self.assertEqual(parse_line("*cdn.onenote.net")[0], "invalid")

    def test_stats(self):
        text = "\n".join([
            "payload:", "  - 'a.com'", "  - '+.b.com'", "# c", "",
            "DOMAIN-KEYWORD,ads", "*bad.com", "1.1.1.0/24",
        ])
        rules, st = parse_text(text)
        self.assertEqual(st.parsed, 3)          # a.com, +.b.com, cidr
        self.assertEqual(st.dropped_keyword, 1)
        self.assertEqual(st.dropped_invalid, 1)


class TestDomainSet(unittest.TestCase):
    def _ds(self, *specs):
        ds = DomainSet()
        for s in specs:
            ds.add(classify_value(s))
        return ds

    def test_compress_covers_children(self):
        ds = self._ds("a.com", "+.a.com", "x.y.a.com")
        removed = ds.compress()
        self.assertEqual(ds.to_payload(), ["+.a.com"])
        self.assertEqual(removed, 2)

    def test_compress_keeps_uncovered(self):
        ds = self._ds("+.a.com", "b.com")
        ds.compress()
        self.assertEqual(ds.to_payload(), ["+.a.com", "b.com"])

    def test_covered(self):
        ds = self._ds("+.a.com")
        self.assertTrue(ds.covered(Rule("exact", "x.a.com")))
        self.assertTrue(ds.covered(Rule("suffix", "x.a.com")))
        self.assertFalse(ds.covered(Rule("exact", "a.org")))

    def test_subtract_exact_removes_exact(self):
        ds = self._ds("a.com", "b.com")
        other = self._ds("a.com")
        removed, conf = ds.subtract(other)
        self.assertEqual(ds.to_payload(), ["b.com"])
        self.assertEqual((removed, conf), (1, []))

    def test_subtract_suffix_removes_subtree(self):
        ds = self._ds("+.a.com", "x.a.com", "b.com")
        removed, conf = ds.subtract(self._ds("+.a.com"))
        self.assertEqual(ds.to_payload(), ["b.com"])
        self.assertEqual(conf, [])

    def test_subtract_narrow_from_broad_is_conflict(self):
        # self holds the broad +.a.com; excluding narrow x.a.com can't be done.
        ds = self._ds("+.a.com")
        removed, conf = ds.subtract(self._ds("x.a.com"))
        self.assertEqual(ds.to_payload(), ["+.a.com"])  # unchanged
        self.assertEqual(removed, 0)
        self.assertEqual(len(conf), 1)


class TestGate(unittest.TestCase):
    def test_first_publish_skips(self):
        check_gate("f", 10, None, 30)  # no raise

    def test_zero_fails(self):
        with self.assertRaises(GateError):
            check_gate("f", 0, 100, 30)

    def test_shrink_over_limit_fails(self):
        with self.assertRaises(GateError):
            check_gate("f", 60, 100, 30)   # 40% shrink

    def test_shrink_within_limit_ok(self):
        check_gate("f", 80, 100, 30)       # 20% shrink

    def test_one_invalid_line_fails_a_routing_category(self):
        """A single dropped line must fail the build, not just get counted.

        The `+.cn` regression was 1 line in 111,516 — a percentage gate would
        have waved it through while the whole .cn TLD leaked to the proxy. The
        limit for routing categories is therefore 0, and the samples ride along
        in the message so the failure is diagnosable without a rebuild.
        """
        with self.assertRaises(GateError) as cm:
            check_invalid_gate("direct", 1, [], ["upstream: - '+.cn'"], 0)
        self.assertIn("+.cn", str(cm.exception))
        check_invalid_gate("direct", 0, [], [], 0)      # nothing dropped -> ok
        check_invalid_gate("reject", 2, [], ["x"], 20)  # reject's slack -> ok

    def test_wholesale_format_change_names_the_source(self):
        """"Upstream added a junk line" and "upstream switched to hosts format"
        arrive as the same counter but need completely different fixes.

        The ratio must be judged PER SOURCE. reject pulls 163897 good lines from
        its first source, so its second source flipping to hosts syntax (17223
        lines, every one rejected) still leaves the category-wide ratio looking
        healthy — diluted in exactly the multi-source case where you cannot
        otherwise tell which source broke.
        """
        with self.assertRaises(GateError) as cm:
            check_invalid_gate("reject", 17223, ["qq5460168/AD886"],
                               ["0.0.0.0 ads.example.com"], 20)
        self.assertIn("changed format", str(cm.exception))
        self.assertIn("qq5460168/AD886", str(cm.exception))
        # A handful of junk lines among many good ones must NOT claim that.
        with self.assertRaises(GateError) as cm:
            check_invalid_gate("reject", 30, [], ["junk"], 20)
        self.assertNotIn("changed format", str(cm.exception))


class TestPartition(unittest.TestCase):
    """End-to-end partition with a fake fetcher (no network)."""

    def _run(self, tmp: Path):
        cfg_text = (
            "defaults: {max-shrink-percent: 100}\n"
            "priority: [hi, lo]\n"
            "categories:\n"
            "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n"
            "  lo: {description: lo, sources: [{url: 'lo://x'}]}\n"
            "  rej: {description: rej, sources: [{url: 'rej://x'}]}\n"
        )
        (tmp / "config.yaml").write_text(cfg_text)
        manual = tmp / "manual"
        manual.mkdir()
        # pin shared.com to lo even though hi's upstream also has it
        (manual / "lo.txt").write_text("shared.com\n")
        out = tmp / "out"

        upstream = {
            "hi://x": "payload:\n  - 'shared.com'\n  - 'onlyhi.com'\n",
            "lo://x": "payload:\n  - 'onlylo.com'\n",
            "rej://x": "payload:\n  - 'ad.com'\n",
        }

        def fake(url, timeout, retries):
            return upstream[url]

        cfg = build.load_config(tmp / "config.yaml")
        rc = build.cmd_build(cfg, tmp, out, None, fake)
        self.assertEqual(rc, 0)
        hi = set(build.Path(out / "final_hi.yaml").read_text().splitlines())
        lo = set(build.Path(out / "final_lo.yaml").read_text().splitlines())
        return hi, lo

    def test_manual_pin_overrides_priority(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            hi, lo = self._run(Path(d))
            # shared.com is pinned to lo -> must be in lo, absent from hi
            self.assertIn("  - 'shared.com'", lo)
            self.assertNotIn("  - 'shared.com'", hi)
            self.assertIn("  - 'onlyhi.com'", hi)
            # partition: hi and lo domain sets disjoint
            hi_d = {l for l in hi if l.strip().startswith("- ")}
            lo_d = {l for l in lo if l.strip().startswith("- ")}
            self.assertEqual(hi_d & lo_d, set())


class TestLint(unittest.TestCase):
    CFG = (
        "defaults: {}\n"
        "priority: [hi, lo]\n"
        "categories:\n"
        "  hi: {description: hi, sources: []}\n"
        "  lo: {description: lo, sources: []}\n"
    )

    def _lint(self, files: dict):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(self.CFG)
            m = tmp / "manual"
            m.mkdir()
            for name, text in files.items():
                (m / name).write_text(text)
            cfg = build.load_config(tmp / "config.yaml")
            return build.lint(cfg, tmp)

    def test_clean(self):
        self.assertEqual(self._lint({"hi.txt": "a.com\n+.b.com\n"}), [])

    def test_duplicate(self):
        errs = self._lint({"hi.txt": "a.com\na.com\n"})
        self.assertTrue(any("duplicate" in e for e in errs), errs)

    def test_add_exclude_overlap(self):
        errs = self._lint({"hi.txt": "a.com\n", "hi-exclude.txt": "a.com\n"})
        self.assertTrue(any("both add and exclude" in e for e in errs), errs)

    def test_missing_trailing_newline(self):
        errs = self._lint({"hi.txt": "a.com"})
        self.assertTrue(any("trailing newline" in e for e in errs), errs)

    def test_invalid_rule(self):
        errs = self._lint({"hi.txt": "*cdn.bad\n"})
        self.assertTrue(any("invalid rule" in e for e in errs), errs)

    def test_cross_category_double_pin(self):
        errs = self._lint({"hi.txt": "shared.com\n", "lo.txt": "shared.com\n"})
        self.assertTrue(any("pinned to both" in e for e in errs), errs)


class TestPublishGating(unittest.TestCase):
    """changed-detection (skip no-op publishes) and the disappeared-product gate."""
    CFG = (
        "defaults: {max-shrink-percent: 100}\n"
        "priority: [hi, lo]\n"
        "categories:\n"
        "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n"
        "  lo: {description: lo, sources: [{url: 'lo://x'}]}\n"
    )

    def _build(self, tmp: Path, upstream: dict, previous=None, out_name="out"):
        (tmp / "config.yaml").write_text(self.CFG)
        (tmp / "manual").mkdir(exist_ok=True)
        out = tmp / out_name

        def fake(url, timeout, retries):
            return upstream[url]

        cfg = build.load_config(tmp / "config.yaml")
        rc = build.cmd_build(cfg, tmp, out, previous, fake)
        return rc, out

    def test_changed_flag(self):
        import tempfile
        up = {"hi://x": "payload:\n  - 'a.com'\n", "lo://x": "payload:\n  - 'b.com'\n"}
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, out1 = self._build(tmp, up)
            self.assertEqual(rc, 0)
            # first publish (no previous) -> changed
            self.assertEqual((out1 / "changed.txt").read_text().strip(), "true")
            # identical rebuild vs previous -> unchanged (timestamp differs, payload same)
            rc, out2 = self._build(tmp, up, previous=out1, out_name="out2")
            self.assertEqual(rc, 0)
            self.assertEqual((out2 / "changed.txt").read_text().strip(), "false")
            # a new domain -> changed
            up2 = {"hi://x": "payload:\n  - 'a.com'\n  - 'c.com'\n",
                   "lo://x": "payload:\n  - 'b.com'\n"}
            rc, out3 = self._build(tmp, up2, previous=out1, out_name="out3")
            self.assertEqual((out3 / "changed.txt").read_text().strip(), "true")

    def test_disappeared_product_fails_gate(self):
        import tempfile
        up = {"hi://x": "payload:\n  - 'a.com'\n", "lo://x": "payload:\n  - 'b.com'\n"}
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, out1 = self._build(tmp, up)
            self.assertEqual(rc, 0)
            # plant a product in "previous" that this build won't reproduce
            (out1 / "final_hi_ipcidr.yaml").write_text("payload:\n  - '1.2.3.0/24'\n")
            rc2, _ = self._build(tmp, up, previous=out1, out_name="out2")
            self.assertEqual(rc2, 1)   # disappearance is gated -> refuse to publish


class TestUnsupportedRuleTypes(unittest.TestCase):
    """A rule type we do not emit must be SKIPPED, never counted as invalid.

    The invalid gate fails the build at one dropped line. If an unhandled but
    perfectly legal upstream rule type lands in that bucket, the first day an
    upstream adds a DOMAIN-REGEX / PROCESS-NAME / logic rule the whole build
    fails, nothing is published, and every subscriber's rules freeze at the
    last release — caused by a line that was never ours to parse. A gate that
    fires on someone else's valid input is worse than no gate.
    """

    UNSUPPORTED = [
        "DOMAIN-REGEX,^ad[0-9]+\\.x\\.com$,DIRECT",
        "PROCESS-NAME,Telegram,PROXY",
        "GEOSITE,cn,DIRECT",
        "GEOIP,CN,DIRECT,no-resolve",
        "RULE-SET,mycn,DIRECT",
        "SRC-IP-CIDR,192.168.1.0/24,DIRECT",
        "DST-PORT,443,PROXY",
        "NETWORK,udp,REJECT",
        "AND,((DOMAIN,x.com),(NETWORK,tcp)),DIRECT",
        "MATCH,DIRECT",
        "SUB-RULE,(NETWORK,tcp),sub",
        # Real mihomo / Surge types that were missing from the allowlist and so
        # counted as invalid. With the gate at 0 that means one upstream line of
        # any of these stops the publish for ALL SIX rule sets.
        "SRC-IP-ASN,13335,DIRECT",
        "DEST-PORT,443,PROXY",
        "PROTOCOL,udp,REJECT",
        "DOMAIN-SET,https://example.com/set.txt,DIRECT",
        "SUBNET,SSID:home,DIRECT",
        "CELLULAR-RADIO,LTE,PROXY",
        "DEVICE-NAME,iPhone,DIRECT",
    ]

    def test_all_skipped_not_invalid(self):
        for line in self.UNSUPPORTED:
            self.assertEqual(parse_line(line)[0], "unsupported", line)

    def test_stats_keep_them_out_of_the_invalid_bucket(self):
        text = "\n".join(["payload:", "  - 'a.com'"] + self.UNSUPPORTED)
        rules, st = parse_text(text)
        self.assertEqual(st.parsed, 1)
        self.assertEqual(st.dropped_invalid, 0, st.invalid_samples)
        self.assertEqual(st.dropped_unsupported, len(self.UNSUPPORTED))
        self.assertIn("DOMAIN-REGEX", st.unsupported_types)

    def test_a_real_malformed_line_is_still_invalid(self):
        """The escape hatch must not swallow genuine garbage."""
        for bad in ["*cdn.x.net", "not a domain", "foo,bar baz", "1.2.3.4"]:
            self.assertEqual(parse_line(bad)[0], "invalid", bad)

    def test_type_token_is_matched_case_insensitively(self):
        """Handled types are upper()-ed before dispatch; skipped types must be
        too. Testing the raw token made the builder lenient for the six types
        it parses and strict for every type it skips — one lowercase
        `process-name,...` line upstream and the whole publish stops."""
        for line in ["process-name,Telegram,PROXY", "Geosite,cn,DIRECT",
                     "and,((DOMAIN,x.com),(NETWORK,tcp)),DIRECT",
                     "MaTcH,DIRECT"]:
            self.assertEqual(parse_line(line)[0], "unsupported", line)

    def test_a_typo_in_a_handled_type_stays_loud(self):
        """Only names on the known-unsupported list may be skipped.

        Skipping "anything that looks like a type token" would swallow
        `DOMAIN-SUFIX,cn` with no signal at all — the same silent drop the
        invalid gate exists to catch, through a new door.
        """
        for typo in ["DOMAIN-SUFIX,cn", "DOMIAN-SUFFIX,cn", "IP-CDIR,1.1.1.0/24",
                     "HOSTSUFFIX,cn", "GARBAGE,,,"]:
            self.assertEqual(parse_line(typo)[0], "invalid", typo)

    def test_bom_and_yaml_scaffolding_are_not_invalid(self):
        """A BOM on line 1 used to make that line invalid. Three of the
        microsoft sources start with a `#` comment, so one Windows edit
        upstream would have failed the whole build with a baffling error."""
        self.assertEqual(parse_line("\ufeff# Microsoft Services")[0], "skip")
        self.assertEqual(parse_line("\ufeffpayload:")[0], "skip")
        self.assertEqual(parse_line("\ufeff  - '+.cn'"),
                         ("ok", Rule("suffix", "cn")))
        for scaffold in ["---", "...", "%YAML 1.2", "payload: []"]:
            self.assertEqual(parse_line(scaffold)[0], "skip", scaffold)

    def test_inline_comment_after_a_rule(self):
        """`.list` files commonly carry trailing comments."""
        self.assertEqual(parse_line("DOMAIN-SUFFIX,cn # China"),
                         ("ok", Rule("suffix", "cn")))
        self.assertEqual(parse_line("  - '+.cn'   # China"),
                         ("ok", Rule("suffix", "cn")))
        self.assertEqual(parse_line("# whole line")[0], "skip")

    def test_ip_types_validate_against_their_declared_type(self):
        """The IP branches used to re-sniff the value with classify_value, so
        `IP-CIDR,+.foo.com` became a domain SUFFIX rule and landed in the
        category's domain payload — a malformed IP line silently routing a
        whole suffix, past a green gate."""
        for bad in ["IP-CIDR,+.example.com", "IP-CIDR6,.example.com",
                    "IP-CIDR,example.com", "IP-CIDR,as123"]:
            self.assertEqual(parse_line(bad)[0], "invalid", bad)
        self.assertEqual(parse_line("IP-CIDR,1.1.1.0/24,no-resolve"),
                         ("ok", Rule("ip-cidr", "1.1.1.0/24")))
        self.assertEqual(parse_line("IP-CIDR6,2001:db8::/32"),
                         ("ok", Rule("ip-cidr6", "2001:db8::/32")))


class TestIpAsn(unittest.TestCase):
    def test_bare_number_is_the_canonical_clash_form(self):
        """`IP-ASN,13335` is how Clash writes it; only the standalone token
        form is `AS13335`. Routing the bare number through the domain parser
        made the canonical spelling count as invalid — one upstream line with
        it would fail the whole build once the gate is at 0."""
        self.assertEqual(parse_line("IP-ASN,13335,DIRECT"),
                         ("ok", Rule("ip-asn", "AS13335")))
        self.assertEqual(parse_line("IP-ASN,AS13335,DIRECT"),
                         ("ok", Rule("ip-asn", "AS13335")))
        self.assertEqual(parse_line("IP-ASN,notanasn,DIRECT")[0], "invalid")
        self.assertEqual(classify_value("AS13335"), Rule("ip-asn", "AS13335"))


class TestSubtractDoesNotMutateArgument(unittest.TestCase):
    def test_other_survives_intact(self):
        """subtract() used to compress its argument in place. apply_partition
        threads one accumulating `claimed` set through every category, so the
        side effect silently rewrote shared state mid-loop."""
        a = DomainSet.from_rules([Rule("suffix", "x.com"), Rule("exact", "y.com")])
        other = DomainSet.from_rules(
            [Rule("suffix", "x.com"), Rule("exact", "a.x.com"), Rule("exact", "y.com")])
        before = sorted(other.to_payload())
        a.subtract(other)
        self.assertEqual(sorted(other.to_payload()), before)


class TestPreviousBaselineIsRequiredWhenAskedFor(unittest.TestCase):
    def test_missing_previous_dir_is_an_error_not_a_mode(self):
        """A typo'd --previous silently turned every gate off and looked
        exactly like a first publish: a gutted product would ship with rc=0."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "priority: [hi]\ncategories:\n  hi: {description: hi, sources: []}\n")
            (tmp / "manual").mkdir()
            rc = build.main(["--root", str(tmp), "--config", "config.yaml",
                             "build", "--out", str(tmp / "out"),
                             "--previous", str(tmp / "nope")])
            self.assertEqual(rc, 1)


class TestExcludeNoopIsReported(unittest.TestCase):
    def test_an_exclude_that_matches_nothing_is_surfaced(self):
        """A stale exclude is indistinguishable from a working one: upstream
        renamed Sukka's watermark domain and the old entry silently became a
        no-op, shipping the watermark for months."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "priority: [hi]\ncategories:\n"
                "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n")
            (tmp / "manual").mkdir()
            (tmp / "manual" / "hi-exclude.txt").write_text("gone.example\n")
            cfg = build.load_config(tmp / "config.yaml")
            out = tmp / "out"
            rc = build.cmd_build(cfg, tmp, out, None,
                                 lambda u, t_, r: "payload:\n  - 'a.com'\n")
            self.assertEqual(rc, 0)
            self.assertIn("gone.example", (out / "report.md").read_text())
            self.assertIn("matched nothing", (out / "report.md").read_text())


class TestLintCatchesDeadManualEdits(unittest.TestCase):
    def _lint(self, tmp, files):
        (tmp / "config.yaml").write_text(
            "priority: [direct]\ncategories:\n"
            "  direct: {description: d, sources: []}\n")
        (tmp / "manual").mkdir(exist_ok=True)
        for n, body in files.items():
            (tmp / "manual" / n).write_text(body)
        return build.lint(build.load_config(tmp / "config.yaml"), tmp)

    def test_misnamed_file_is_never_read(self):
        """`dirct.txt` / `Direct.txt` / `direct_exclude.txt` lint clean and are
        never opened — the whole edit does nothing."""
        import tempfile
        for name in ("dirct.txt", "Direct.txt", "direct_exclude.txt"):
            with tempfile.TemporaryDirectory() as d:
                errs = self._lint(Path(d), {name: "example.com\n"})
                self.assertTrue(any(name in e for e in errs), f"{name}: {errs}")

    def test_unsupported_type_in_a_manual_file_is_an_error(self):
        """'unsupported' is right for upstream files we do not control; in a
        hand-written file it means the line you added does nothing. Before the
        unsupported bucket existed these were 'invalid' and lint caught them."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            errs = self._lint(Path(d), {"direct.txt": "GEOSITE,cn\nDOMAIN-KEYWORD,x\n"})
            self.assertEqual(len(errs), 2, errs)


class TestAsnNeverEntersAnIpcidrProduct(unittest.TestCase):
    def test_asn_rules_are_kept_out(self):
        """`behavior: ipcidr` payloads carry CIDRs only; an `AS####` entry
        makes mihomo reject the whole provider."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "priority: [hi]\ncategories:\n"
                "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n")
            (tmp / "manual").mkdir()
            cfg = build.load_config(tmp / "config.yaml")
            out = tmp / "out"
            rc = build.cmd_build(
                cfg, tmp, out, None,
                lambda u, t_, r: "IP-ASN,13335,DIRECT\nIP-CIDR,1.1.1.0/24,DIRECT\n")
            self.assertEqual(rc, 0)
            ipc = (out / "final_hi_ipcidr.yaml").read_text()
            self.assertIn("1.1.1.0/24", ipc)
            self.assertNotIn("AS13335", ipc)


class TestProductRemovalKnob(unittest.TestCase):
    CFG = ("defaults: {{allow-product-removal: {flag}}}\n"
           "priority: [hi]\ncategories:\n"
           "  hi: {{description: hi, sources: [{{url: 'hi://x'}}]}}\n")

    def _run(self, flag):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(self.CFG.format(flag=flag))
            (tmp / "manual").mkdir()
            cfg = build.load_config(tmp / "config.yaml")
            prev = tmp / "prev"; prev.mkdir()
            (prev / "final_hi.yaml").write_text("payload:\n  - 'a.com'\n")
            (prev / "final_hi_ipcidr.yaml").write_text("payload:\n  - '1.2.3.0/24'\n")
            return build.cmd_build(cfg, tmp, tmp / "out", prev,
                                   lambda u, t_, r: "payload:\n  - 'a.com'\n")

    def test_removal_is_gated_by_default_but_has_a_documented_way_out(self):
        """Creating a product is ungated, destroying one is fatal and
        self-perpetuating: an upstream that ships one IP rule for a day, then
        drops it, fails every run from then on because the file is still on the
        release branch. The knob is the only recovery that is not hand-editing
        that branch."""
        self.assertEqual(self._run("false"), 1)
        self.assertEqual(self._run("true"), 0)


class TestBareTldGate(unittest.TestCase):
    """A vanishing bare TLD must fail the build even though it is one line.

    The invalid gate only sees lines WE drop. When the UPSTREAM drops `+.cn`
    the product goes 110757 -> 110756: shrink gate silent, invalid gate silent,
    product-disappeared gate silent — and the entire .cn top-level domain has
    no direct rule, so every Chinese .cn site falls through to the catch-all
    group and leaves over a metered VPS. That is the 2026-08-11 incident with
    the cause moved one step upstream, and nothing else in this file catches it.
    """

    def test_a_disappearing_tld_fails_even_though_it_is_one_line(self):
        old = ["+.cn", "+.icbc"] + [f"+.host{i}.com" for i in range(5000)]
        new = [p for p in old if p != "+.cn"]
        with self.assertRaises(GateError) as cm:
            check_tld_gate("final_direct.yaml", new, old)
        self.assertIn("+.cn", str(cm.exception))
        self.assertIn("allow-tld-removal", str(cm.exception))

    def test_ordinary_churn_and_growth_do_not_trip_it(self):
        old = ["+.cn", "+.a.com", "+.b.com"]
        check_tld_gate("f", ["+.cn", "+.a.com", "+.c.com"], old)   # host swapped
        check_tld_gate("f", ["+.cn", "+.icbc", "+.a.com"], old)    # TLD added
        check_tld_gate("f", ["+.a.com"], None)                     # first publish

    def test_an_exact_rule_named_like_a_tld_is_not_one(self):
        """Only `+.cn` is the whole TLD. A bare `cn` exact entry matches
        nothing real and must not arm or satisfy the gate."""
        check_tld_gate("f", [], ["cn", "a.com"])

    def test_the_knob_turns_it_off_for_one_run(self):
        """End-to-end: the gate is wired into cmd_build and honours the knob."""
        import tempfile
        for flag, want in (("false", 1), ("true", 0)):
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                (tmp / "config.yaml").write_text(
                    f"defaults: {{max-shrink-percent: 100, allow-tld-removal: {flag}}}\n"
                    "priority: [hi]\ncategories:\n"
                    "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n")
                (tmp / "manual").mkdir()
                prev = tmp / "prev"
                prev.mkdir()
                (prev / "final_hi.yaml").write_text(
                    "payload:\n  - '+.cn'\n  - '+.keep.com'\n")
                rc = build.cmd_build(build.load_config(tmp / "config.yaml"), tmp,
                                     tmp / "out", prev,
                                     lambda u, t_, r: "payload:\n  - '+.keep.com'\n")
                self.assertEqual(rc, want, flag)


class TestPerSourceGate(unittest.TestCase):
    """A dead upstream source must be loud even when its category is not.

    The shrink gate measures a whole category, so in a multi-source category the
    survivors hide the corpse. Measured 2026-08 by blanking each source in turn:
    7 of 10 sources could return an empty payload for under 8% category shrink,
    and apple's ONLY source could die with the product not moving by a single
    entry, because manual/apple.txt carries all of it.
    """

    def test_a_source_that_goes_to_zero_is_always_fatal(self):
        with self.assertRaises(GateError) as cm:
            check_source_gate("microsoft", {"u": 0}, {"u": 81}, 30)
        self.assertIn("dead", str(cm.exception))

    def test_percentage_only_applies_above_the_floor(self):
        """ACL4SSR Bing contributes 3 rules. Judging it by percentage would fail
        the build every time it moved by one line — a gate that cries wolf gets
        its limit raised, and then it guards nothing."""
        check_source_gate("microsoft", {"u": 2}, {"u": 3}, 30)     # 33%, below floor
        with self.assertRaises(GateError):
            check_source_gate("reject", {"u": 100}, {"u": 1000}, 30)

    def test_new_and_removed_sources_are_not_gated(self):
        check_source_gate("c", {"new": 5}, {"old": 900}, 30)   # config changed
        check_source_gate("c", {}, None, 30)                   # no baseline yet

    def test_end_to_end_a_blanked_source_fails_even_with_an_unchanged_product(self):
        """The apple case exactly: one source, product identical, gate must fire."""
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "defaults: {max-shrink-percent: 100}\npriority: [hi]\ncategories:\n"
                "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n")
            (tmp / "manual").mkdir()
            # manual carries the whole product, so upstream dying changes nothing
            (tmp / "manual" / "hi.txt").write_text("+.example.com\n")
            prev = tmp / "prev"
            prev.mkdir()
            (prev / "final_hi.yaml").write_text("payload:\n  - '+.example.com'\n")
            (prev / "sources.json").write_text(_json.dumps({"hi": {"hi://x": 164}}))
            rc = build.cmd_build(build.load_config(tmp / "config.yaml"), tmp,
                                 tmp / "out", prev,
                                 lambda u, t_, r: "payload: []\n")
            self.assertEqual(rc, 1)
            self.assertIn("dead", (tmp / "out" / "report.md").read_text())

    def test_a_corrupt_baseline_is_an_error_not_a_missing_one(self):
        """Falling back to "no baseline" would switch the gate off for exactly
        as long as the file stayed broken, and nothing would say so."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "sources.json").write_text("{not json")
            with self.assertRaises(GateError):
                build.read_source_counts(tmp)

    def test_a_missing_baseline_forces_one_publish_so_it_can_be_created(self):
        """The gate's baseline lives on the release branch, and the publisher
        only pushes when `changed` is true. Without this, the first run after
        the gate shipped would find the rules unchanged, skip the publish,
        never write sources.json — and the gate would sit armed in the code and
        absent in reality, forever."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "priority: [hi]\ncategories:\n"
                "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n")
            (tmp / "manual").mkdir()
            prev = tmp / "prev"
            prev.mkdir()
            (prev / "final_hi.yaml").write_text("payload:\n  - 'a.com'\n")
            out = tmp / "out"
            build.cmd_build(build.load_config(tmp / "config.yaml"), tmp, out, prev,
                            lambda u, t_, r: "payload:\n  - 'a.com'\n")
            # identical rules, yet it must publish — otherwise no baseline
            self.assertEqual((out / "changed.txt").read_text().strip(), "true")
            self.assertTrue((out / "sources.json").exists())

    def test_the_baseline_is_written_next_to_the_products(self):
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "priority: [hi]\ncategories:\n"
                "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n")
            (tmp / "manual").mkdir()
            out = tmp / "out"
            build.cmd_build(build.load_config(tmp / "config.yaml"), tmp, out, None,
                            lambda u, t_, r: "payload:\n  - 'a.com'\n")
            self.assertEqual(_json.loads((out / "sources.json").read_text()),
                             {"hi": {"hi://x": 1}})


class TestPartitionTransfersAreReported(unittest.TestCase):
    def test_what_a_category_lost_and_to_whom(self):
        """`conflicts` lists the exclusions that could NOT happen. The ones that
        DID happen were invisible, and they are the expensive half: a broad
        upstream suffix in a high-priority category swallows specific hosts out
        of a lower one, which is a routing decision nobody made. Measured
        2026-08: 137 proxy hosts taken by microsoft, 403 direct hosts by proxy.
        """
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "defaults: {max-shrink-percent: 100}\npriority: [hi, lo]\n"
                "categories:\n"
                "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n"
                "  lo: {description: lo, sources: [{url: 'lo://x'}]}\n")
            (tmp / "manual").mkdir()
            up = {"hi://x": "payload:\n  - '+.shared.net'\n",
                  "lo://x": "payload:\n  - 'a.shared.net'\n  - 'b.shared.net'\n"}
            out = tmp / "out"
            build.cmd_build(build.load_config(tmp / "config.yaml"), tmp, out, None,
                            lambda u, t_, r: up[u])
            rpt = (out / "report.md").read_text()
            self.assertIn("partition transfers", rpt)
            self.assertIn("**lo**: lost 2 entries (hi 2)", rpt)
            self.assertIn("`+.shared.net` (hi) took 2", rpt)

    def test_one_for_one_transfers_are_summarised_not_listed(self):
        """A suffix that swallowed many hosts is actionable — one exclude line
        undoes it. A 1:1 transfer is the partition doing its job. Listing every
        one of them buries the handful that matter (proxy has 134 transfers, of
        which the interesting ones are a dozen)."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "defaults: {max-shrink-percent: 100}\npriority: [hi, lo]\n"
                "categories:\n"
                "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n"
                "  lo: {description: lo, sources: [{url: 'lo://x'}]}\n")
            (tmp / "manual").mkdir()
            shared = "".join(f"  - 'n{i}.com'\n" for i in range(4))
            up = {"hi://x": f"payload:\n{shared}", "lo://x": f"payload:\n{shared}"}
            out = tmp / "out"
            build.cmd_build(build.load_config(tmp / "config.yaml"), tmp, out, None,
                            lambda u, t_, r: up[u])
            rpt = (out / "report.md").read_text()
            self.assertIn("plus 4 one-for-one transfer(s)", rpt)
            self.assertNotIn("n0.com", rpt)


class TestExcludeRerouteNote(unittest.TestCase):
    def test_excluding_from_a_non_last_category_is_flagged_as_a_reroute(self):
        """Both docs described `-exclude.txt` as deleting without rerouting.
        It runs BEFORE the partition, so dropping a domain from a high-priority
        category releases the claim and the next category carrying it takes
        over — the traffic moves. Only the last routing category is safe."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "priority: [hi, lo]\ncategories:\n"
                "  hi: {description: hi, sources: []}\n"
                "  lo: {description: lo, sources: []}\n")
            (tmp / "manual").mkdir()
            (tmp / "manual" / "hi-exclude.txt").write_text("x.com\n")
            (tmp / "manual" / "lo-exclude.txt").write_text("y.com\n")
            cfg = build.load_config(tmp / "config.yaml")
            notes = build.lint_notes(cfg, tmp)
            self.assertEqual(len(notes), 1, notes)      # only hi, not the last one
            self.assertIn("REROUTES", notes[0])
            self.assertIn("x.com", notes[0])
            self.assertEqual(build.lint(cfg, tmp), [])  # a note is never an error


class TestReportIsAppendedToTheStepSummary(unittest.TestCase):
    def test_an_earlier_step_is_not_clobbered(self):
        """GITHUB_STEP_SUMMARY is one workflow-wide buffer. Overwriting it works
        today only because nothing else writes there — which is why the day
        something does, the loss is silent."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            summary = tmp / "summary.md"
            summary.write_text("EARLIER STEP OUTPUT\n")
            (tmp / "config.yaml").write_text(
                "priority: [hi]\ncategories:\n"
                "  hi: {description: hi, sources: [{url: 'hi://x'}]}\n")
            (tmp / "manual").mkdir()
            old = os.environ.get("GITHUB_STEP_SUMMARY")
            os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
            try:
                build.cmd_build(build.load_config(tmp / "config.yaml"), tmp,
                                tmp / "out", None,
                                lambda u, t_, r: "payload:\n  - 'a.com'\n")
            finally:
                if old is None:
                    del os.environ["GITHUB_STEP_SUMMARY"]
                else:
                    os.environ["GITHUB_STEP_SUMMARY"] = old
            text = summary.read_text()
            self.assertIn("EARLIER STEP OUTPUT", text)
            self.assertIn("build report", text)


class TestUnreachableSourceIsARefusalNotACrash(unittest.TestCase):
    def test_it_exits_1_with_a_readable_message(self):
        """A source that stays unreachable after every retry used to escape as a
        bare urllib traceback. The outcome was right — nothing published — but
        the message named an exception, not the category or the URL, so it read
        as a crash in the builder rather than a refusal to publish."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "config.yaml").write_text(
                "defaults: {retries: 1, timeout-seconds: 2}\n"
                "priority: [hi]\ncategories:\n"
                "  hi: {description: hi, sources: "
                "[{url: 'file:///nonexistent/clash-rules-test'}]}\n")
            (tmp / "manual").mkdir()
            rc = build.main(["--root", str(tmp), "build", "--out", str(tmp / "out")])
            self.assertEqual(rc, 1)


class TestRealConfig(unittest.TestCase):
    """Pins the repo's own config.yaml, not a fixture.

    Every knob below is one whose wrong value is silent: the build still runs,
    the products still publish, and a safety mechanism is simply not there any
    more. A fixture-only test suite cannot see that.
    """

    @classmethod
    def setUpClass(cls):
        cls.cfg = build.load_config(REPO / "config.yaml")

    def test_routing_categories_allow_no_invalid_lines(self):
        """`+.cn` was 1 line in 111516. Any nonzero limit on a routing category
        re-opens that exact hole, and raising one is a two-character edit."""
        for name in self.cfg.priority:
            self.assertEqual(self.cfg.categories[name].max_invalid, 0, name)

    def test_reject_is_the_only_category_with_slack(self):
        slack = [n for n, c in self.cfg.categories.items() if c.max_invalid > 0]
        self.assertEqual(slack, ["reject"], slack)

    def test_escape_hatches_are_closed(self):
        """Both are "set true, publish once, set it back" knobs. Left on, the
        gate they disable is gone and nothing ever says so again."""
        self.assertFalse(self.cfg.allow_product_removal)
        self.assertFalse(self.cfg.allow_tld_removal)

    def test_every_category_has_at_least_one_source(self):
        for name, c in self.cfg.categories.items():
            self.assertTrue(c.sources, name)

    def test_every_source_url_is_https(self):
        for name, c in self.cfg.categories.items():
            for s in c.sources:
                self.assertTrue(s.url.startswith("https://"), f"{name}: {s.url}")

    def test_reject_is_an_overlay_and_stays_out_of_the_partition(self):
        """In the partition, reject would strip ad domains out of direct/proxy
        instead of overlaying them, and the RULE-SET order in the README stops
        describing what the products do."""
        self.assertNotIn("reject", self.cfg.priority)
        extra = set(self.cfg.categories) - set(self.cfg.priority)
        self.assertEqual(extra, {"reject"}, extra)

    def test_priority_matches_the_order_the_readme_tells_people_to_use(self):
        """~330 domains are covered by another category's broader suffix, and
        which policy they land on is decided purely by RULE-SET order. The
        products are built assuming the README's order; if the two drift, those
        domains route the other way and nothing reports it."""
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        order = [n for n in self.cfg.priority
                 if f"RULE-SET,{n}," in readme]
        self.assertEqual(order, self.cfg.priority)
        positions = [readme.index(f"RULE-SET,{n},") for n in self.cfg.priority]
        self.assertEqual(positions, sorted(positions), self.cfg.priority)

    def test_publish_branch_is_what_the_subscription_urls_point_at(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"clash-rules/{self.cfg.publish_branch}/", readme)


class TestRealManualFiles(unittest.TestCase):
    def test_the_repos_own_manual_files_lint_clean(self):
        cfg = build.load_config(REPO / "config.yaml")
        self.assertEqual(build.lint(cfg, REPO), [])


if __name__ == "__main__":
    unittest.main()
