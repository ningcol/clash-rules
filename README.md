# Clash Rules

[![Build Status](https://github.com/ningcol/clash-rules/actions/workflows/build-rules.yml/badge.svg)](https://github.com/ningcol/clash-rules/actions)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Update](https://img.shields.io/badge/update-daily-green.svg)](https://github.com/ningcol/clash-rules/actions)

自动化构建的 Clash 规则集，支持多种规则类型，每日自动更新。

## 📋 规则列表

| 规则类型 | 说明 | 订阅链接 | 优先级 |
|---------|------|----------|--------|
| REJECT | 广告拦截规则 | [订阅](https://raw.githubusercontent.com/ningcol/clash-rules/main/final_reject.yaml) | 普通 |
| PROXY | 代理规则 | [订阅](https://raw.githubusercontent.com/ningcol/clash-rules/main/final_proxy.yaml) | 普通 |
| DIRECT | 直连规则 | [订阅](https://raw.githubusercontent.com/ningcol/clash-rules/main/final_direct.yaml) | 普通 |
| MICROSOFT | 微软服务规则 | [订阅](https://raw.githubusercontent.com/ningcol/clash-rules/main/final_microsoft.yaml) | **优先级** |
| APPLE | 苹果服务规则 | [订阅](https://raw.githubusercontent.com/ningcol/clash-rules/main/final_apple.yaml) | **优先级** |

> **优先级规则**: 这些规则会先处理，并自动从其他规则集中排除，避免重复匹配。

## 🚀 快速使用

### 在 Clash 配置中使用

```yaml
rule-providers:
  reject:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/main/final_reject.yaml"
    path: ./ruleset/reject.yaml
    interval: 86400

  proxy:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/main/final_proxy.yaml"
    path: ./ruleset/proxy.yaml
    interval: 86400

  direct:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/main/final_direct.yaml"
    path: ./ruleset/direct.yaml
    interval: 86400

  microsoft:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/main/final_microsoft.yaml"
    path: ./ruleset/microsoft.yaml
    interval: 86400

  apple:
    type: http
    behavior: domain
    url: "https://raw.githubusercontent.com/ningcol/clash-rules/main/final_apple.yaml"
    path: ./ruleset/apple.yaml
    interval: 86400

rules:
  - RULE-SET,reject,REJECT
  - RULE-SET,microsoft,DIRECT  # 优先级规则
  - RULE-SET,apple,DIRECT       # 优先级规则
  - RULE-SET,proxy,PROXY
  - RULE-SET,direct,DIRECT
  - MATCH,PROXY
```

## 📁 项目结构

```
clash-rules/
├── .github/
│   ├── scripts/
│   │   └── build-rules.sh          # 核心构建脚本
│   └── workflows/
│       └── build-rules.yml         # GitHub Actions 工作流
├── reject/                         # REJECT 规则目录
│   ├── sources.list                # 规则源列表
│   ├── rules.txt                   # 手动添加的规则
│   └── exclude.txt                 # 排除列表
├── proxy/                          # PROXY 规则目录
├── direct/                         # DIRECT 规则目录
├── microsoft/                      # MICROSOFT 规则目录（优先级）
├── apple/                          # APPLE 规则目录（优先级）
└── final_*.yaml                    # 生成的最终规则文件
```

## 🎯 优先级规则机制

本项目支持**优先级规则**自动排除功能，避免规则重复匹配：

### 工作原理

```
Step 1: 优先处理优先级规则
  ├─ 处理 microsoft → 生成 final_microsoft.yaml
  └─ 处理 apple → 生成 final_apple.yaml

Step 2: 处理其他规则（自动排除）
  ├─ 处理 proxy
  │   ├─ 下载规则源
  │   ├─ 合并手动规则
  │   ├─ 应用 proxy/exclude.txt（手动排除）
  │   ├─ 自动排除 final_microsoft.yaml 中的域名 ✨
  │   └─ 自动排除 final_apple.yaml 中的域名 ✨
  └─ 处理 direct（同上）
```

### 配置优先级规则

在 `.github/scripts/build-rules.sh` 中配置：

```bash
# 定义哪些规则集需要被其他规则排除
declare -A PRIORITY_RULES=(
    ["microsoft"]="proxy,direct"  # microsoft 会从 proxy 和 direct 中排除
    ["apple"]="proxy,direct"      # apple 会从 proxy 和 direct 中排除
    # 未来可以添加更多：
    # ["google"]="proxy,direct"
    # ["cn"]="proxy"
)
```

### 优势

- ✅ **自动排除**：无需手动维护 exclude.txt 中的重复域名
- ✅ **灵活配置**：可以指定任意规则集作为优先级规则
- ✅ **向后兼容**：原有的 exclude.txt 仍然有效
- ✅ **健壮容错**：如果优先级规则不存在，跳过不影响构建

## 🛠️ 规则目录说明

每个规则目录包含 3 个文件：

### `sources.list` (必需)
远程规则源 URL 列表，支持 YAML 和 TXT 格式。

```
# 示例
https://example.com/rules.yaml
https://another-source.com/rules.txt
# 支持注释
```

**支持的源格式：**
- ✅ YAML 格式（`payload:` 开头）
- ✅ Clash 文本格式（`DOMAIN-SUFFIX,domain.com`）
- ✅ 纯域名列表（每行一个域名）
- ✅ IP-CIDR 规则（会单独生成 `final_*_ipcidr.yaml`）

### `rules.txt` (可选)
手动添加的规则，会在下载远程规则后合并。

```
# 示例 - 支持多种格式
example.com
DOMAIN-SUFFIX,test.com
+.domain.com
*.wildcard.com
IP-CIDR,192.168.0.0/16
```

### `exclude.txt` (可选)
需要从最终规则中排除的域名（**手动排除**）。

```
# 示例
cdn.example.com
unwanted-domain.com
+.exclude-suffix.com
```

> **💡 提示**: 如果使用了优先级规则机制，优先级规则会**自动排除**，无需在 `exclude.txt` 中重复添加。

## ➕ 添加新规则类型

### 方式一：添加普通规则

以添加 `cdn` 规则为例：

#### 1. 创建规则目录和文件

```bash
mkdir -p cdn
touch cdn/sources.list
touch cdn/rules.txt
touch cdn/exclude.txt
```

#### 2. 添加规则源

编辑 `cdn/sources.list`：

```
# CDN 相关规则
https://example.com/cdn-rules.yaml
```

#### 3. 更新构建配置

编辑 `.github/scripts/build-rules.sh` 第 23 行：

```bash
declare -a RULE_CATEGORIES=("reject" "proxy" "direct" "microsoft" "apple" "cdn")
```

#### 4. 提交并推送

```bash
git add .
git commit -m "feat: add CDN rules"
git push
```

### 方式二：添加优先级规则

如果新规则需要**自动排除**功能（如 Google、国内网站等）：

#### 额外步骤：配置优先级

编辑 `.github/scripts/build-rules.sh` 第 28-35 行：

```bash
declare -A PRIORITY_RULES=(
    ["microsoft"]="proxy,direct"
    ["apple"]="proxy,direct"
    ["google"]="proxy,direct"  # 新增：google 会从 proxy 和 direct 中排除
)
```

**完成！** GitHub Actions 会自动构建并生成相应的 YAML 文件。

### 注意事项

⚠️ **优先级规则之间不能互相排除**  
- 如果配置 `["apple"]="microsoft,direct"`，apple **无法**排除 microsoft（它们在同一批次处理）
- 如需此功能，请在 `apple/exclude.txt` 中手动添加 microsoft 的域名

⚠️ **文件夹不存在时的行为**  
- 如果 `RULE_CATEGORIES` 中定义了 `cdn`，但没有 `cdn/` 目录，脚本会跳过并继续
- 如果配置了优先级规则 `["google"]="proxy"`，但 `final_google.yaml` 不存在，排除功能会跳过

✅ **推荐做法**  
- 普通规则：直接添加到 `RULE_CATEGORIES`
- 需要避免重复的规则：配置为优先级规则

## 🔄 更新机制

- **自动更新**: 每天北京时间 11:00 (UTC 03:00) 自动运行
- **手动触发**: 在 GitHub Actions 页面手动触发
- **Push 触发**: 推送代码到 main 分支时自动运行

## 📊 规则处理流程

### 完整流程

```
┌─────────────────────────────────┐
│  Step 1: 优先级规则处理         │
├─────────────────────────────────┤
│  ├─ 下载 microsoft 规则源       │
│  ├─ 合并 + 标准化               │
│  ├─ 应用 microsoft/exclude.txt  │
│  ├─ 去重 + 生成 final_microsoft.yaml │
│  └─ 同理处理 apple 规则         │
└─────────────────────────────────┘
         ↓
┌─────────────────────────────────┐
│  Step 2: 其他规则处理           │
├─────────────────────────────────┤
│  ├─ 下载 proxy 规则源           │
│  ├─ 合并 proxy/rules.txt        │
│  ├─ 标准化格式                  │
│  ├─ 应用 proxy/exclude.txt      │ ← 手动排除
│  ├─ 自动排除 final_microsoft.yaml│ ← 自动排除
│  ├─ 自动排除 final_apple.yaml   │ ← 自动排除
│  ├─ 去重 + 排序                 │
│  └─ 生成 final_proxy.yaml       │
│                                  │
│  └─ 同理处理 direct、reject      │
└─────────────────────────────────┘
         ↓
┌─────────────────────────────────┐
│  Step 3: 清理临时文件           │
└─────────────────────────────────┘
```

### 规则格式转换

| 输入格式 | 标准化格式 | 最终输出 |
|---------|-----------|----------|
| `DOMAIN-SUFFIX,example.com` | `domain-suffix,example.com` | `+.example.com` |
| `+.example.com` | `domain-suffix,example.com` | `+.example.com` |
| `*.example.com` | `domain-suffix,example.com` | `+.example.com` |
| `DOMAIN,test.com` | `domain,test.com` | `test.com` |
| `test.com` | `domain,test.com` | `test.com` |
| `IP-CIDR,1.1.1.0/24` | `ip-cidr,1.1.1.0/24` | 单独生成 ipcidr 文件 |

> **注意**: `DOMAIN-KEYWORD` 规则会被自动过滤，不会出现在最终规则中。

## 🔧 本地构建

```bash
# 克隆仓库
git clone https://github.com/ningcol/clash-rules.git
cd clash-rules

# 安装 yq (可选，用于解析 YAML)
# macOS
brew install yq

# Ubuntu/Debian
sudo wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/bin/yq
sudo chmod +x /usr/bin/yq

# 运行构建脚本
chmod +x .github/scripts/build-rules.sh
./.github/scripts/build-rules.sh

# 查看生成的文件
ls -lh final_*.yaml
```

## 📝 规则格式支持

### 支持的输入格式

#### 域名规则
- `DOMAIN,example.com` → 完整域名匹配
- `DOMAIN-SUFFIX,example.com` → 域名后缀匹配
- `+.example.com` → 域名后缀匹配（YAML 格式）
- `*.example.com` → 域名后缀匹配
- `.example.com` → 域名后缀匹配
- `example.com` → 纯域名（自动识别为完整域名）

#### IP 规则（会生成单独的 ipcidr 文件）
- `IP-CIDR,192.168.0.0/16` → IPv4 CIDR
- `IP-CIDR6,2001:db8::/32` → IPv6 CIDR
- `IP-ASN,AS13335` → ASN 号码

#### 不支持的格式
- ❌ `DOMAIN-KEYWORD` - 会被自动过滤

### 输出格式

所有域名规则统一转换为 Clash `behavior: domain` 格式：

- **完整域名**: `example.com`
- **域名后缀**: `+.example.com`

IP 规则会单独生成 `behavior: ipcidr` 格式：

- **IPv4**: `192.168.0.0/16`
- **IPv6**: `2001:db8::/32`
- **ASN**: `AS13335`

### 文件命名规则

- 域名规则: `final_<category>.yaml`
- IP 规则: `final_<category>_ipcidr.yaml`

例如：
- `final_proxy.yaml` - proxy 的域名规则
- `final_proxy_ipcidr.yaml` - proxy 的 IP 规则（如果有）

## 🤝 贡献指南

欢迎提交 Issue 和 Pull Request！

### 提交规则源

如果您有优质的规则源，欢迎通过以下方式贡献：

1. Fork 本仓库
2. 在对应的 `sources.list` 文件中添加规则源 URL
3. 确保规则源格式正确（支持 YAML/TXT 格式）
4. 提交 Pull Request，并说明规则源的用途

### 报告问题

如发现规则有误或建议改进，请：

1. 在 Issues 中详细描述问题
2. 提供相关域名或规则示例
3. 说明期望的行为
4. 如果是排除问题，说明是否应该使用优先级规则

### 开发建议

- **测试本地构建**: 提交前请在本地运行 `build-rules.sh` 确保脚本正常工作
- **检查日志输出**: 关注构建日志中的警告信息
- **验证生成文件**: 检查生成的 `final_*.yaml` 文件格式是否正确

### 常见问题

**Q: 如何验证规则是否生效？**  
A: 查看生成的 `final_*.yaml` 文件，或在 GitHub Actions 日志中查看处理结果。

**Q: 优先级规则不生效怎么办？**  
A: 确保：
1. 优先级规则已在 `RULE_CATEGORIES` 中定义
2. 优先级规则目录存在且 `sources.list` 有效
3. `PRIORITY_RULES` 配置正确
4. 查看构建日志中的 "Priority rules configuration" 部分

**Q: 如何临时禁用某个规则源？**  
A: 在 `sources.list` 中将对应行注释（添加 `#` 前缀）。

## 📜 开源协议

本项目采用 [MIT License](LICENSE)。

## ⭐ Star History

如果这个项目对您有帮助，请给个 Star ⭐️

## 🔗 相关链接

- [Clash](https://github.com/Dreamacro/clash)
- [Clash.Meta](https://github.com/MetaCubeX/Clash.Meta)
- [Clash Verge](https://github.com/clash-verge-rev/clash-verge-rev)

## 💡 高级用法

### 自定义优先级规则顺序

虽然优先级规则之间无法互相排除，但可以通过配置实现分层：

```bash
# 场景：需要 apple 排除 microsoft
# 方式1：将 apple 从优先级规则移除，作为普通规则处理
RULE_CATEGORIES=("reject" "proxy" "direct" "microsoft" "apple")
PRIORITY_RULES=(["microsoft"]="proxy,direct,apple")

# 方式2：在 apple/exclude.txt 中手动添加 microsoft 域名
```

### 使用已生成的规则作为排除源

如果您的项目中没有某些规则的源，但想排除它们：

```bash
# 1. 确保 RULE_CATEGORIES 中不包含该规则（如 google）
RULE_CATEGORIES=("reject" "proxy" "direct")

# 2. 配置优先级规则（即使本地没有 google/）
PRIORITY_RULES=(["google"]="proxy,direct")

# 3. 手动将 final_google.yaml 放到项目根目录
# 脚本会自动读取它并用于排除
```

## 📧 联系方式

- **作者**: ningcol
- **项目地址**: https://github.com/ningcol/clash-rules
- **Issues**: https://github.com/ningcol/clash-rules/issues

---

**最后更新**: 2025-11-15  
**自动构建**: [![Build Status](https://github.com/ningcol/clash-rules/actions/workflows/build-rules.yml/badge.svg)](https://github.com/ningcol/clash-rules/actions)
