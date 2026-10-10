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
| YOUTUBE | YouTube 网页与视频规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_youtube.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_youtube.yaml) |
| SPEEDTEST | Speedtest 测速服务规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_speedtest.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_speedtest.yaml) |
| CHATGPT | ChatGPT 与 OpenAI 服务规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_chatgpt.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_chatgpt.yaml) |
| MICROSOFT | 微软服务规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_microsoft.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_microsoft.yaml) |
| APPLE | 苹果服务规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_apple.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_apple.yaml) |
| ICLOUD | iCloud 服务规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_icloud.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_icloud.yaml) |
| CLAUDE | Claude 服务规则 | [raw](https://raw.githubusercontent.com/ningcol/clash-rules/release/final_claude.yaml) | [jsDelivr](https://cdn.jsdelivr.net/gh/ningcol/clash-rules@release/final_claude.yaml) |
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
  claude:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_claude.yaml"
    path: ./ruleset/claude.yaml
    interval: 86400
  youtube:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_youtube.yaml"
    path: ./ruleset/youtube.yaml
    interval: 86400
  speedtest:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_speedtest.yaml"
    path: ./ruleset/speedtest.yaml
    interval: 86400
  chatgpt:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/release/final_chatgpt.yaml"
    path: ./ruleset/chatgpt.yaml
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
  - RULE-SET,youtube,PROXY
  - RULE-SET,speedtest,PROXY
  - RULE-SET,chatgpt,PROXY
  - RULE-SET,microsoft,DIRECT
  - RULE-SET,apple,DIRECT
  - RULE-SET,icloud,DIRECT
  - RULE-SET,claude,PROXY
  - RULE-SET,proxy,PROXY
  - RULE-SET,direct,DIRECT
  - MATCH,PROXY
```

> 规则集之间基本互不重叠（同一域名只出现在一个路由类目里），因此绝大多数域名的路由与 RULE-SET 顺序无关。**建议保持上面的顺序**（与构建优先级 `youtube → speedtest → chatgpt → microsoft → apple → icloud → claude → proxy → direct` 一致）：少数域名被上游的广义后缀规则（如 direct 源里的 `+.mi.com`、proxy 源里的共享 CA `+.digicert.com`）覆盖，而 domain 格式无法对后缀做“减一个子域”的裁剪，这些域名依赖此顺序才能路由到正确的策略。

> **jsDelivr 缓存**：`@release` 分支形式的 jsDelivr 链接有约 12 小时 CDN 缓存，push 后最长约半天才刷新；想立即生效可用上面的 raw 链接，或访问一次 `https://purge.jsdelivr.net/gh/ningcol/clash-rules@release/<文件名>` 强制回源。

## 🧩 工作原理

- **划分（partition）**：路由类目（youtube / speedtest / chatgpt / microsoft / apple / icloud / claude / proxy / direct）构成一个划分——每个域名基本只出现在其中一个规则集里，因此路由基本与 RULE-SET 顺序无关。极少数域名因上游广义后缀规则重叠（构建时会报告 conflict、不影响构建），需靠上面推荐的 RULE-SET 顺序消歧。`reject` 是策略叠加层，不参与划分。四个服务类目优先合并专属上游，`manual/` 只补上游尚未收录的已确认域名。
- **优先级 + 手工指派**：域名归属由 `config.yaml` 的 `priority` 顺序决定（前者从后者中排除）；`manual/<类目>.txt` 里的手工指派**优先于**此顺序——写进哪个类目就钉在哪个类目，并自动从其他路由类目移除。
- **单源过滤**：`sources[].exclude` 只排除对应上游的共享或非专属域名，在合并前执行；其他来源的精确主机与手工补充保留。排除无法裁剪上游宽后缀时构建失败，不静默放行。
- **语义去重**：用域名后缀树去重，`+.example.com` 存在时自动压掉其覆盖的所有子域。

## 🛠️ 如何维护

只改两处：`config.yaml`（类目、规则源、优先级、阈值）和 `manual/` 目录。产物 `final_*.yaml` 是生成的，**不要手改**。

| 我想做的事 | 改哪里 |
|-----------|--------|
| 换/加一个上游规则源 | `config.yaml` 对应类目的 `sources` |
| 手工加域名到某类目 | `manual/<类目>.txt`（写清原因+日期） |
| **强制某域名走某策略** | `manual/<目标类目>.txt`（一处，自动从其他类目移除） |
| 从某规则集删域名 | `manual/<类目>-exclude.txt`（**注意下面这条**） |
| 加新类目 / 调优先级 | `config.yaml`（README 订阅表格自动更新） |

> ⚠️ **`-exclude.txt` 不是「删掉但不改路由」。** 排除发生在划分**之前**，所以从高优先级
> 类目里排掉一个域名等于放弃这份所有权 —— 优先级更靠后、同样收录它的类目会接手，
> 流量随之改道。只有优先级**最后**那个类目（当前是 `direct`）排除时才是纯删除，
> 因为它下面没有类目能继承。`lint` 会对非末位类目的 exclude 打一条提示。
>
> 反过来这也是个有用的工具：把 microsoft 上游里那些其实不属于微软的广义后缀
> （Akamai、Azure 的公共地址段）写进 `microsoft-exclude.txt`，就能把它们还给
> proxy / direct。

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

发布前要过五道闸，任何一道不过就整趟不发布，上一版 `release` 原样留存。它们各自盯着一种「产物坏了但看起来很正常」的形态：

| 闸门 | 拦什么 | 为什么单独要一道 |
|---|---|---|
| 数量闸 | 任一产物较上一版跌幅超过 `max-shrink-percent`（默认 30%） | 某个源挂掉时不发出缩水规则 |
| 产物消失闸 | 上一版有、这一版没有的产物 | 创建产物不设闸、销毁产物致命，且这种失败会自我循环，所以配了 `allow-product-removal` 逃生开关 |
| 非法行数闸 | 上游行被判非法丢弃，路由类目**一条都不许** | `+.cn` 是 111516 行里的 1 行 —— 一条规则有多宽和有多少条无关，任何比例阈值都会放行 |
| 裸顶级域闸 | `+.cn` / `+.icbc` 这类单段后缀从上一版消失 | 上面三道量的都是条数，而丢一个顶级域的跌幅是 0.0009%，三道全绿 |
| 逐源闸 | 单个上游源的解析条数塌方（归零一律致命） | 数量闸量的是整个类目，多源类目里死一个源会被其他源盖住；实测 10 个源里 7 个整份变空都不到 8% |

- **显式顶级域移交**：`defaults.tld-transfers` 可单次声明 `{domain: youtube, from: proxy, to: youtube}`。来源类目仅豁免这一个顶级域，且目标构建产物必须完整保留同一个后缀；其他顶级域仍受门禁保护。发布完成后移除声明，`allow-tld-removal` 始终保持 `false`。
- **失败会主动找上门**：闸门拦下时 `publish.yml` 自动开 Issue —— 规则冻结在订阅侧完全不可观测，客户端会继续拉到那份旧规则。另有 `heartbeat.yml` 每周检查 `release` 分支有多久没更新，覆盖「流程根本没跑」的情况。
- **CI**：`check.yml` 在每次 PR/push 跑 lint + 测试 + 干跑构建（拉上一版 release 当基线，让全部闸门在 PR 上也生效）；`publish.yml` 每日构建、过闸门后发布到 `release` 分支。

## 🔄 更新机制

- **自动更新**：每天北京时间 05:00（UTC 21:00）构建并发布到 `release` 分支
- **手动触发**：GitHub Actions 页面 dispatch `publish.yml`
- **可回溯**：`release` 分支保留提交历史，可回滚、可用 `@<commit>` 锁定版本

## 📜 开源协议

[MIT License](LICENSE)。

## 🔗 相关链接

- [Clash.Meta / mihomo](https://github.com/MetaCubeX/mihomo)
- 规则源：[Loyalsoldier/clash-rules](https://github.com/Loyalsoldier/clash-rules)、[SukkaW/Surge](https://github.com/SukkaW/Surge)、[ACL4SSR](https://github.com/ACL4SSR/ACL4SSR)、[MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat)、[blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script)、[AWAvenue-Ads-Rule](https://github.com/TG-Twilight/AWAvenue-Ads-Rule)

## 相关项目

- [cf-optimizer](../cf/README.md)：引用本规则集的订阅生成器；规则分类名称需保持一致。
- [Stash / OpenClash](../../stash/README.md)：客户端加载规则、DNS 与实际出口的排查记录。

## 服务专属分流

| 类目 | 自动跟随的上游 | 本地补充 |
|---|---|---|
| YouTube | MetaCubeX YouTube + ACL4SSR YouTube | 无重复域名清单 |
| Speedtest | MetaCubeX Speedtest + blackmatrix7 Speedtest | 无重复域名清单 |
| ChatGPT | MetaCubeX OpenAI | 官方网络清单中的 `cdn.openaimerge.com` |
| Claude | MetaCubeX Anthropic | 官方已确认的 `+.claude.app`，以及已有的 `+.claude.site` |

每天北京时间 05:00 自动拉取上游并通过发布门禁，客户端仍使用原来的 `final_*.yaml` 地址，每天刷新规则。上游已有的域名不在手工清单重复维护；官方新增、上游尚未收录的专属域名才补入 `manual/`。

YouTube 的共享 `ggpht.com` / `ggpht.cn` 只从 MetaCubeX 单源中排除，ACL4SSR 的 `yt3.ggpht.com` 精确规则保留。`gvt2.com` 继续通过类目排除保留原有路由。ChatGPT 的共享语音命名空间、遥测、未确认的第三方站点及共享认证/支付后缀在单源过滤；保留原有分流，不将整个共享服务交给 ChatGPT。Claude 的 `statsig.anthropic.com` 仍由广告规则优先拦截。
