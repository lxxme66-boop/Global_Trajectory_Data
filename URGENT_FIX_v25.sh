#!/bin/bash
# 紧急修复 v2.5 - 串行化重排，解决44秒卡死问题
# 创建时间：2025-12-10 19:00

echo "🚨 紧急修复：串行化重排阶段"
echo "========================================"
echo ""
echo "问题：重排并发时卡死（44秒）"
echo "方案：添加全局锁，强制串行"
echo ""

# 停止旧服务
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
echo "2️⃣  启动 v2.5（串行重排版）..."
nohup python3 search_srv_pipeline_optimized.py > service_v25_serial.log 2>&1 &
echo "   PID: $!"
echo "   日志: service_v25_serial.log"

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

if [[ "$VERSION" == *"v2.5"* ]]; then
    echo "   ✅ 版本正确！"
else
    echo "   ❌ 版本错误，请检查"
    exit 1
fi

# 运行测试
echo ""
echo "5️⃣  运行测试（观察重排时间是否稳定）..."
echo ""
python3 test_optimized_service.py 10.70.223.31 9510

echo ""
echo "========================================"
echo "✅ 测试完成"
echo ""
echo "📊 关键指标："
echo "   - 重排时间应该稳定在 7-20秒"
echo "   - 不应该再出现 40+ 秒的情况"
echo "   - 成功率应该提升"
echo ""
echo "📝 查看实时日志："
echo "   tail -f service_v25_serial.log | grep Reranking"
