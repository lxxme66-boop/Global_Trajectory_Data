#!/bin/bash
# 快速重启并测试 v2.4 服务
# 创建时间：2025-12-10 18:00

echo "🔄 重启服务并测试 v2.4"
echo "=========================================="

# 配置
HOST="10.70.223.31"
PORT="9510"
SERVICE_SCRIPT="search_srv_pipeline_optimized.py"

echo ""
echo "1️⃣  停止旧服务..."
pkill -f "$SERVICE_SCRIPT"
sleep 2

echo ""
echo "2️⃣  检查端口..."
if lsof -i :$PORT >/dev/null 2>&1; then
    echo "   ⚠️  端口 $PORT 仍被占用，强制杀死进程..."
    lsof -ti :$PORT | xargs kill -9 2>/dev/null
    sleep 2
fi

echo ""
echo "3️⃣  启动新服务（v2.4）..."
nohup python3 "$SERVICE_SCRIPT" > service_v24.log 2>&1 &
SERVICE_PID=$!

echo "   ✅ 服务已启动，PID: $SERVICE_PID"
echo "   📝 日志文件: service_v24.log"

echo ""
echo "4️⃣  等待服务启动..."
for i in {1..10}; do
    sleep 1
    if curl -s "http://$HOST:$PORT/api-rqa-search/health" >/dev/null 2>&1; then
        echo "   ✅ 服务已就绪！"
        break
    fi
    echo "   ⏳ 等待中... ($i/10)"
done

echo ""
echo "5️⃣  验证服务版本..."
HEALTH=$(curl -s "http://$HOST:$PORT/api-rqa-search/health")
VERSION=$(echo "$HEALTH" | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
echo "   📦 版本: $VERSION"

if [[ "$VERSION" == *"v2.4"* ]]; then
    echo "   ✅ 版本正确！"
else
    echo "   ❌ 版本不对，请检查是否启动了正确的服务"
    exit 1
fi

echo ""
echo "6️⃣  运行测试..."
python3 test_optimized_service.py "$HOST" "$PORT"

echo ""
echo "=========================================="
echo "✅ 测试完成！"
echo ""
echo "📊 查看详细日志:"
echo "   tail -f service_v24.log"
echo ""
echo "📈 查看测试结果分析:"
echo "   cat TEST_RESULTS_ANALYSIS.md"
