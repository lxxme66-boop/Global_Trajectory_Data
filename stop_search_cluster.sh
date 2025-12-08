#!/bin/bash
# 搜索服务集群停止脚本

set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PID_FILE="${SCRIPT_DIR}/search_cluster.pid"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${YELLOW}========================================${NC}"
echo -e "${YELLOW}🛑 停止搜索服务集群${NC}"
echo -e "${YELLOW}========================================${NC}"

# 检查 PID 文件
if [ ! -f "${PID_FILE}" ]; then
    echo -e "${YELLOW}⚠️  PID 文件不存在: ${PID_FILE}${NC}"
    echo -e "${YELLOW}   尝试通过端口查找进程...${NC}"
    
    # 通过端口查找进程
    PORTS=(9511 9512 9513 9514)
    FOUND=0
    
    for port in "${PORTS[@]}"; do
        PID=$(lsof -ti:$port 2>/dev/null || true)
        if [ ! -z "$PID" ]; then
            echo -e "  找到进程 PID=$PID (端口 $port)"
            kill $PID 2>/dev/null || true
            FOUND=1
        fi
    done
    
    if [ $FOUND -eq 0 ]; then
        echo -e "${YELLOW}⚠️  未找到运行中的进程${NC}"
    else
        echo -e "${GREEN}✅ 已停止所有进程${NC}"
    fi
    
    exit 0
fi

# 读取 PID
PIDS=$(cat "${PID_FILE}")
echo -e "从 PID 文件读取: ${PIDS}"

# 停止进程
STOPPED=0
for pid in $PIDS; do
    if ps -p $pid > /dev/null 2>&1; then
        echo -e "  停止进程 PID=${pid}..."
        kill $pid 2>/dev/null || true
        
        # 等待进程退出
        for i in {1..10}; do
            if ! ps -p $pid > /dev/null 2>&1; then
                echo -e "    ${GREEN}✅ 已停止${NC}"
                STOPPED=$((STOPPED+1))
                break
            fi
            sleep 1
        done
        
        # 强制杀死
        if ps -p $pid > /dev/null 2>&1; then
            echo -e "    ${YELLOW}⚠️  强制停止...${NC}"
            kill -9 $pid 2>/dev/null || true
            STOPPED=$((STOPPED+1))
        fi
    else
        echo -e "  进程 PID=${pid} 已不存在"
    fi
done

# 删除 PID 文件
rm -f "${PID_FILE}"

echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}✅ 已停止 ${STOPPED} 个进程${NC}"
echo -e "${GREEN}========================================${NC}"
