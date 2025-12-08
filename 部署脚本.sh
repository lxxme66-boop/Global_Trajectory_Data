#!/bin/bash
# MongoDB 超时问题修复 - 部署脚本
# 创建时间: 2025-12-08

set -e  # 遇到错误立即退出

echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║         MongoDB 超时问题修复 - 自动部署脚本                      ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""

# 配置
SERVICE_NAME="search_srv_pipeline_v3_turbo"
NEW_FILE="${SERVICE_NAME}_new.py"
OLD_FILE="${SERVICE_NAME}.py"
PORT=9510
HOST="10.70.223.31"
LOG_FILE="search_${PORT}.log"

# 检查文件是否存在
if [ ! -f "$NEW_FILE" ]; then
    echo "❌ 错误: 找不到文件 $NEW_FILE"
    exit 1
fi

echo "📁 文件检查"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✅ 新文件: $NEW_FILE ($(wc -l < $NEW_FILE) 行)"
if [ -f "$OLD_FILE" ]; then
    echo "  ✅ 旧文件: $OLD_FILE ($(wc -l < $OLD_FILE) 行)"
else
    echo "  ⚠️  旧文件不存在，将创建新文件"
fi
echo ""

# 语法检查
echo "🔍 语法检查"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if python3 -m py_compile "$NEW_FILE" 2>/dev/null; then
    echo "  ✅ 语法检查通过"
else
    echo "  ❌ 语法检查失败"
    exit 1
fi
echo ""

# 询问部署方式
echo "🚀 部署方式选择"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  1. 直接替换原文件（推荐）"
echo "  2. 使用新文件名部署"
echo ""
read -p "请选择 (1/2): " choice

case $choice in
    1)
        echo ""
        echo "📋 方式 1: 直接替换原文件"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        
        # 备份原文件
        if [ -f "$OLD_FILE" ]; then
            BACKUP_FILE="${OLD_FILE}.backup.$(date +%Y%m%d_%H%M%S)"
            echo "  📦 备份原文件: $BACKUP_FILE"
            cp "$OLD_FILE" "$BACKUP_FILE"
        fi
        
        # 替换文件
        echo "  🔄 替换文件: $OLD_FILE"
        cp "$NEW_FILE" "$OLD_FILE"
        
        DEPLOY_FILE="$OLD_FILE"
        ;;
        
    2)
        echo ""
        echo "📋 方式 2: 使用新文件名部署"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        DEPLOY_FILE="$NEW_FILE"
        ;;
        
    *)
        echo "❌ 无效选择"
        exit 1
        ;;
esac
echo ""

# 停止旧服务
echo "🛑 停止旧服务"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if pgrep -f "$SERVICE_NAME.py" > /dev/null; then
    echo "  🔄 找到运行中的服务，正在停止..."
    pkill -f "$SERVICE_NAME.py"
    sleep 2
    
    # 确认已停止
    if pgrep -f "$SERVICE_NAME.py" > /dev/null; then
        echo "  ⚠️  强制停止服务..."
        pkill -9 -f "$SERVICE_NAME.py"
        sleep 1
    fi
    echo "  ✅ 旧服务已停止"
else
    echo "  ℹ️  没有运行中的服务"
fi
echo ""

# 启动新服务
echo "🚀 启动新服务"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📝 命令: nohup python $DEPLOY_FILE --port $PORT --host $HOST > $LOG_FILE 2>&1 &"
nohup python "$DEPLOY_FILE" --port "$PORT" --host "$HOST" > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "  ✅ 新服务已启动 (PID: $NEW_PID)"
echo "  📄 日志文件: $LOG_FILE"
echo ""

# 等待服务启动
echo "⏳ 等待服务启动..."
sleep 5

# 验证服务
echo ""
echo "✅ 验证服务"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 检查进程
if ps -p $NEW_PID > /dev/null; then
    echo "  ✅ 进程运行正常 (PID: $NEW_PID)"
else
    echo "  ❌ 进程已退出，请查看日志: tail -50 $LOG_FILE"
    exit 1
fi

# 检查端口
if lsof -i:$PORT > /dev/null 2>&1; then
    echo "  ✅ 端口监听正常 (Port: $PORT)"
else
    echo "  ⚠️  端口未监听，请稍等或查看日志"
fi

# 测试 API
echo ""
echo "  🔄 测试 API 接口..."
sleep 3

TEST_URL="http://$HOST:$PORT/api-rqa-search/test"
if curl -s "$TEST_URL" > /dev/null 2>&1; then
    RESPONSE=$(curl -s "$TEST_URL")
    echo "  ✅ API 测试成功: $RESPONSE"
else
    echo "  ⚠️  API 暂时无法访问，请稍后再试"
fi
echo ""

# 显示日志
echo "📋 最近的日志"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
tail -20 "$LOG_FILE" | grep -E "MongoDB|Server|Ready|ERROR" || tail -20 "$LOG_FILE"
echo ""

# 监控命令提示
echo "🔍 监控命令"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  # 查看实时日志"
echo "  tail -f $LOG_FILE"
echo ""
echo "  # 查看 MongoDB 相关日志"
echo "  tail -f $LOG_FILE | grep -E 'MongoDB|ERROR|TIMEOUT|KeepAlive'"
echo ""
echo "  # 查看统计信息"
echo "  curl -s http://$HOST:$PORT/api-rqa-search/stats | python -m json.tool"
echo ""
echo "  # 查看超时日志"
echo "  grep 'TIMEOUT' $LOG_FILE | tail -20"
echo ""

# 完成
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║              🎉 部署完成！                                        ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
echo "✅ 验证清单"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  请在接下来的几分钟内验证："
echo ""
echo "  □ 服务启动成功（curl http://$HOST:$PORT/api-rqa-search/test）"
echo "  □ 日志显示 'Connection pool warmed up'"
echo "  □ 日志显示 'KeepAlive ping successful'"
echo "  □ 统计接口返回数据（curl http://$HOST:$PORT/api-rqa-search/stats）"
echo "  □ 成功率 > 98%"
echo ""
echo "📚 相关文档: START_HERE.md, README-最终总结.md"
echo ""
