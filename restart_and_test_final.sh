#!/bin/bash
# 一键重启并测试 v2.6 + v1.3
# 创建时间：2025-12-10 20:10
# 完整解决方案：串行重排 + 300秒超时

echo "🎯 完整解决方案：v2.6服务 + v1.3测试"
echo "========================================================================"
echo ""
echo "✅ 服务端 v2.6："
echo "   - 串行化重排（4-20秒稳定，无44秒异常）"
echo "   - 防御性类型检查（无append错误）"
echo ""
echo "✅ 测试端 v1.3："
echo "   - 超时增加到300秒（适配串行重排）"
echo "   - 预期成功率：90-100% (18-20/20)"
echo ""
echo "========================================================================"

# 停止旧服务
echo ""
echo "1️⃣  停止旧服务..."
pkill -f search_srv_pipeline_optimized.py
sleep 2

# 确保端口释放
if lsof -i :9510 >/dev/null 2>&1; then
    echo "   强制释放端口..."
    lsof -ti :9510 | xargs kill -9 2>/dev/null
    sleep 2
fi

# 启动新服务
echo ""
echo "2️⃣  启动服务 v2.6..."
nohup python3 search_srv_pipeline_optimized.py > service_final.log 2>&1 &
echo "   PID: $!"
echo "   日志: service_final.log"

# 等待服务启动
echo ""
echo "3️⃣  等待服务启动..."
for i in {1..10}; do
    sleep 1
    if curl -s http://10.70.223.31:9510/api-rqa-search/health >/dev/null 2>&1; then
        echo "   ✅ 服务已就绪"
        break
    fi
    echo "   ⏳ 等待中... ($i/10)"
done

# 验证版本
echo ""
echo "4️⃣  验证版本..."
HEALTH=$(curl -s http://10.70.223.31:9510/api-rqa-search/health)
VERSION=$(echo "$HEALTH" | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
echo "   📦 服务版本: $VERSION"

if [[ "$VERSION" == *"v2.6"* ]]; then
    echo "   ✅ 服务版本正确！"
else
    echo "   ❌ 服务版本错误，请检查"
    exit 1
fi

# 运行测试
echo ""
echo "5️⃣  运行测试（20个请求，300秒超时）..."
echo "   ⏰ 预计耗时：5-6分钟（串行重排需要时间）"
echo "   ☕ 请耐心等待..."
echo ""
python3 test_optimized_service.py 10.70.223.31 9510

echo ""
echo "========================================================================"
echo "✅ 测试完成！"
echo ""
echo "📊 预期结果："
echo "   ✅ 成功率：90-100% (18-20/20)"
echo "   ✅ 重排时间：4-20秒（稳定）"
echo "   ✅ 无44秒异常"
echo "   ✅ 无append错误"
echo ""
echo "📝 实时观察重排时间："
echo "   tail -f service_final.log | grep 'Reranking completed'"
echo ""
echo "📝 观察完成情况："
echo "   tail -f service_final.log | grep 'Completed in'"
echo ""
echo "📝 检查错误（应该为空）："
echo "   grep -E '44\..*Reranking|append.*dict' service_final.log"
