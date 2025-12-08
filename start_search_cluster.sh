#!/bin/bash
# 搜索服务集群启动脚本
# 用途：启动 4 个搜索进程 + Nginx 负载均衡

set -e  # 遇到错误立即退出

# 配置
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PYTHON_SCRIPT="${SCRIPT_DIR}/search_srv_pipeline_v3_turbo.py"
NGINX_CONF="${SCRIPT_DIR}/nginx_search.conf"
LOG_DIR="${SCRIPT_DIR}/logs"
PID_FILE="${SCRIPT_DIR}/search_cluster.pid"

# 端口配置
PORTS=(9511 9512 9513 9514)
HOST="10.70.223.31"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}🚀 搜索服务集群启动脚本${NC}"
echo -e "${GREEN}========================================${NC}"

# 创建日志目录
mkdir -p "${LOG_DIR}"

# 检查 Python 脚本是否存在
if [ ! -f "${PYTHON_SCRIPT}" ]; then
    echo -e "${RED}❌ 错误：找不到 Python 脚本: ${PYTHON_SCRIPT}${NC}"
    exit 1
fi

# 检查是否已经运行
if [ -f "${PID_FILE}" ]; then
    echo -e "${YELLOW}⚠️  检测到服务可能已在运行${NC}"
    echo -e "${YELLOW}   PID 文件: ${PID_FILE}${NC}"
    echo -e "${YELLOW}   是否要停止并重启？(y/n)${NC}"
    read -r answer
    if [ "$answer" = "y" ]; then
        echo -e "${YELLOW}停止现有服务...${NC}"
        "${SCRIPT_DIR}/stop_search_cluster.sh"
        sleep 2
    else
        echo -e "${RED}取消启动${NC}"
        exit 1
    fi
fi

# 启动搜索进程
echo -e "${GREEN}[1/3] 启动搜索进程...${NC}"
PIDS=()

for port in "${PORTS[@]}"; do
    LOG_FILE="${LOG_DIR}/search_${port}.log"
    echo -e "  启动进程: ${HOST}:${port}"
    
    nohup python "${PYTHON_SCRIPT}" --host "${HOST}" --port "${port}" \
        > "${LOG_FILE}" 2>&1 &
    
    pid=$!
    PIDS+=($pid)
    echo -e "    进程 PID: ${pid}"
    
    # 等待进程启动
    sleep 2
    
    # 检查进程是否成功启动
    if ps -p $pid > /dev/null; then
        echo -e "    ${GREEN}✅ 启动成功${NC}"
    else
        echo -e "    ${RED}❌ 启动失败，查看日志: ${LOG_FILE}${NC}"
        # 清理已启动的进程
        for p in "${PIDS[@]}"; do
            kill $p 2>/dev/null || true
        done
        exit 1
    fi
done

# 保存 PID
echo "${PIDS[@]}" > "${PID_FILE}"
echo -e "${GREEN}✅ 所有搜索进程已启动${NC}"

# 配置 Nginx
echo -e "\n${GREEN}[2/3] 配置 Nginx 负载均衡...${NC}"

# 检查 Nginx 是否安装
if ! command -v nginx &> /dev/null; then
    echo -e "${RED}❌ 错误：Nginx 未安装${NC}"
    echo -e "${YELLOW}   请先安装 Nginx：sudo apt-get install nginx${NC}"
    exit 1
fi

# 复制 Nginx 配置
NGINX_CONF_TARGET="/etc/nginx/conf.d/search.conf"
echo -e "  复制配置文件..."
sudo cp "${NGINX_CONF}" "${NGINX_CONF_TARGET}"

# 测试 Nginx 配置
echo -e "  测试 Nginx 配置..."
if sudo nginx -t; then
    echo -e "  ${GREEN}✅ Nginx 配置正确${NC}"
else
    echo -e "  ${RED}❌ Nginx 配置错误${NC}"
    exit 1
fi

# 重载 Nginx
echo -e "  重载 Nginx..."
sudo nginx -s reload
echo -e "${GREEN}✅ Nginx 已重载${NC}"

# 健康检查
echo -e "\n${GREEN}[3/3] 健康检查...${NC}"
sleep 3

for port in "${PORTS[@]}"; do
    echo -e "  检查 ${HOST}:${port}..."
    
    if curl -s -f "http://${HOST}:${port}/api-rqa-search/test" > /dev/null; then
        echo -e "    ${GREEN}✅ 健康${NC}"
    else
        echo -e "    ${YELLOW}⚠️  暂未响应（可能仍在启动中）${NC}"
    fi
done

echo -e "\n  检查 Nginx 负载均衡（${HOST}:9510）..."
if curl -s -f "http://${HOST}:9510/api-rqa-search/test" > /dev/null; then
    echo -e "    ${GREEN}✅ 负载均衡正常${NC}"
else
    echo -e "    ${RED}❌ 负载均衡异常${NC}"
fi

# 显示状态
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}✅ 搜索服务集群启动完成！${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "\n服务地址："
echo -e "  负载均衡入口: ${GREEN}http://${HOST}:9510/api-rqa-search/search${NC}"
echo -e "  后端进程:"
for port in "${PORTS[@]}"; do
    echo -e "    - http://${HOST}:${port}"
done

echo -e "\n日志文件："
for port in "${PORTS[@]}"; do
    echo -e "  - ${LOG_DIR}/search_${port}.log"
done

echo -e "\n管理命令："
echo -e "  查看状态: ${YELLOW}${SCRIPT_DIR}/status_search_cluster.sh${NC}"
echo -e "  停止服务: ${YELLOW}${SCRIPT_DIR}/stop_search_cluster.sh${NC}"
echo -e "  查看日志: ${YELLOW}tail -f ${LOG_DIR}/search_9511.log${NC}"

echo -e "\n${GREEN}========================================${NC}"
