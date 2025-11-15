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
# 功能: 标准化规则格式
# ========================================
normalize_rules() {
    awk -v q="'" '
    BEGIN{ IGNORECASE=0 }
    {
        gsub(q, "")
        if ($0 ~ /^[[:space:]]*#/ || $0 ~ /^[[:space:]]*$/) next
        line=$0
        if (match(line, /^(DOMAIN|DOMAIN-SUFFIX|DOMAIN-KEYWORD|IP-CIDR|IP-CIDR6)[[:space:]]*,[[:space:]]*([^,]+)/, m)) {
            if (tolower(m[1]) == "domain-keyword") next
            printf "%s,%s\n", tolower(m[1]), tolower(m[2])
        } else {
            sub(/^[[:space:]]*payload:[[:space:]]*/, "", line)
            if (line ~ /\// || line ~ /:/ || line ~ /@/) next
            if (tolower(line) ~ /^domain-keyword[[:space:]]*,/) next
            print tolower(line)
        }
    }'
}

# ========================================
# 函数: convert_to_domain_format
# 功能: 将规则转换为域名格式
# ========================================
convert_to_domain_format() {
    awk -F',' '
    {
        type="domain"; val=$0
        if (NF>1) { type=tolower($1); val=$2 }
        gsub(/^[ \t]+|[ \t]+$/, "", val)
        v=tolower(val)
        if (type=="ip-cidr" || type=="ip-cidr6") next
        if (type=="domain-keyword") next
        if (type=="domain-suffix") {
            sub(/^\+\./, "", v)
            sub(/^\*\./, "", v)
            sub(/^\./, "", v)
            print "+." v
        } else {
            print v
        }
    }'
}

# ========================================
# 函数: download_and_merge_rules
# 功能: 下载并合并规则文件
# 参数: $1 - 规则类型（如 reject, proxy 等）
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
        
        if grep -q -E '^[[:space:]]*payload:' rule_content.tmp; then
            if command -v yq >/dev/null 2>&1; then
                if yq e '.payload[]' rule_content.tmp >> "$output_file" 2>/dev/null; then
                    echo "  -> Parsed as YAML"
                else
                    echo "  -> WARNING: yq parse failed, using raw merge"
                    cat rule_content.tmp >> "$output_file"
                fi
            else
                echo "  -> No yq found, using raw merge"
                cat rule_content.tmp >> "$output_file"
            fi
        else
            cat rule_content.tmp >> "$output_file"
            echo "  -> Parsed as TXT"
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
    
    echo "Filtering $category_upper rules..."
    
    local exclude_file="$PROJECT_ROOT/$category/exclude.txt"
    local input_file="all_${category}_rules.tmp"
    local normalized_allowlist="normalized_${category}_allowlist.txt"
    local normalized_list="normalized_${category}_list.txt"
    local filtered_list="filtered_${category}_list.txt"
    
    if [ -f "$exclude_file" ]; then
        cat "$exclude_file" \
            | sed "s/'//g" \
            | sed -E 's/^\s*#.*//' \
            | sed -E '/^\s*$/d' \
            | tr '[:upper:]' '[:lower:]' \
            | sort | uniq \
            > "$normalized_allowlist"
        echo "  -> Normalized allowlist ($(wc -l < "$normalized_allowlist") entries)"
    else
        touch "$normalized_allowlist"
    fi
    
    cat "$input_file" | normalize_rules > "$normalized_list" || true
    echo "  -> Normalized rules ($(wc -l < "$normalized_list") entries)"
    
    if [ -s "$normalized_allowlist" ]; then
        grep -F -v -x -f "$normalized_allowlist" "$normalized_list" > "$filtered_list" || true
        local removed=$(($(wc -l < "$normalized_list") - $(wc -l < "$filtered_list")))
        echo "  -> Filtered out $removed entries"
    else
        cp "$normalized_list" "$filtered_list"
        echo "  -> No allowlist, skipping filter"
    fi
    
    echo "  ✓ Filtering complete"
}

# ========================================
# 函数: format_and_generate_yaml
# 功能: 格式化规则并生成最终YAML文件
# 参数: $1 - 规则类型
# ========================================
format_and_generate_yaml() {
    local category=$1
    local category_upper=$(echo "$category" | tr '[:lower:]' '[:upper:]')
    
    echo "Formatting $category_upper rules..."
    
    local filtered_list="filtered_${category}_list.txt"
    local domains_file="final_${category}_domains.txt"
    local payload_file="final_${category}_payload.txt"
    local yaml_file="$PROJECT_ROOT/final_${category}.yaml"
    
    cat "$filtered_list" | sort | uniq > "$domains_file" || true
    echo "  -> Deduplicated ($(wc -l < "$domains_file") unique entries)"
    
    cat "$domains_file" | convert_to_domain_format | sort | uniq > "$payload_file" || true
    echo "  -> Converted to domain format ($(wc -l < "$payload_file") domains)"
    
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
    
    echo "  ✓ Generated $yaml_file"
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
        if [ -f "$yaml_file" ]; then
            local line_count=$(wc -l < "$yaml_file")
            local domain_count=$((line_count - 7))
            echo "  - $yaml_file ($domain_count domains)"
        fi
    done
}

# 运行主流程
main "$@"
