#!/bin/bash
# 检查当前运行的服务

echo "🔍 检查当前运行的搜索服务..."
echo "=================================================================="

# 查找所有运行中的search_srv_pipeline进程
ps aux | grep search_srv_pipeline | grep -v grep | while read line; do
    # 提取进程ID
    pid=$(echo $line | awk '{print $2}')
    # 提取完整命令
    cmd=$(echo $line | awk '{for(i=11;i<=NF;i++) printf $i" "; print ""}')
    
    echo ""
    echo "📌 找到进程 PID: $pid"
    echo "   命令: $cmd"
    
    # 提取文件名
    filename=$(echo $cmd | grep -oP 'search_srv_pipeline[^ ]*\.py' | head -1)
    if [ ! -z "$filename" ]; then
        echo "   文件名: $filename"
        
        # 检查版本
        if echo "$filename" | grep -q "optimized"; then
            echo "   ✅ 这是优化后的版本"
        else
            echo "   ❌ 这是旧版本！需要停止并启动新版本"
        fi
    fi
done

echo ""
echo "=================================================================="
echo ""
echo "📝 建议操作："
echo ""
echo "1️⃣ 停止所有旧版本:"
echo "   pkill -9 -f search_srv_pipeline"
echo ""
echo "2️⃣ 启动优化后的新版本:"
echo "   cd /workspace"
echo "   python3 search_srv_pipeline_optimized.py --port 9510 --host 10.70.223.31"
echo ""
echo "3️⃣ 验证版本:"
echo "   python3 verify_service_version.py"
echo ""
