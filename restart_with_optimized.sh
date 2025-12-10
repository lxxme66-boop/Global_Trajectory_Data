#!/bin/bash
# 一键重启优化后的服务

echo "🔧 一键重启优化后的搜索服务"
echo "=================================================================="

# 1. 停止所有旧服务
echo ""
echo "1️⃣ 停止所有旧服务..."
pkill -9 -f search_srv_pipeline
sleep 2

# 2. 确认已停止
echo ""
echo "2️⃣ 确认进程已停止..."
if ps aux | grep search_srv_pipeline | grep -v grep > /dev/null; then
    echo "   ⚠️  仍有进程在运行，再次尝试停止..."
    pkill -9 -f search_srv_pipeline
    sleep 2
else
    echo "   ✅ 所有进程已停止"
fi

# 3. 启动新服务
echo ""
echo "3️⃣ 启动优化后的新服务..."
cd /workspace

# 后台启动
nohup python3 search_srv_pipeline_optimized.py \
    --port 9510 \
    --host 10.70.223.31 \
    --workers 4 \
    --batch-size 100 \
    > search_service_optimized.log 2>&1 &

NEW_PID=$!
echo "   ✅ 新服务已启动，PID: $NEW_PID"

# 4. 等待服务启动
echo ""
echo "4️⃣ 等待服务启动..."
sleep 5

# 5. 查看启动日志
echo ""
echo "5️⃣ 启动日志（最后20行）:"
echo "=================================================================="
tail -20 search_service_optimized.log
echo "=================================================================="

# 6. 验证版本
echo ""
echo "6️⃣ 验证服务版本..."
sleep 2

python3 /workspace/verify_service_version.py 10.70.223.31 9510

echo ""
echo "=================================================================="
echo "✅ 完成！"
echo ""
echo "📝 后续操作："
echo "   - 查看完整日志: tail -f /workspace/search_service_optimized.log"
echo "   - 运行测试: python3 /workspace/test_optimized_service.py 10.70.223.31 9510"
echo ""
