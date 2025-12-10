#!/bin/bash
# 启动搜索服务（强制实时输出）

echo "🚀 启动搜索服务（强制实时输出模式）"
echo "=================================="

# 设置环境变量，禁用Python输出缓冲
export PYTHONUNBUFFERED=1

# 启动服务
python -u search_srv_pipeline_v3_adaptive_timeout.py \
    --port 9510 \
    --host 10.70.223.31 \
    --workers 4 \
    --request-timeout 300 \
    --debug

# 说明：
# -u: 禁用Python输出缓冲（强制实时输出）
# PYTHONUNBUFFERED=1: 环境变量方式禁用缓冲
# --debug: 启用调试模式，输出更多信息
