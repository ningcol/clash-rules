"""Unit tests for scripts/build.py. Run: python -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build  # noqa: E402
from build import (  # noqa: E402
    Rule, DomainSet, classify_value, parse_line, parse_text,
    check_gate, GateError,
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
        rc = build.cmd_build(cfg, tmp, out, None, False, fake)
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


if __name__ == "__main__":
    unittest.main()
