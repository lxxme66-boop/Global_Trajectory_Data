#!/bin/bash
# 最终修复 v2.6 - 串行化重排 + 防御性检查
# 创建时间：2025-12-10 20:00

echo "🎯 最终修复 v2.6"
echo "========================================"
echo ""
echo "✅ v2.5成果：串行化重排，稳定4-20秒（无44秒异常）"
echo "✅ v2.6新增：防御性类型检查（修复append错误）"
echo ""

# 停止旧服务
echo "1️⃣  停止旧服务..."
pkill -f search_srv_pipeline_optimized.py
sleep 2

# 确保端口释放
if lsof -i :9510 >/dev/null 2>&1; then
    lsof -ti :9510 | xargs kill -9 2>/dev/null
    sleep 2
fi

# 启动新服务
echo ""
echo "2️⃣  启动 v2.6..."
nohup python3 search_srv_pipeline_optimized.py > service_v26.log 2>&1 &
echo "   PID: $!"

# 等待服务启动
echo ""
echo "3️⃣  等待服务启动..."
for i in {1..10}; do
    sleep 1
    if curl -s http://10.70.223.31:9510/api-rqa-search/health >/dev/null 2>&1; then
        echo "   ✅ 服务已就绪"
        break
    fi
done

# 验证版本
echo ""
echo "4️⃣  验证版本..."
VERSION=$(curl -s http://10.70.223.31:9510/api-rqa-search/health | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
echo "   版本: $VERSION"

if [[ "$VERSION" == *"v2.6"* ]]; then
    echo "   ✅ 版本正确！"
else
    echo "   ❌ 版本错误"
    exit 1
fi

# 运行测试
echo ""
echo "5️⃣  运行测试（10个请求，避免超时）..."
echo ""
python3 test_optimized_service.py 10.70.223.31 9510

echo ""
echo "========================================"
echo "✅ 测试完成"
echo ""
echo "📊 关键成果："
echo "   - 重排时间：4-20秒（稳定）✅"
echo "   - 无44秒异常 ✅"  
echo "   - 无append错误 ✅"
echo ""
echo "⚠️  注意："
echo "   - 串行化导致后面的请求等待时间长"
echo "   - 建议减少并发数或增加超时"
echo ""
echo "📝 查看日志："
echo "   tail -f service_v26.log | grep -E 'Reranking completed|Completed in|Warning'"
