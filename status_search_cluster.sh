#!/bin/bash
# 搜索服务集群状态查看脚本

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PID_FILE="${SCRIPT_DIR}/search_cluster.pid"
HOST="10.70.223.31"
PORTS=(9511 9512 9513 9514)

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}========================================${NC}"
echo -e "${BLUE}📊 搜索服务集群状态${NC}"
echo -e "${BLUE}========================================${NC}"

# 检查 PID 文件
if [ -f "${PID_FILE}" ]; then
    PIDS=$(cat "${PID_FILE}")
    echo -e "\n${BLUE}[进程状态]${NC}"
    
    RUNNING=0
    STOPPED=0
    
    for pid in $PIDS; do
        if ps -p $pid > /dev/null 2>&1; then
            echo -e "  PID ${pid}: ${GREEN}✅ 运行中${NC}"
            RUNNING=$((RUNNING+1))
        else
            echo -e "  PID ${pid}: ${RED}❌ 已停止${NC}"
            STOPPED=$((STOPPED+1))
        fi
    done
    
    echo -e "\n  总计: ${GREEN}${RUNNING} 运行${NC} / ${RED}${STOPPED} 停止${NC}"
else
    echo -e "\n${YELLOW}⚠️  PID 文件不存在${NC}"
fi

# 检查端口
echo -e "\n${BLUE}[端口状态]${NC}"
for port in "${PORTS[@]}"; do
    PID=$(lsof -ti:$port 2>/dev/null || true)
    if [ ! -z "$PID" ]; then
        echo -e "  端口 ${port}: ${GREEN}✅ 监听中${NC} (PID=$PID)"
    else
        echo -e "  端口 ${port}: ${RED}❌ 未监听${NC}"
    fi
done

# 健康检查
echo -e "\n${BLUE}[健康检查]${NC}"
for port in "${PORTS[@]}"; do
    RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" "http://${HOST}:${port}/api-rqa-search/test" 2>/dev/null || echo "000")
    if [ "$RESPONSE" = "200" ]; then
        echo -e "  ${HOST}:${port}: ${GREEN}✅ 健康 (HTTP 200)${NC}"
    else
        echo -e "  ${HOST}:${port}: ${RED}❌ 异常 (HTTP ${RESPONSE})${NC}"
    fi
done

# Nginx 状态
echo -e "\n${BLUE}[Nginx 负载均衡]${NC}"
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" "http://${HOST}:9510/api-rqa-search/test" 2>/dev/null || echo "000")
if [ "$RESPONSE" = "200" ]; then
    echo -e "  ${HOST}:9510: ${GREEN}✅ 正常 (HTTP 200)${NC}"
else
    echo -e "  ${HOST}:9510: ${RED}❌ 异常 (HTTP ${RESPONSE})${NC}"
fi

# 获取服务统计
echo -e "\n${BLUE}[服务统计]${NC}"
for port in "${PORTS[@]}"; do
    STATS=$(curl -s "http://${HOST}:${port}/api-rqa-search/stats" 2>/dev/null || echo "{}")
    if [ ! -z "$STATS" ] && [ "$STATS" != "{}" ]; then
        CACHE=$(echo "$STATS" | python3 -c "import sys, json; data=json.load(sys.stdin); print(f\"缓存: {data['data']['encode_cache']['size']}/{data['data']['encode_cache']['hit']}+{data['data']['encode_cache']['miss']} (命中率 {data['data']['encode_cache']['hit_rate']})\")" 2>/dev/null || echo "无法解析")
        echo -e "  端口 ${port}: ${CACHE}"
    fi
done

# MongoDB 连接
echo -e "\n${BLUE}[MongoDB 连接]${NC}"
MONGO_HOST="10.70.223.31:27017"
if timeout 5 bash -c "echo > /dev/tcp/10.70.223.31/27017" 2>/dev/null; then
    echo -e "  ${MONGO_HOST}: ${GREEN}✅ 可连接${NC}"
else
    echo -e "  ${MONGO_HOST}: ${RED}❌ 无法连接${NC}"
fi

# 编码服务
echo -e "\n${BLUE}[编码服务]${NC}"
ENCODER_HOST="8.130.183.20:8031"
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" "http://${ENCODER_HOST}/encode" \
    -H "Content-Type: application/json" \
    -d '{"queries":["test"]}' \
    --max-time 5 2>/dev/null || echo "000")
if [ "$RESPONSE" = "200" ]; then
    echo -e "  ${ENCODER_HOST}: ${GREEN}✅ 正常 (HTTP 200)${NC}"
else
    echo -e "  ${ENCODER_HOST}: ${YELLOW}⚠️  响应: HTTP ${RESPONSE}${NC}"
fi

# Rerank 服务
echo -e "\n${BLUE}[Rerank 服务]${NC}"
RERANKER_HOST="8.130.183.20:8032"
RESPONSE=$(curl -s -o /dev/null -w "%{http_code}" "http://${RERANKER_HOST}/query_bge_reranker/" \
    --max-time 5 2>/dev/null || echo "000")
if [ "$RESPONSE" = "200" ] || [ "$RESPONSE" = "400" ]; then
    echo -e "  ${RERANKER_HOST}: ${GREEN}✅ 运行中 (HTTP ${RESPONSE})${NC}"
else
    echo -e "  ${RERANKER_HOST}: ${YELLOW}⚠️  响应: HTTP ${RESPONSE}${NC}"
fi

# 系统资源
echo -e "\n${BLUE}[系统资源]${NC}"
MEM_TOTAL=$(free -h | awk '/^Mem:/{print $2}')
MEM_USED=$(free -h | awk '/^Mem:/{print $3}')
MEM_PERCENT=$(free | awk '/^Mem:/{printf("%.1f", $3/$2*100)}')
echo -e "  内存: ${MEM_USED} / ${MEM_TOTAL} (${MEM_PERCENT}%)"

CPU_PERCENT=$(top -bn1 | grep "Cpu(s)" | sed "s/.*, *\([0-9.]*\)%* id.*/\1/" | awk '{print 100 - $1}')
echo -e "  CPU: ${CPU_PERCENT}%"

DISK_PERCENT=$(df -h / | awk 'NR==2{print $5}')
echo -e "  磁盘: ${DISK_PERCENT}"

echo -e "\n${BLUE}========================================${NC}"
