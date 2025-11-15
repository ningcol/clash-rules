# Clash Rules

[![Build Status](https://github.com/ningcol/clash-rules/actions/workflows/build-rules.yml/badge.svg)](https://github.com/ningcol/clash-rules/actions)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Update](https://img.shields.io/badge/update-daily-green.svg)](https://github.com/ningcol/clash-rules/actions)

自动化构建的 Clash 规则集，支持多种规则类型，每日自动更新。

## 📋 规则列表

| 规则类型 | 说明 | 订阅链接 |
|---------|------|----------|
| REJECT | 广告拦截规则 | [订阅](https://raw.githubusercontent.com/ningcol/clash-rules/main/final_reject.yaml) |
| PROXY | 代理规则 | [订阅](https://raw.githubusercontent.com/ningcol/clash-rules/main/final_proxy.yaml) |
| DIRECT | 直连规则 | [订阅](https://raw.githubusercontent.com/ningcol/clash-rules/main/final_direct.yaml) |
| MICROSOFT | 微软服务规则 | [订阅](https://raw.githubusercontent.com/ningcol/clash-rules/main/final_microsoft.yaml) |

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

rules:
  - RULE-SET,reject,REJECT
  - RULE-SET,proxy,PROXY
  - RULE-SET,microsoft,DIRECT
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
├── microsoft/                      # MICROSOFT 规则目录
└── final_*.yaml                    # 生成的最终规则文件
```

## 🛠️ 规则目录说明

每个规则目录包含 3 个文件：

### `sources.list` (必需)
远程规则源 URL 列表，支持 YAML 和 TXT 格式。

```
# 示例
https://example.com/rules.yaml
https://another-source.com/rules.txt
```

### `rules.txt` (可选)
手动添加的规则，会在下载远程规则后合并。

```
# 示例
example.com
DOMAIN-SUFFIX,test.com
+.domain.com
```

### `exclude.txt` (可选)
需要从最终规则中排除的域名。

```
# 示例
cdn.example.com
unwanted-domain.com
```

## ➕ 添加新规则类型

以添加 `apple` 规则为例：

### 1. 创建规则目录和文件

```bash
mkdir -p apple
touch apple/sources.list
touch apple/rules.txt
touch apple/exclude.txt
```

### 2. 添加规则源

编辑 `apple/sources.list`：

```
# Apple 官方域名
https://example.com/apple-rules.yaml
https://another-source.com/apple.txt
```

### 3. 更新构建配置

编辑 `.github/scripts/build-rules.sh` 第 13 行：

```bash
declare -a RULE_CATEGORIES=("reject" "proxy" "direct" "microsoft" "apple")
```

### 4. 提交并推送

```bash
git add .
git commit -m "feat: add Apple rules"
git push
```

**完成！** GitHub Actions 会自动构建并生成 `final_apple.yaml`。

## 🔄 更新机制

- **自动更新**: 每天北京时间 11:00 (UTC 03:00) 自动运行
- **手动触发**: 在 GitHub Actions 页面手动触发
- **Push 触发**: 推送代码到 main 分支时自动运行

## 📊 规则处理流程

```
下载远程规则源
        ↓
    合并手动规则
        ↓
    标准化格式
        ↓
    应用排除列表
        ↓
    去重和排序
        ↓
    生成 YAML 文件
        ↓
    提交到仓库
```

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

### 输入格式
- `DOMAIN,example.com`
- `DOMAIN-SUFFIX,example.com`
- `+.example.com`
- `*.example.com`
- `.example.com`
- `example.com` (纯域名)

### 输出格式
所有规则统一转换为 Clash `behavior: domain` 格式：
- 完整域名: `example.com`
- 域名后缀: `+.example.com`

**注意**: IP-CIDR 和 DOMAIN-KEYWORD 规则会被自动过滤。

## 🤝 贡献指南

欢迎提交 Issue 和 Pull Request！

### 提交规则源

如果您有优质的规则源，欢迎通过以下方式贡献：

1. Fork 本仓库
2. 在对应的 `sources.list` 文件中添加规则源 URL
3. 提交 Pull Request

### 报告问题

如发现规则有误或建议改进，请：

1. 在 Issues 中详细描述问题
2. 提供相关域名或规则示例
3. 说明期望的行为

## 📜 开源协议

本项目采用 [MIT License](LICENSE)。

## ⭐ Star History

如果这个项目对您有帮助，请给个 Star ⭐️

## 🔗 相关链接

- [Clash](https://github.com/Dreamacro/clash)
- [Clash.Meta](https://github.com/MetaCubeX/Clash.Meta)
- [yq - YAML 处理工具](https://github.com/mikefarah/yq)

## 📧 联系方式

- **作者**: ningcol
- **项目地址**: https://github.com/ningcol/clash-rules
- **Issues**: https://github.com/ningcol/clash-rules/issues

---

**最后更新**: 2025-11-15  
**自动构建**: [![Build Status](https://github.com/ningcol/clash-rules/actions/workflows/build-rules.yml/badge.svg)](https://github.com/ningcol/clash-rules/actions)
