#!/bin/bash
set -e

# ========================================
# Clash Rules Builder - Modular Version
# Author: ningcol
# 
# 处理流程:
# 1. download_and_merge_rules: 下载源文件，支持两种格式
#    - YAML格式: payload: 开头 + - '+.domain.com' 或 - 'domain.com'
#    - 文本格式: DOMAIN-SUFFIX,domain.com 或 DOMAIN,domain.com
# 
# 2. filter_rules: 标准化规则
#    - 统一转换为: domain,example.com 或 domain-suffix,example.com
# 
# 3. format_and_generate_yaml: 格式化并生成最终文件
#    - 去重 → 转换为最终格式 → 应用排除列表 → 生成YAML
#    - 最终格式: example.com 或 +.example.com
# ========================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# 规则配置列表 - 添加新规则只需在这里添加一行
declare -a RULE_CATEGORIES=("reject" "proxy" "direct" "microsoft" "apple" "icloud")

# 优先级规则配置：定义哪些规则集需要从其他规则集中自动排除
# 格式: ["规则集名称"]="需要排除它的规则集列表(逗号分隔)"
# 排除机制：
#   1. 如果 microsoft/ 目录存在于 RULE_CATEGORIES，会先生成 final_microsoft.yaml
#   2. 处理 proxy/direct 时，自动读取 final_microsoft.yaml 并排除其中的域名
#   3. 如果 final_microsoft.yaml 不存在，跳过排除（不影响构建）
declare -A PRIORITY_RULES=(
    ["microsoft"]="proxy,direct"
    ["apple"]="proxy,direct"
    ["icloud"]="proxy,direct"
    # 未来可以添加更多，例如:
    # ["google"]="proxy,direct"
    # ["cn"]="proxy"
)

# ========================================
# 函数: normalize_rules
# 功能: 标准化规则格式，处理多种源格式
# 说明:
#  - 该函数将各种输入格式统一输出为内置的规范格式，便于后续处理。
#  - 支持三类输入：YAML payload 列表、Clash 文本规则、纯域名/纯 IP 列表。
#  - 输出格式示例：
#      domain,example.com
#      domain-suffix,example.com
#      ip-cidr,1.1.1.0/24
#  - 会自动忽略注释、空行和 DOMAIN-KEYWORD 类型
# 格式1 (YAML): payload: 后跟 - '+.domain.com' 或 - 'domain.com' 或 - '1.1.1.0/24'
# 格式2 (文本): DOMAIN-SUFFIX,domain.com 或 IP-CIDR,1.1.1.0/24
# 输出: domain,example.com | domain-suffix,example.com | ip-cidr,1.1.1.0/24 等
# ========================================
normalize_rules() {
    awk -v q="'" '
    BEGIN{ IGNORECASE=0 }
    {
        # 移除所有单引号
        gsub(q, "")
        
        # 跳过注释和空行
        if ($0 ~ /^[[:space:]]*#/ || $0 ~ /^[[:space:]]*$/) next
        
        # 跳过 payload: 标记行
        if ($0 ~ /^[[:space:]]*payload:[[:space:]]*$/) next
        
        line = $0
        
        # 处理格式1: YAML数组格式 (- '+.domain.com' 或 - '1.1.1.0/24')
        if (line ~ /^[[:space:]]*-[[:space:]]+/) {
            # 提取 - 后面的内容
            sub(/^[[:space:]]*-[[:space:]]+/, "", line)
            content = line
            gsub(/^[ \t]+|[ \t]+$/, "", content)
            content = tolower(content)
            
            # 判断是否为 IP-CIDR 格式 (IPv4)
            if (content ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\/[0-9]+$/) {
                printf "ip-cidr,%s\n", content
            } else if (content ~ /^[0-9a-f]+:[0-9a-f:]+\/[0-9]+$/) {
                # IPv6 CIDR (要求至少有两段，更严格)
                printf "ip-cidr6,%s\n", content
            } else if (content ~ /^[Aa][Ss][0-9]+$/) {
                # ASN 格式 (AS13335, as15169 等)
                printf "ip-asn,%s\n", content
            } else if (content ~ /^\+\./) {
                # +.domain.com 格式 (DOMAIN-SUFFIX)
                sub(/^\+\./, "", content)
                printf "domain-suffix,%s\n", content
            } else if (content ~ /^\*\./) {
                sub(/^\*\./, "", content)
                printf "domain-suffix,%s\n", content
            } else if (content ~ /^\./) {
                sub(/^\./, "", content)
                printf "domain-suffix,%s\n", content
            } else {
                # 纯域名格式（跳过包含冒号或@的行）
                if (index(content, ":") == 0 && index(content, "@") == 0 && content != "") {
                    printf "domain,%s\n", content
                }
            }
            next
        }
        
        # 处理格式2: 文本格式 (DOMAIN-SUFFIX,domain.com 或 IP-CIDR,1.1.1.0/24)
        if (line ~ /^(DOMAIN|DOMAIN-SUFFIX|DOMAIN-KEYWORD|IP-CIDR|IP-CIDR6|IP-ASN)[[:space:]]*,/) {
            # 分离类型和值
            n = split(line, parts, /,/)
            if (n >= 2) {
                rule_type = tolower(parts[1])
                gsub(/^[ \t]+|[ \t]+$/, "", rule_type)
                rule_value = tolower(parts[2])
                gsub(/^[ \t]+|[ \t]+$/, "", rule_value)
                
                # 跳过 KEYWORD 规则
                if (rule_type == "domain-keyword") next
                
                printf "%s,%s\n", rule_type, rule_value
            }
            next
        }
        
        # 处理格式3: 纯域名或纯IP行（兜底）
        gsub(/^[ \t]+|[ \t]+$/, "", line)
        line = tolower(line)
        
        # 判断是否为 IP 或 ASN
        if (line ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\/[0-9]+$/) {
            printf "ip-cidr,%s\n", line
        } else if (line ~ /^[0-9a-f]+:[0-9a-f:]+\/[0-9]+$/) {
            # IPv6 CIDR (更严格的正则)
            printf "ip-cidr6,%s\n", line
        } else if (line ~ /^[Aa][Ss][0-9]+$/) {
            # ASN 格式
            printf "ip-asn,%s\n", line
        } else if (index(line, ":") == 0 && index(line, "@") == 0 && line != "") {
            # 纯域名
            printf "domain,%s\n", line
        }
    }'
}

# ========================================
# 函数: convert_to_domain_format
# 功能: 将标准化规则转换为 Clash domain 格式
# 说明:
#  - 接收 normalize_rules 的输出（如 "domain,example.com" 或 "domain-suffix,example.com"）
#  - 输出符合 Clash `behavior: domain` 的表示：
#      - 完整域名: example.com
#      - 后缀匹配: +.example.com
#  - 会跳过 IP 及 DOMAIN-KEYWORD 类型
# 输入: domain,example.com 或 domain-suffix,example.com
# 输出: example.com 或 +.example.com
# ========================================
convert_to_domain_format() {
    awk -F',' '
    {
        # 默认为 domain 类型
        type = "domain"
        val = $0
        
        # 如果有逗号分隔，提取类型和值
        if (NF > 1) {
            type = tolower($1)
            val = $2
        }
        
        # 去除首尾空格
        gsub(/^[ \t]+|[ \t]+$/, "", val)
        val = tolower(val)
        
        # 跳过 IP 和 KEYWORD 规则
        if (type == "ip-cidr" || type == "ip-cidr6" || type == "domain-keyword") next
        
        # 处理 domain-suffix 类型
        if (type == "domain-suffix") {
            # 移除可能已存在的前缀
            sub(/^\+\./, "", val)
            sub(/^\*\./, "", val)
            sub(/^\./, "", val)
            # 添加 +. 前缀
            printf "+.%s\n", val
        } else {
            # domain 类型，直接输出域名
            print val
        }
    }'
}

# ========================================
# 函数: download_and_merge_rules
# 功能: 下载并合并规则文件，智能识别格式
# 参数: $1 - 规则类型（如 reject, proxy 等）
# 支持格式:
#   1. YAML格式: payload: 开头，包含 - 'domain' 数组
#   2. 文本格式: 纯文本规则列表
# ========================================
download_and_merge_rules() {
    local category=$1
    local category_upper=$(echo "$category" | tr '[:lower:]' '[:upper:]')
    
    echo "==============================================="
    echo "Processing $category_upper rules..."
    echo "==============================================="
    
    local sources_file="$PROJECT_ROOT/$category/sources.list"
    local manual_file="$PROJECT_ROOT/$category/rules.txt"
    local output_file="all_${category}_rules.tmp"
    
    if [ ! -f "$sources_file" ]; then
        echo "Warning: $sources_file not found, skipping..."
        return
    fi
    
    touch "$output_file"
    
    while IFS= read -r url || [[ -n "$url" ]]; do
        if [[ "$url" == \#* ]] || [[ -z "$url" ]]; then continue; fi
        
        echo "  [$(date '+%H:%M:%S')] Downloading: $url"
        
        if ! curl -s -L -o rule_content.tmp "$url"; then
            echo "  -> WARNING: Failed to download"
            continue
        fi
        
        if [ ! -s rule_content.tmp ]; then
            echo "  -> WARNING: File is empty"
            continue
        fi
        
        # 检测文件格式：优先级顺序检测
        # 1. 检测 YAML 格式 (有独立的 payload: 行)
        # 2. 检测文本格式 (以 DOMAIN/IP-CIDR 等关键字开头)
        # 3. 其他格式作为纯域名列表处理
        
        if grep -q -E '^[[:space:]]*payload:[[:space:]]*$' rule_content.tmp; then
            # 格式1: YAML格式 (payload: + - 'domain')
            echo "  -> Detected YAML format (payload:)"
            # 统一使用 normalize_rules 处理，保留完整格式信息
            cat rule_content.tmp >> "$output_file"
        elif grep -q -E '^[[:space:]]*(DOMAIN|DOMAIN-SUFFIX|DOMAIN-KEYWORD|IP-CIDR|IP-CIDR6|IP-ASN)' rule_content.tmp; then
            # 格式2: Clash 文本格式 (DOMAIN-SUFFIX,domain.com 或 IP-CIDR,1.1.1.0/24)
            echo "  -> Detected Clash TEXT format (DOMAIN/IP-CIDR)"
            cat rule_content.tmp >> "$output_file"
        else
            # 格式3: 纯域名/IP列表或其他格式
            echo "  -> Detected plain list format"
            cat rule_content.tmp >> "$output_file"
        fi
    done < "$sources_file"
    
    rm -f rule_content.tmp
    
    if [ -f "$manual_file" ]; then
        cat "$manual_file" >> "$output_file"
        echo "  -> Added manual rules from rules.txt"
    fi
    
    echo "  ✓ Download and merge complete"
}

# ========================================
# 函数: get_priority_exclude_rules
# 功能: 从 final_xxx.yaml 文件读取域名规则用于排除
# 说明:
#  - 优先级规则在 Step 1 已生成 final_<priority>.yaml
#  - 本函数解析该 YAML 并输出 normalize_rules 可识别的规范格式，便于合并到排除列表
# 参数: $1 - 优先级规则类型
# 输出: 规范化格式的域名规则（domain,xxx 或 domain-suffix,xxx）
# ========================================
get_priority_exclude_rules() {
    local priority_category=$1
    local final_yaml="$PROJECT_ROOT/final_${priority_category}.yaml"
    
    # 检查 final_xxx.yaml 是否存在
    if [ ! -f "$final_yaml" ]; then
        return 1
    fi
    
    # 从 YAML 提取域名并转换为规范格式
    awk '
        /^[[:space:]]*-[[:space:]]+/ {
            gsub(/^[[:space:]]*-[[:space:]]+'\''?/, "")
            gsub(/'\''?[[:space:]]*$/, "")
            if ($0 ~ /^\+\./) {
                # +.domain.com -> domain-suffix,domain.com
                sub(/^\+\./, "")
                print "domain-suffix," $0
            } else if ($0 !~ /^#/ && $0 != "" && $0 != "payload:") {
                # domain.com -> domain,domain.com
                print "domain," $0
            }
        }
    ' "$final_yaml"
}

# ========================================
# 函数: should_exclude_from
# 功能: 判断当前规则集是否需要排除某个优先级规则集
# 参数: $1 - 当前规则类型, $2 - 优先级规则类型
# 返回: 0 表示需要排除, 1 表示不需要
# ========================================
should_exclude_from() {
    local current_category=$1
    local priority_category=$2
    
    local exclude_list="${PRIORITY_RULES[$priority_category]}"
    if [ -z "$exclude_list" ]; then
        return 1
    fi
    
    # 检查当前规则是否在排除列表中
    if [[ ",$exclude_list," == *",$current_category,"* ]]; then
        return 0
    fi
    
    return 1
}

# ========================================
# 函数: filter_rules
# 功能: 标准化并生成中间过滤文件
# 说明:
#  - 从 all_<category>_rules.tmp 中读取原始合并内容，调用 normalize_rules
#  - 结果写入 normalized_<category>_list.txt，并复制为 filtered_<category>_list.txt
#  - `filtered_<category>_list.txt` 是后续 format_and_generate_yaml 的输入
# 参数: $1 - 规则类型
# ========================================
filter_rules() {
    local category=$1
    local category_upper=$(echo "$category" | tr '[:lower:]' '[:upper:]')
    
    echo "Normalizing $category_upper rules..."
    
    local input_file="all_${category}_rules.tmp"
    local normalized_list="normalized_${category}_list.txt"
    local filtered_list="filtered_${category}_list.txt"
    
    cat "$input_file" | normalize_rules > "$normalized_list" || true
    echo "  -> Normalized rules ($(wc -l < "$normalized_list") entries)"
    
    cp "$normalized_list" "$filtered_list"
    echo "  ✓ Normalization complete"
}

# ========================================
# 函数: format_and_generate_yaml
# 功能: 格式化规则并生成最终YAML文件，支持 domain 和 ipcidr 分离
# 参数: $1 - 规则类型
# ========================================
format_and_generate_yaml() {
    local category=$1
    local category_upper=$(echo "$category" | tr '[:lower:]' '[:upper:]')
    
    echo "Formatting $category_upper rules..."
    
    local filtered_list="filtered_${category}_list.txt"
    local exclude_file="$PROJECT_ROOT/$category/exclude.txt"
    local normalized_allowlist="normalized_${category}_allowlist.txt"
    
    # 分离 domain 和 IP 规则
    local domain_rules="temp_${category}_domain.txt"
    local ip_rules="temp_${category}_ip.txt"
    
    # 从标准化的规则中分离（移除 domain-keyword）
    grep -E '^(domain|domain-suffix),' "$filtered_list" > "$domain_rules" 2>/dev/null || echo -n > "$domain_rules"
    grep -E '^(ip-cidr|ip-cidr6|ip-asn),' "$filtered_list" > "$ip_rules" 2>/dev/null || echo -n > "$ip_rules"
    
    local domain_count=$(wc -l < "$domain_rules" | tr -d ' ')
    local ip_count=$(wc -l < "$ip_rules" | tr -d ' ')
    
    echo "  -> Separated: $domain_count domain rules, $ip_count IP rules"
    
    # 处理 Domain 规则
    if [ "$domain_count" -gt 0 ]; then
        echo "  -> Processing domain rules..."
        
        local payload_file="final_${category}_payload.txt"
        local payload_before_exclude="final_${category}_payload_before_exclude.txt"
        local yaml_file="$PROJECT_ROOT/final_${category}.yaml"
        
        # 转换为 Clash domain 格式
        cat "$domain_rules" | convert_to_domain_format | sort | uniq > "$payload_before_exclude" || true
        
        local before_count=$(wc -l < "$payload_before_exclude" | tr -d ' ')
        echo "     Converted: $before_count unique domains"
        
        # 创建合并的排除列表
        local combined_exclude="temp_${category}_combined_exclude.txt"
        touch "$combined_exclude"
        
        # 1. 添加本类别的 exclude.txt
        if [ -f "$exclude_file" ] && [ -s "$exclude_file" ]; then
            echo "     Adding exclude.txt to exclusion list..."
            cat "$exclude_file" | normalize_rules >> "$combined_exclude" || true
        fi
        
        # 2. 自动添加优先级规则集的排除
        local excluded_priority_rules=()
        for priority_category in "${!PRIORITY_RULES[@]}"; do
            if should_exclude_from "$category" "$priority_category"; then
                echo "     Auto-excluding $priority_category rules..."
                
                # 使用新函数获取排除规则（支持多种来源）
                if get_priority_exclude_rules "$priority_category" >> "$combined_exclude"; then
                    excluded_priority_rules+=("$priority_category")
                else
                    echo "     Warning: No exclude rules found for $priority_category" >&2
                fi
            fi
        done
        
        # 应用合并后的排除列表
        if [ -s "$combined_exclude" ]; then
            echo "     Applying combined exclude list..."
            cat "$combined_exclude" | convert_to_domain_format | sort | uniq > temp_exclude_domains.txt || true
            
            # 精确匹配：只排除列表中明确指定的规则
            grep -Fxv -f temp_exclude_domains.txt "$payload_before_exclude" > "$payload_file" || cp "$payload_before_exclude" "$payload_file"
            rm -f temp_exclude_domains.txt
            
            local after_count=$(wc -l < "$payload_file" | tr -d ' ')
            local excluded_count=$((before_count - after_count))
            
            if [ "$excluded_count" -gt 0 ]; then
                echo "     Excluded: $excluded_count domains"
                if [ ${#excluded_priority_rules[@]} -gt 0 ]; then
                    echo "       (from priority rules: ${excluded_priority_rules[*]})"
                fi
            fi
        else
            cp "$payload_before_exclude" "$payload_file"
        fi
        
        rm -f "$combined_exclude"
        
        local final_count=$(wc -l < "$payload_file" | tr -d ' ')
        echo "     Final: $final_count unique domains"
        
        # 生成 domain 类型的 YAML
        {
            echo "#########################################"
            echo "# 作者: ningcol"
            echo "# 项目地址: https://github.com/ningcol/clash-rules"
            echo "# 更新时间: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
            echo "# 说明: 本文件为自动生成的 Clash $category_upper 规则（behavior: domain）。"
            echo "#########################################"
            echo "payload:"
            awk '{printf("  - '\''%s'\''\n", $0)}' "$payload_file"
        } > "$yaml_file"
        
        echo "  ✓ Generated $yaml_file ($final_count domains)"
    else
        echo "  ⚠️  No domain rules found"
    fi
    
    # 处理 IP 规则（仅当文本格式源产生 IP 规则时才生成）
    if [ "$ip_count" -gt 0 ]; then
        echo "  -> Processing IP rules..."
        
        local ip_payload_file="final_${category}_ip_payload.txt"
        local yaml_ip_file="$PROJECT_ROOT/final_${category}_ipcidr.yaml"
        
        # 提取 IP 地址（去掉类型前缀）
        awk -F',' '{print $2}' "$ip_rules" | sort | uniq > "$ip_payload_file" || true
        
        local actual_ip_count=$(wc -l < "$ip_payload_file" | tr -d ' ')
        echo "     Deduplicated: $actual_ip_count unique IP entries"
        
        # 生成 ipcidr 类型的 YAML
        {
            echo "#########################################"
            echo "# 作者: ningcol"
            echo "# 项目地址: https://github.com/ningcol/clash-rules"
            echo "# 更新时间: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
            echo "# 说明: 本文件为自动生成的 Clash $category_upper IP规则（behavior: ipcidr）。"
            echo "#########################################"
            echo "payload:"
            awk '{printf("  - '\''%s'\''\n", $0)}' "$ip_payload_file"
        } > "$yaml_ip_file"
        
        echo "  ✓ Generated $yaml_ip_file ($actual_ip_count IPs)"
    else
        echo "  -> No IP rules from text sources, skipping IP-CIDR file"
        # 删除可能存在的旧 IP-CIDR 文件
        local yaml_ip_file="$PROJECT_ROOT/final_${category}_ipcidr.yaml"
        if [ -f "$yaml_ip_file" ]; then
            rm "$yaml_ip_file"
            echo "  ✓ Removed obsolete $yaml_ip_file"
        fi
    fi
    
    echo ""
}

# ========================================
# 函数: cleanup_temp_files
# 功能: 清理临时文件
# 说明: 删除脚本运行过程中创建的临时文件，保持工作目录整洁
# ========================================
cleanup_temp_files() {
    echo "Cleaning up temporary files..."
    rm -f ./*.tmp
    rm -f ./normalized_*.txt
    rm -f ./filtered_*.txt
    rm -f ./final_*_domains.txt
    rm -f ./final_*_payload.txt
    rm -f ./final_*_payload_before_exclude.txt
    rm -f ./temp_*_domain.txt
    rm -f ./temp_*_ip.txt
    rm -f ./final_*_ip_payload.txt
    rm -f ./temp_exclude_domains.txt
    echo "  ✓ Cleanup complete"
}

# ========================================
# 主流程
# ========================================
main() {
    echo "========================================"
    echo "Clash Rules Builder"
    echo "Start time: $(date)"
    echo "========================================"
    echo ""
    
    cd "$PROJECT_ROOT"
    
    # 第一步：先处理所有优先级规则集
    echo "Step 1: Processing priority rules first..."
    for priority_category in "${!PRIORITY_RULES[@]}"; do
        if [[ " ${RULE_CATEGORIES[@]} " =~ " $priority_category " ]]; then
            echo "  -> Processing priority rule: $priority_category"
            download_and_merge_rules "$priority_category"
            filter_rules "$priority_category"
            format_and_generate_yaml "$priority_category"
        fi
    done
    echo ""
    
    # 第二步：处理其他规则集
    echo "Step 2: Processing remaining rules..."
    for category in "${RULE_CATEGORIES[@]}"; do
        # 跳过已处理的优先级规则
        if [[ -n "${PRIORITY_RULES[$category]}" ]]; then
            continue
        fi
        
        download_and_merge_rules "$category"
        filter_rules "$category"
        format_and_generate_yaml "$category"
    done
    
    cleanup_temp_files
    
    echo "========================================"
    echo "Build complete!"
    echo "End time: $(date)"
    echo "========================================"
    
    echo ""
    echo "Generated files:"
    for category in "${RULE_CATEGORIES[@]}"; do
        yaml_file="final_${category}.yaml"
        yaml_ip_file="final_${category}_ipcidr.yaml"
        
        if [ -f "$yaml_file" ]; then
            local line_count=$(wc -l < "$yaml_file")
            local domain_count=$((line_count - 7))
            echo "  - $yaml_file ($domain_count domains)"
        fi
        
        if [ -f "$yaml_ip_file" ]; then
            local line_count=$(wc -l < "$yaml_ip_file")
            local ip_count=$((line_count - 7))
            echo "  - $yaml_ip_file ($ip_count IPs)"
        fi
    done
    
    # 显示优先级规则配置摘要
    echo ""
    echo "Priority rules configuration:"
    for priority_category in "${!PRIORITY_RULES[@]}"; do
        local exclude_list="${PRIORITY_RULES[$priority_category]}"
        echo "  - $priority_category → auto-excluded from: $exclude_list"
    done
}

# 运行主流程
main "$@"
