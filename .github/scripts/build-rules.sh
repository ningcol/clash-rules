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
declare -a RULE_CATEGORIES=("reject" "proxy" "direct" "microsoft")

# ========================================
# 函数: normalize_rules
# 功能: 标准化规则格式，处理多种源格式
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
        if (match(line, /^[[:space:]]*-[[:space:]]+(.+)$/, m)) {
            content = m[1]
            gsub(/^[ \t]+|[ \t]+$/, "", content)
            content = tolower(content)
            
            # 判断是否为 IP-CIDR 格式
            if (content ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\/[0-9]+$/) {
                printf "ip-cidr,%s\n", content
            } else if (content ~ /^[0-9a-f:]+:[0-9a-f:]*\/[0-9]+$/i) {
                # IPv6 CIDR
                printf "ip-cidr6,%s\n", content
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
                # 纯域名格式
                if (content !~ /:/ && content !~ /@/) {
                    printf "domain,%s\n", content
                }
            }
            next
        }
        
        # 处理格式2: 文本格式 (DOMAIN-SUFFIX,domain.com 或 IP-CIDR,1.1.1.0/24)
        if (match(line, /^(DOMAIN|DOMAIN-SUFFIX|DOMAIN-KEYWORD|IP-CIDR|IP-CIDR6|IP-ASN)[[:space:]]*,[[:space:]]*([^,]+)/, m)) {
            rule_type = tolower(m[1])
            rule_value = tolower(m[2])
            
            # 跳过 KEYWORD 规则
            if (rule_type == "domain-keyword") next
            
            printf "%s,%s\n", rule_type, rule_value
            next
        }
        
        # 处理格式3: 纯域名或纯IP行（兜底）
        gsub(/^[ \t]+|[ \t]+$/, "", line)
        line = tolower(line)
        
        # 判断是否为 IP
        if (line ~ /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\/[0-9]+$/) {
            printf "ip-cidr,%s\n", line
        } else if (line ~ /^[0-9a-f:]+:[0-9a-f:]*\/[0-9]+$/i) {
            printf "ip-cidr6,%s\n", line
        } else if (line !~ /:/ && line !~ /@/ && line != "") {
            # 纯域名
            printf "domain,%s\n", line
        }
    }'
}

# ========================================
# 函数: convert_to_domain_format
# 功能: 将标准化规则转换为 Clash domain 格式
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
            
            if command -v yq >/dev/null 2>&1; then
                # 优先使用 yq 解析
                if yq e '.payload[]' rule_content.tmp >> "$output_file" 2>/dev/null; then
                    echo "  -> Parsed with yq successfully"
                else
                    # yq 解析失败，使用原始内容
                    echo "  -> WARNING: yq parse failed, using raw content"
                    cat rule_content.tmp >> "$output_file"
                fi
            else
                # 没有 yq，直接使用原始内容（normalize_rules 会处理）
                echo "  -> No yq found, will parse in normalize step"
                cat rule_content.tmp >> "$output_file"
            fi
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
# 函数: filter_rules
# 功能: 使用排除列表过滤规则
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
        
        # 应用排除列表
        if [ -f "$exclude_file" ] && [ -s "$exclude_file" ]; then
            echo "     Applying exclude list..."
            cat "$exclude_file" | normalize_rules > "$normalized_allowlist" || true
            cat "$normalized_allowlist" | convert_to_domain_format | sort | uniq > temp_exclude_domains.txt || true
            
            grep -Fxv -f temp_exclude_domains.txt "$payload_before_exclude" > "$payload_file" || cp "$payload_before_exclude" "$payload_file"
            rm -f temp_exclude_domains.txt
            
            local after_count=$(wc -l < "$payload_file" | tr -d ' ')
            local excluded_count=$((before_count - after_count))
            echo "     Excluded: $excluded_count domains"
        else
            cp "$payload_before_exclude" "$payload_file"
        fi
        
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
    
    for category in "${RULE_CATEGORIES[@]}"; do
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
}

# 运行主流程
main "$@"
