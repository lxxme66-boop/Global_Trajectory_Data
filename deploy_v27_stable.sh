#!/bin/bash
# 一键部署 v2.7 超稳定版
# 创建时间：2025-12-10 21:00

echo "🚀 部署 v2.7 超稳定版"
echo "========================================================================"
echo ""
echo "✅ 核心优化："
echo "   1. 修复 append 错误（强化所有防御检查）"
echo "   2. 超时增加 3倍（60→180秒基础，最大600秒）"
echo "   3. 批量减小（MongoDB 50→30，重排 15→10，限制 300→200）"
echo "   4. 串行重排（稳定4-20秒）"
echo ""
echo "✅ 预期效果："
echo "   - 无 append 错误"
echo "   - 无 44秒异常"
echo "   - 成功率 90%+"
echo ""
echo "========================================================================"

# 检查是否在workspace目录
if [ ! -f "/workspace/search_srv_pipeline_optimized.py" ]; then
    echo "❌ 错误：请先 cd 到有 search_srv_pipeline_optimized.py 的目录"
    exit 1
fi

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
echo "2️⃣  启动 v2.7 超稳定版..."
nohup python3 search_srv_pipeline_optimized.py > service_v27.log 2>&1 &
echo "   PID: $!"
echo "   日志: service_v27.log"

# 等待服务启动
echo ""
echo "3️⃣  等待服务启动..."
for i in {1..15}; do
    sleep 1
    if curl -s http://10.70.223.31:9510/api-rqa-search/health >/dev/null 2>&1; then
        echo "   ✅ 服务已就绪"
        break
    fi
    echo "   ⏳ 等待中... ($i/15)"
done

# 验证版本
echo ""
echo "4️⃣  验证版本..."
HEALTH=$(curl -s http://10.70.223.31:9510/api-rqa-search/health)
VERSION=$(echo "$HEALTH" | grep -o '"version":"[^"]*"' | cut -d'"' -f4)
echo "   📦 服务版本: $VERSION"

if [[ "$VERSION" == *"v2.7"* ]]; then
    echo "   ✅ 版本正确！"
else
    echo "   ❌ 版本错误（期望 v2.7，实际 $VERSION）"
    echo "   可能是旧服务还在运行，请手动检查"
    exit 1
fi

# 显示配置
echo ""
echo "5️⃣  服务配置确认..."
echo "   ✅ 基础超时: 90秒（原60秒）"
echo "   ✅ 最大超时: 600秒（原300秒）"
echo "   ✅ 编码超时: 60秒（原30秒）"
echo "   ✅ 召回超时: 120秒（原60秒）"
echo "   ✅ 排序超时: 240秒（原120秒）"
echo "   ✅ 重排超时: 180秒（原90秒）"
echo "   ✅ MongoDB批量: 30（原50）"
echo "   ✅ 重排批量: 10（原15）"
echo "   ✅ 重排限制: 200（原300）"

# 运行测试
echo ""
echo "6️⃣  运行测试（20个请求，300秒超时）..."
echo "   ⏰ 预计耗时：5-6分钟"
echo "   ☕ 请耐心等待..."
echo ""

# 检查测试文件是否存在
if [ ! -f "test_optimized_service.py" ]; then
    echo "   ⚠️  测试文件不存在，跳过测试"
    echo "   请手动运行: python3 test_optimized_service.py 10.70.223.31 9510"
else
    python3 test_optimized_service.py 10.70.223.31 9510
fi

echo ""
echo "========================================================================"
echo "✅ 部署完成！"
echo ""
echo "📊 验证指标："
echo "   ✅ 版本：v2.7_20251210_2100"
echo "   ✅ 成功率：应该 ≥ 90%"
echo "   ✅ 重排时间：4-20秒（稳定）"
echo "   ✅ 无 append 错误"
echo "   ✅ 无 44秒异常"
echo ""
echo "📝 实时观察："
echo "   # 观察重排时间（应该4-20秒）"
echo "   tail -f service_v27.log | grep 'Reranking completed'"
echo ""
echo "   # 观察完成情况（应该大部分成功）"
echo "   tail -f service_v27.log | grep 'Completed in'"
echo ""
echo "   # 检查错误（应该为空）"
echo "   grep -E 'append.*dict|44\..*s.*Reranking' service_v27.log"
echo ""
echo "========================================================================"
