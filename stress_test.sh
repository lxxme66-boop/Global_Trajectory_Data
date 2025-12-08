#!/bin/bash
# 搜索服务压力测试脚本

# 配置
URL="http://10.70.223.31:9510/api-rqa-search/search"
CONCURRENT=50  # 并发数
TOTAL=500      # 总请求数

# 测试查询
QUERIES=(
    "显示器技术"
    "半导体制造"
    "芯片设计"
    "处理器架构"
    "内存技术"
    "人工智能算法"
    "机器学习模型"
    "深度学习框架"
    "计算机视觉"
    "自然语言处理"
)

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}🔥 搜索服务压力测试${NC}"
echo -e "${BLUE}========================================${NC}"
echo -e "服务地址: ${URL}"
echo -e "并发数: ${CONCURRENT}"
echo -e "总请求数: ${TOTAL}"
echo -e "查询类型: ${#QUERIES[@]} 种"
echo -e "${BLUE}========================================${NC}\n"

# 清理旧结果
rm -f results.csv
rm -f errors.log

echo "timestamp,request_id,query,duration_ms,status" > results.csv

# 开始时间
START_TIME=$(date +%s)

echo -e "${GREEN}开始测试...${NC}\n"

# 发送请求
SUCCESS=0
FAILED=0

for i in $(seq 1 $TOTAL); do
    # 随机选择查询
    QUERY=${QUERIES[$RANDOM % ${#QUERIES[@]}]}
    
    (
        REQUEST_START=$(date +%s%3N)
        TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
        
        # 发送请求
        RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$URL" \
          -d "query=$QUERY" \
          -d "id=$i" \
          -d "top_doc_num=5" \
          --max-time 120 \
          2>&1)
        
        REQUEST_END=$(date +%s%3N)
        DURATION=$((REQUEST_END - REQUEST_START))
        
        # 解析响应
        HTTP_CODE=$(echo "$RESPONSE" | tail -1)
        
        if [ "$HTTP_CODE" = "200" ]; then
            STATUS="success"
            echo "$TIMESTAMP,$i,$QUERY,$DURATION,$STATUS" >> results.csv
        else
            STATUS="failed"
            echo "$TIMESTAMP,$i,$QUERY,$DURATION,$STATUS" >> results.csv
            echo "[$TIMESTAMP] Request $i failed with HTTP $HTTP_CODE" >> errors.log
        fi
        
        # 进度输出
        if [ $((i % 10)) -eq 0 ]; then
            echo -e "${GREEN}进度: $i / $TOTAL${NC}"
        fi
        
    ) &
    
    # 控制并发数
    if [ $((i % CONCURRENT)) -eq 0 ]; then
        wait
    fi
done

# 等待所有请求完成
wait

END_TIME=$(date +%s)
TOTAL_TIME=$((END_TIME - START_TIME))

echo -e "\n${BLUE}========================================${NC}"
echo -e "${GREEN}✅ 测试完成！${NC}"
echo -e "${BLUE}========================================${NC}\n"

# 统计分析
echo -e "${BLUE}[统计结果]${NC}"

# 成功/失败统计
SUCCESS=$(grep -c "success" results.csv)
FAILED=$(grep -c "failed" results.csv)
SUCCESS_RATE=$(awk "BEGIN {printf \"%.2f\", $SUCCESS / $TOTAL * 100}")

echo -e "  总请求数: ${TOTAL}"
echo -e "  成功: ${GREEN}${SUCCESS}${NC}"
echo -e "  失败: ${RED}${FAILED}${NC}"
echo -e "  成功率: ${GREEN}${SUCCESS_RATE}%${NC}"
echo -e "  总耗时: ${TOTAL_TIME}s"

# 响应时间统计
echo -e "\n${BLUE}[响应时间]${NC}"

AVG=$(awk -F',' 'NR>1 && $5=="success" {sum+=$4; count++} END {printf "%.0f", sum/count}' results.csv)
MIN=$(awk -F',' 'NR>1 && $5=="success" {if(min=="" || $4<min) min=$4} END {print min}' results.csv)
MAX=$(awk -F',' 'NR>1 && $5=="success" {if(max=="" || $4>max) max=$4} END {print max}' results.csv)
P50=$(awk -F',' 'NR>1 && $5=="success" {print $4}' results.csv | sort -n | awk '{a[NR]=$1} END {print a[int(NR*0.5)]}')
P90=$(awk -F',' 'NR>1 && $5=="success" {print $4}' results.csv | sort -n | awk '{a[NR]=$1} END {print a[int(NR*0.9)]}')
P95=$(awk -F',' 'NR>1 && $5=="success" {print $4}' results.csv | sort -n | awk '{a[NR]=$1} END {print a[int(NR*0.95)]}')
P99=$(awk -F',' 'NR>1 && $5=="success" {print $4}' results.csv | sort -n | awk '{a[NR]=$1} END {print a[int(NR*0.99)]}')

echo -e "  平均: ${AVG}ms"
echo -e "  最小: ${MIN}ms"
echo -e "  最大: ${MAX}ms"
echo -e "  P50: ${P50}ms"
echo -e "  P90: ${P90}ms"
echo -e "  P95: ${YELLOW}${P95}ms${NC}"
echo -e "  P99: ${RED}${P99}ms${NC}"

# QPS 统计
QPS=$(awk "BEGIN {printf \"%.2f\", $TOTAL / $TOTAL_TIME}")
echo -e "\n${BLUE}[吞吐量]${NC}"
echo -e "  QPS: ${GREEN}${QPS}${NC} 请求/秒"

# 响应时间分布
echo -e "\n${BLUE}[响应时间分布]${NC}"
UNDER_10=$(awk -F',' 'NR>1 && $5=="success" && $4<10000 {count++} END {print count+0}' results.csv)
UNDER_20=$(awk -F',' 'NR>1 && $5=="success" && $4<20000 {count++} END {print count+0}' results.csv)
UNDER_30=$(awk -F',' 'NR>1 && $5=="success" && $4<30000 {count++} END {print count+0}' results.csv)
UNDER_40=$(awk -F',' 'NR>1 && $5=="success" && $4<40000 {count++} END {print count+0}' results.csv)
OVER_40=$(awk -F',' 'NR>1 && $5=="success" && $4>=40000 {count++} END {print count+0}' results.csv)

echo -e "  < 10s: ${UNDER_10} ($(awk "BEGIN {printf \"%.1f\", $UNDER_10 / $SUCCESS * 100}")%)"
echo -e "  < 20s: ${UNDER_20} ($(awk "BEGIN {printf \"%.1f\", $UNDER_20 / $SUCCESS * 100}")%)"
echo -e "  < 30s: ${UNDER_30} ($(awk "BEGIN {printf \"%.1f\", $UNDER_30 / $SUCCESS * 100}")%)"
echo -e "  < 40s: ${UNDER_40} ($(awk "BEGIN {printf \"%.1f\", $UNDER_40 / $SUCCESS * 100}")%)"
echo -e "  ≥ 40s: ${OVER_40} ($(awk "BEGIN {printf \"%.1f\", $OVER_40 / $SUCCESS * 100}")%)"

# 失败请求
if [ $FAILED -gt 0 ]; then
    echo -e "\n${RED}[失败请求]${NC}"
    echo -e "  查看详情: cat errors.log"
    echo -e "  失败数: ${FAILED}"
fi

# 生成报告
echo -e "\n${BLUE}[报告文件]${NC}"
echo -e "  详细结果: results.csv"
echo -e "  错误日志: errors.log"

echo -e "\n${BLUE}========================================${NC}"

# 生成图表数据（可选）
echo -e "\n${YELLOW}生成图表数据...${NC}"

cat > plot_results.py << 'EOF'
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# 读取数据
df = pd.read_csv('results.csv')
df_success = df[df['status'] == 'success']

# 响应时间分布
plt.figure(figsize=(12, 4))

plt.subplot(1, 3, 1)
plt.hist(df_success['duration_ms'], bins=50, color='skyblue', edgecolor='black')
plt.xlabel('Response Time (ms)')
plt.ylabel('Frequency')
plt.title('Response Time Distribution')

plt.subplot(1, 3, 2)
df_success['duration_ms'].plot(kind='line', color='green')
plt.xlabel('Request ID')
plt.ylabel('Response Time (ms)')
plt.title('Response Time Trend')

plt.subplot(1, 3, 3)
percentiles = [50, 75, 90, 95, 99]
values = [np.percentile(df_success['duration_ms'], p) for p in percentiles]
plt.bar([str(p) for p in percentiles], values, color='orange')
plt.xlabel('Percentile')
plt.ylabel('Response Time (ms)')
plt.title('Response Time Percentiles')

plt.tight_layout()
plt.savefig('stress_test_report.png', dpi=300)
print('图表已保存: stress_test_report.png')
EOF

if command -v python3 &> /dev/null; then
    if python3 -c "import pandas, matplotlib" 2>/dev/null; then
        python3 plot_results.py
        echo -e "${GREEN}✅ 图表生成成功: stress_test_report.png${NC}"
    else
        echo -e "${YELLOW}⚠️  缺少依赖（pandas, matplotlib），跳过图表生成${NC}"
    fi
fi

echo -e "\n${GREEN}测试完成！${NC}"
