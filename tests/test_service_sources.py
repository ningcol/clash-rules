"""服务上游的离线回归：更新能跟进，精确主机不被共享后缀过滤误删。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build
from build import GateError, Rule

REPO = Path(__file__).resolve().parent.parent


class TestSourceFiltering(unittest.TestCase):
    def config(self, root, exclusion="+.shared.example"):
        (root / "config.yaml").write_text(
            "priority: [service]\ncategories:\n  service:\n    sources:\n"
            f"      - {{url: 'https://one.example/list', exclude: ['{exclusion}']}}\n"
            "      - {url: 'https://two.example/list'}\n")
        (root / "manual").mkdir()
        return build.load_config(root / "config.yaml")

    def test_filter_is_source_local_and_keeps_other_precise_hosts(self):
        """类目级排除会误删另一上游的精确头像主机；单源排除不能波及它与手工补充。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = self.config(root)
            (root / "manual/service.txt").write_text("manual.shared.example\n")
            bodies = {
                "https://one.example/list": "+.shared.example\nonly-one.example\n1.2.3.0/24\n",
                "https://two.example/list": "image.shared.example\nonly-two.example\n",
            }
            result = build.build_category(cfg.categories["service"], cfg, root / "manual",
                                          lambda u, t, r: bodies[u])
            self.assertEqual(result.domains.to_payload(), [
                "image.shared.example", "manual.shared.example", "only-one.example", "only-two.example"])
            self.assertEqual(result.ips, [Rule("ip-cidr", "1.2.3.0/24")])
            # 逐源门禁仍记录原始解析条数，不能因为过滤而把基线误判为上游缩水。
            self.assertEqual(result.source_counts["https://one.example/list"], 3)

    def test_untrimmable_source_suffix_refuses_to_build(self):
        """上游扩成共享后缀后若精确排除无法实现，须阻止发布，不能留下过宽的服务规则。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = self.config(root, "image.shared.example")
            with self.assertRaisesRegex(GateError, "source exclusion cannot be applied"):
                build.cmd_build(cfg, root, root / "out", None,
                                lambda u, t, r: "+.shared.example\n")
            self.assertFalse((root / "out/final_service.yaml").exists())

    def test_bad_exclusions_are_configuration_errors(self):
        """写错过滤条件不能被静默忽略；拒绝关键字、IP、非列表与非字符串。"""
        import yaml
        for values in ["+.shared.example", ["DOMAIN-KEYWORD,shared"], ["1.2.3.0/24"], [12]]:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "config.yaml").write_text(yaml.safe_dump({
                    "priority": ["service"], "categories": {"service": {"sources": [
                        {"url": "https://one.example/list", "exclude": values}]}}}))
                with self.assertRaises(SystemExit):
                    build.load_config(root / "config.yaml")


class TestTldTransfers(unittest.TestCase):
    def run_build(self, transfer, service="+.brand\n+.keep.example\n", proxy="+.brand\n+.cn\n"):
        import yaml
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            defaults = {"max-shrink-percent": 100, "allow-tld-removal": False}
            if transfer is not None:
                defaults["tld-transfers"] = transfer
            (root / "config.yaml").write_text(yaml.safe_dump({
                "defaults": defaults, "priority": ["service", "proxy"],
                "categories": {name: {"sources": [{"url": f"https://{name}.example/list"}]}
                               for name in ["service", "proxy"]}}))
            cfg = build.load_config(root / "config.yaml")
            (root / "manual").mkdir()
            previous = root / "previous"
            previous.mkdir()
            (previous / "final_service.yaml").write_text("payload:\n  - '+.keep.example'\n")
            (previous / "final_proxy.yaml").write_text("payload:\n  - '+.brand'\n  - '+.cn'\n")
            bodies = {"https://service.example/list": service, "https://proxy.example/list": proxy}
            rc = build.cmd_build(cfg, root, root / "out", previous, lambda u, t, r: bodies[u])
            if rc == 0:
                self.assertIn("+.brand", build.read_payload(root / "out/final_service.yaml"))
            return rc

    def test_transfer_requires_explicit_declaration(self):
        self.assertEqual(self.run_build(None), 1)
        self.assertEqual(self.run_build([{"domain": "brand", "from": "proxy", "to": "service"}]), 0)

    def test_transfer_requires_same_suffix_in_target(self):
        self.assertEqual(self.run_build([{"domain": "brand", "from": "proxy", "to": "service"}],
                                        service="+.keep.example\n", proxy="+.cn\n"), 1)

    def test_other_tld_removals_still_fail(self):
        self.assertEqual(self.run_build([{"domain": "brand", "from": "proxy", "to": "service"}],
                                        proxy="+.brand\n"), 1)

    def test_invalid_declarations_are_configuration_errors(self):
        for transfer in ["brand", [{}], [{"domain": "brand.example", "from": "proxy", "to": "service"}],
                         [{"domain": "brand", "from": "proxy", "to": "unknown"}],
                         [{"domain": "brand", "from": "proxy", "to": "proxy"}]]:
            with self.subTest(transfer=transfer), self.assertRaises(SystemExit):
                self.run_build(transfer)


class TestRealServiceSources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = build.load_config(REPO / "config.yaml")
        cls.bodies = {}
        for file in (REPO / "tests/fixtures/services").glob("*.txt"):
            body = file.read_text()
            url = body.splitlines()[0].split("：", 1)[1]
            cls.bodies[url] = body

    def category(self, name, bodies=None):
        sources = self.bodies if bodies is None else bodies
        return build.build_category(self.cfg.categories[name], self.cfg, REPO / "manual",
                                    lambda u, t, r: sources[u])

    def test_every_service_has_multiple_distinct_upstreams(self):
        """删除第二来源后必须失败：四个服务都由两份不同上游互补，不能退回单来源。"""
        for name in ["youtube", "speedtest", "chatgpt", "claude"]:
            urls = [source.url for source in self.cfg.categories[name].sources]
            self.assertGreaterEqual(len(set(urls)), 2, name)
            self.assertEqual(len(urls), len(set(urls)), name)

    def test_core_and_previously_missing_domains_are_covered(self):
        """实际来源、过滤和补充一起验证，防止上游接上但播放器、头像、官方新域仍漏配。"""
        expected = {
            "youtube": ["www.youtube.com", "www.youtube.co.jp", "r1.googlevideo.com", "i.ytimg.com",
                        "youtubeembeddedplayer.googleapis.com", "yt3.googleusercontent.com", "yt3.ggpht.com",
                        "youtubemobilesupport.com", "youtubefanfest.com"],
            "speedtest": ["www.speedtest.net", "server.ooklaserver.net", "speed.cloudflare.com", "fast.com",
                          "speed.dler.io", "cdnst.net", "ookla.com", "www.speedtest.net.cdn.cloudflare.net"],
            "chatgpt": ["chatgpt.com", "api.openai.com", "cdn.oaistatic.com", "files.oaiusercontent.com",
                        "openaiassets.blob.core.windows.net", "openaiapi-site.azureedge.net", "cdn.openaimerge.com"],
            "claude": ["api.anthropic.com", "platform.claude.com", "downloads.claude.ai",
                       "bridge.claudeusercontent.com", "claudemcpcontent.com", "clau.de", "claude.app",
                       "livepreview.claude.app", "claude.site"],
        }
        shared = ["auth0.com", "stripe.com", "sentry.io", "challenges.cloudflare.com",
                  "gvt1.com", "gvt2.com", "ggpht.com", "ggpht.cn", "host.livekit.cloud", "turn.livekit.cloud",
                  "client-api.arkoselabs.com", "events.statsigapi.net", "featuregates.org", "identrust.com",
                  "intercom.io", "intercomcdn.com", "cdn.usefathom.com"]
        for name, domains in expected.items():
            result = self.category(name)
            self.assertTrue(result.source_counts, name + " 未接入上游")
            for domain in domains:
                self.assertTrue(result.domains.covered(Rule("exact", domain)), (name, domain))
            for domain in shared:
                self.assertFalse(result.domains.covered(Rule("exact", domain)), (name, domain))

    def test_new_upstream_domains_arrive_without_manual_edits(self):
        """上游日后新增专属域必须自动进入产物；退回纯手工或未实际读取来源时此测试失败。"""
        for name in ["youtube", "speedtest", "chatgpt", "claude"]:
            with self.subTest(name=name):
                category = self.cfg.categories[name]
                self.assertTrue(category.sources, name)
                for index, source in enumerate(category.sources):
                    domain = f"future-{name}-{index}.example"
                    bodies = dict(self.bodies)
                    bodies[source.url] += domain + "\n"
                    self.assertTrue(self.category(name, bodies).domains.covered(Rule("exact", domain)),
                                    source.url)

    def test_manual_files_do_not_duplicate_upstream_domains(self):
        """重复手工钉住上游域名会使上游删除无法生效，只保留确实缺失的本地补充。"""
        for name in ["youtube", "speedtest", "chatgpt", "claude"]:
            category = self.cfg.categories[name]
            upstream = build.DomainSet()
            for source in category.sources:
                rules, _ = build.parse_text(self.bodies[source.url])
                for rule in rules:
                    if rule.kind in ("exact", "suffix"):
                        upstream.add(rule)
            for rule in build._read_manual(REPO / "manual", name):
                self.assertFalse(upstream.covered(rule), (name, rule))


class TestMultiSourceBaseline(unittest.TestCase):
    def test_redundant_new_source_is_published_then_gated(self):
        """新增上游没有新增域名也要发布基线，否则它随后清空时逐源门禁永远不生效。"""
        import json
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "manual").mkdir()
            (root / "config.yaml").write_text(
                "priority: [service]\ncategories:\n  service:\n    sources:\n"
                "      - {url: 'https://one.example/list'}\n")
            cfg = build.load_config(root / "config.yaml")
            bodies = {"https://one.example/list": "+.service.example\n",
                      "https://two.example/list": "api.service.example\n"}
            fetch = lambda u, t, r: bodies[u]
            first = root / "first"
            self.assertEqual(build.cmd_build(cfg, root, first, None, fetch), 0)
            cfg.categories["service"].sources.append(build.Source("https://two.example/list"))
            second = root / "second"
            self.assertEqual(build.cmd_build(cfg, root, second, first, fetch), 0)
            self.assertEqual(build.read_payload(first / "final_service.yaml"),
                             build.read_payload(second / "final_service.yaml"))
            self.assertEqual((second / "changed.txt").read_text().strip(), "true")
            self.assertEqual(json.loads((second / "sources.json").read_text())["service"],
                             {url: 1 for url in bodies})
            third = root / "third"
            self.assertEqual(build.cmd_build(cfg, root, third, second, fetch), 0)
            self.assertEqual((third / "changed.txt").read_text().strip(), "false")
            bodies["https://two.example/list"] = ""
            failed = root / "failed"
            self.assertEqual(build.cmd_build(cfg, root, failed, second, fetch), 1)
            self.assertFalse((failed / "final_service.yaml").exists())


if __name__ == "__main__":
    unittest.main()
