#!/bin/bash
# 一键诊断脚本 - 快速确认搜索服务问题

HOST=${1:-10.70.223.31}
PORT=${2:-9510}
BASE_URL="http://${HOST}:${PORT}"

echo "================================"
echo "🔍 快速诊断搜索服务"
echo "================================"
echo "服务地址: $BASE_URL"
echo ""

# 1. 获取当前统计
echo "📊 步骤1: 获取当前统计..."
STATS=$(curl -s "${BASE_URL}/api-rqa-search/stats" 2>/dev/null)

if [ $? -ne 0 ]; then
    echo "❌ 无法连接到服务！"
    exit 1
fi

TOTAL=$(echo "$STATS" | grep -o '"total_requests":[0-9]*' | cut -d':' -f2)
SUCCESS=$(echo "$STATS" | grep -o '"successful_requests":[0-9]*' | cut -d':' -f2)
FAILED=$(echo "$STATS" | grep -o '"failed_requests":[0-9]*' | cut -d':' -f2)
TIMEOUT=$(echo "$STATS" | grep -o '"timeout_requests":[0-9]*' | cut -d':' -f2)

echo "  总请求: $TOTAL"
echo "  成功: $SUCCESS"
echo "  失败: $FAILED"
echo "  超时: $TIMEOUT"

ACCOUNTED=$((SUCCESS + FAILED + TIMEOUT))
UNACCOUNTED=$((TOTAL - ACCOUNTED))

echo "  已统计: $ACCOUNTED"
echo "  未统计: $UNACCOUNTED"
echo ""

# 2. 判断问题
if [ $UNACCOUNTED -gt 0 ]; then
    echo "❌ 发现问题: $UNACCOUNTED 个请求未被正确统计！"
    echo ""
    echo "可能原因："
    echo "  1. 请求在参数校验阶段就被拦截了"
    echo "  2. 监控代码未覆盖所有返回路径"
    echo "  3. 客户端使用了错误的参数传递方式"
    echo ""
    
    if [ $SUCCESS -eq 0 ] || [ $SUCCESS -eq 1 ]; then
        echo "⚠️  几乎没有成功的请求，很可能是参数传递问题！"
        echo ""
    fi
else
    echo "✅ 监控统计正常"
fi

# 3. 测试参数传递
echo "================================"
echo "🧪 步骤2: 测试参数传递方式..."
echo "================================"
echo ""

echo "测试1: 正确方式 (form data)..."
START=$(date +%s.%N)
RESULT1=$(curl -s -X POST "${BASE_URL}/api-rqa-search/search" \
    -d "query=test semiconductor" \
    -d "id=12345" \
    -d "top_doc_num=5" 2>/dev/null)
END=$(date +%s.%N)
ELAPSED1=$(echo "$END - $START" | bc)
CODE1=$(echo "$RESULT1" | grep -o '"code":-\?[0-9]*' | cut -d':' -f2)

echo "  响应码: $CODE1"
echo "  耗时: ${ELAPSED1}秒"

if [ "$CODE1" = "0" ]; then
    echo "  ✅ 请求成功"
elif [ "$CODE1" = "-1" ]; then
    MSG=$(echo "$RESULT1" | grep -o '"msg":"[^"]*"' | cut -d'"' -f4)
    echo "  ❌ 请求失败: $MSG"
fi
echo ""

echo "测试2: 错误方式 (json)..."
START=$(date +%s.%N)
RESULT2=$(curl -s -X POST "${BASE_URL}/api-rqa-search/search" \
    -H "Content-Type: application/json" \
    -d '{"query":"test semiconductor","id":12345,"top_doc_num":5}' 2>/dev/null)
END=$(date +%s.%N)
ELAPSED2=$(echo "$END - $START" | bc)
CODE2=$(echo "$RESULT2" | grep -o '"code":-\?[0-9]*' | cut -d':' -f2)

echo "  响应码: $CODE2"
echo "  耗时: ${ELAPSED2}秒"

if [ "$CODE2" = "0" ]; then
    echo "  ✅ 请求成功"
elif [ "$CODE2" = "-1" ]; then
    MSG=$(echo "$RESULT2" | grep -o '"msg":"[^"]*"' | cut -d'"' -f4)
    echo "  ❌ 请求失败: $MSG"
fi
echo ""

# 4. 分析结果
echo "================================"
echo "💡 诊断结论"
echo "================================"
echo ""

if [ "$CODE1" = "0" ] && [ "$CODE2" = "-1" ]; then
    echo "✅ 服务正常，但只支持 form data"
    echo ""
    echo "客户端修复方法："
    echo "  Python: 使用 data={...} 而不是 json={...}"
    echo "  curl: 使用 -d 'param=value' 而不是 -d '{\"param\":\"value\"}'"
    echo ""
elif [ "$CODE1" = "-1" ] && [ "$CODE2" = "-1" ]; then
    echo "❌ 两种方式都失败，服务端可能有问题"
    echo ""
    echo "服务端检查："
    echo "  1. 编码服务是否正常"
    echo "  2. 数据库连接是否正常"
    echo "  3. 查看服务端日志"
    echo ""
elif [ "$CODE1" = "0" ] && [ "$CODE2" = "0" ]; then
    echo "✅ 服务完全正常，支持两种参数格式"
    echo ""
else
    echo "⚠️  结果异常，需要进一步检查"
    echo ""
fi

# 5. 监控建议
if [ $UNACCOUNTED -gt 0 ]; then
    echo "================================"
    echo "🔧 修复建议"
    echo "================================"
    echo ""
    echo "在搜索接口中使用 try-finally 模式："
    echo ""
    echo "  @app.route('/api-rqa-search/search', methods=['POST'])"
    echo "  def get_data():"
    echo "      req_id = ..."
    echo "      success = False"
    echo "      "
    echo "      MONITOR.start_request(req_id, '')"
    echo "      TIMEOUT_MANAGER.enter_request()"
    echo "      "
    echo "      try:"
    echo "          # 参数校验和搜索逻辑"
    echo "          ..."
    echo "          success = True"
    echo "          return json_result(0, '', data)"
    echo "      except:"
    echo "          return json_result(-1, str(e), None)"
    echo "      finally:"
    echo "          # 无论如何都会执行"
    echo "          MONITOR.end_request(req_id, success=success)"
    echo "          TIMEOUT_MANAGER.exit_request()"
    echo ""
fi

echo "================================"
echo "✅ 诊断完成"
echo "================================"
echo ""
echo "详细修复方案请查看: 修复说明.md"
