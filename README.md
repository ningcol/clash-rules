# Clash Rules

[![Check](https://github.com/ningcol/clash-rules/actions/workflows/check.yml/badge.svg)](https://github.com/ningcol/clash-rules/actions/workflows/check.yml)
[![Publish](https://github.com/ningcol/clash-rules/actions/workflows/publish.yml/badge.svg)](https://github.com/ningcol/clash-rules/actions/workflows/publish.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

自动化构建的 Clash 规则集，每日自动更新。规则源码与维护输入在 `main` 分支，构建产物发布在 **`release` 分支**。

## 📋 订阅链接

> 订阅 URL 指向 `release` 分支。规则每日构建（北京时间 05:00）。

<!-- BUILD:SUBSCRIPTIONS:BEGIN -->
| 规则类型 | 说明 | raw 订阅 | jsDelivr 订阅（国内更稳） |
|---------|------|----------|--------------------------|
| MICROSOFT | 微软服务规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_microsoft.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_microsoft.yaml) |
| APPLE | 苹果服务规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_apple.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_apple.yaml) |
| ICLOUD | iCloud 服务规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_icloud.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_icloud.yaml) |
| PROXY | 代理规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_proxy.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_proxy.yaml) |
| DIRECT | 直连规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_direct.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_direct.yaml) |
| REJECT | 广告拦截规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_reject.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_reject.yaml) |
<!-- BUILD:SUBSCRIPTIONS:END -->

## 🚀 快速使用

```yaml
rule-providers:
  reject:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_reject.yaml"
    path: ./ruleset/reject.yaml
    interval: 86400
  microsoft:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_microsoft.yaml"
    path: ./ruleset/microsoft.yaml
    interval: 86400
  apple:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_apple.yaml"
    path: ./ruleset/apple.yaml
    interval: 86400
  icloud:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_icloud.yaml"
    path: ./ruleset/icloud.yaml
    interval: 86400
  proxy:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_proxy.yaml"
    path: ./ruleset/proxy.yaml
    interval: 86400
  direct:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_direct.yaml"
    path: ./ruleset/direct.yaml
    interval: 86400

rules:
  - RULE-SET,reject,REJECT
  - RULE-SET,microsoft,DIRECT
  - RULE-SET,apple,DIRECT
  - RULE-SET,icloud,DIRECT
  - RULE-SET,proxy,PROXY
  - RULE-SET,direct,DIRECT
  - MATCH,PROXY
```

> 规则集之间基本互不重叠（同一域名只出现在一个路由类目里），因此绝大多数域名的路由与 RULE-SET 顺序无关。**建议保持上面的顺序**（与构建优先级 `microsoft → apple → icloud → proxy → direct` 一致）：少数域名被上游的广义后缀规则（如 direct 源里的 `+.mi.com`、proxy 源里的共享 CA `+.digicert.com`）覆盖，而 domain 格式无法对后缀做“减一个子域”的裁剪，这些域名依赖此顺序才能路由到正确的策略。

> **jsDelivr 缓存**：`@release` 分支形式的 jsDelivr 链接有约 12 小时 CDN 缓存，push 后最长约半天才刷新；想立即生效可用上面的 raw 链接，或访问一次 `https://purge.jsdelivr.net/gh/ningcol/clash-rules@release/<文件名>` 强制回源。

## 🧩 工作原理

- **划分（partition）**：路由类目（microsoft / apple / icloud / proxy / direct）构成一个划分——每个域名基本只出现在其中一个规则集里，因此路由基本与 RULE-SET 顺序无关。极少数域名因上游广义后缀规则重叠（构建时会报告 conflict、不影响构建），需靠上面推荐的 RULE-SET 顺序消歧。`reject` 是策略叠加层，不参与划分。
- **优先级 + 手工指派**：域名归属由 `config.yaml` 的 `priority` 顺序决定（前者从后者中排除）；`manual/<类目>.txt` 里的手工指派**优先于**此顺序——写进哪个类目就钉在哪个类目，并自动从其他路由类目移除。
- **语义去重**：用域名后缀树去重，`+.example.com` 存在时自动压掉其覆盖的所有子域。

## 🛠️ 如何维护

只改两处：`config.yaml`（类目、规则源、优先级、阈值）和 `manual/` 目录。产物 `final_*.yaml` 是生成的，**不要手改**。

| 我想做的事 | 改哪里 |
|-----------|--------|
| 换/加一个上游规则源 | `config.yaml` 对应类目的 `sources` |
| 手工加域名到某类目 | `manual/<类目>.txt`（写清原因+日期） |
| **强制某域名走某策略** | `manual/<目标类目>.txt`（一处，自动从其他类目移除） |
| 从某规则集删域名（不改路由） | `manual/<类目>-exclude.txt` |
| 加新类目 / 调优先级 | `config.yaml`（README 订阅表格自动更新） |

## 🔧 本地构建

```bash
pip install -r scripts/requirements.txt

python scripts/build.py build --out dist    # 构建所有产物到 dist/
python scripts/build.py lint                # 校验 manual/ 文件
python scripts/build.py readme              # 重新生成上面的订阅表格
python -m unittest discover -s tests        # 跑单元测试
```

## 📝 支持的规则格式

**输入**（`sources` 与 `manual/` 均支持）：

- `DOMAIN,x` / `x` → 完整域名匹配
- `DOMAIN-SUFFIX,x` / `+.x` / `*.x` / `.x` → 域名后缀匹配
- `IP-CIDR,1.1.1.0/24` / `IP-CIDR6,::/0` / `IP-ASN,AS13335` → 单独生成 `final_<类目>_ipcidr.yaml`
- `DOMAIN-KEYWORD` → 忽略；非法通配（如 `*cdn.x`）、裸 IP 等垃圾行会被丢弃并计数

**输出**：完整域名 `example.com`，后缀 `+.example.com`。

## 🔒 稳健性

- **数量闸门**：任一产物较上一版跌幅超过 `max-shrink-percent`（默认 30%）时构建失败、不发布，上一版 `release` 原样留存——某个源挂掉不会发出缩水规则。
- **CI**：`check.yml` 在每次 PR/push 跑 lint + 测试 + 干跑构建；`publish.yml` 每日构建、过闸门后发布到 `release` 分支。

## 🔄 更新机制

- **自动更新**：每天北京时间 05:00（UTC 21:00）构建并发布到 `release` 分支
- **手动触发**：GitHub Actions 页面 dispatch `publish.yml`
- **可回溯**：`release` 分支保留提交历史，可回滚、可用 `@<commit>` 锁定版本

## 📜 开源协议

[MIT License](LICENSE)。

## 🔗 相关链接

- [Clash.Meta / mihomo](https://github.com/MetaCubeX/mihomo)
- 规则源：[Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules)、[SukkaW/Surge](https://github.com/SukkaW/Surge)、[ACL4SSR](https://github.com/ACL4SSR/ACL4SSR)、[AWAvenue-Ads-Rule](https://github.com/TG-Twilight/AWAvenue-Ads-Rule)
