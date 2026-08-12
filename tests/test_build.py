"""Unit tests for scripts/build.py. Run: python -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build  # noqa: E402
from build import (  # noqa: E402
    Rule, DomainSet, classify_value, parse_line, parse_text,
    check_gate, check_invalid_gate, GateError,
)


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
            check_invalid_gate("direct", 1, ["upstream: - '+.cn'"], 0)
        self.assertIn("+.cn", str(cm.exception))
        check_invalid_gate("direct", 0, [], 0)      # nothing dropped -> ok
        check_invalid_gate("reject", 2, ["x"], 20)  # reject's slack -> ok


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


if __name__ == "__main__":
    unittest.main()


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
