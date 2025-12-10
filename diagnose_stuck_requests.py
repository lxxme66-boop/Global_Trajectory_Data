#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
诊断卡住的请求
"""

import requests
import json
import sys

def diagnose_service(host='10.70.223.31', port=9510):
    """诊断服务状态"""
    
    base_url = f'http://{host}:{port}'
    
    print(f'🔍 诊断服务: {base_url}')
    print('=' * 80)
    
    # 1. 健康检查
    print('\n1️⃣ 健康检查...')
    try:
        response = requests.get(f'{base_url}/api-rqa-search/health', timeout=5)
        health = response.json()
        print(f'   状态: {health.get("data", {}).get("status", "unknown")}')
        print(f'   运行时间: {health.get("data", {}).get("uptime", 0):.1f}秒')
    except Exception as e:
        print(f'   ❌ 健康检查失败: {e}')
    
    # 2. 获取统计信息
    print('\n2️⃣ 服务统计...')
    try:
        response = requests.get(f'{base_url}/api-rqa-search/stats', timeout=5)
        stats = response.json().get('data', {})
        
        print(f'   总请求数: {stats.get("total_requests", 0)}')
        print(f'   成功: {stats.get("successful_requests", 0)}')
        print(f'   失败: {stats.get("failed_requests", 0)}')
        print(f'   超时: {stats.get("timeout_requests", 0)}')
        print(f'   活跃请求: {stats.get("active_requests", 0)}')
        print(f'   HTTP会话数: {stats.get("http_sessions", 0)}')
        
        # 3. 活跃请求详情
        active_requests = stats.get('active_requests_detail', [])
        if active_requests:
            print(f'\n3️⃣ 活跃请求详情 ({len(active_requests)}个):')
            for req in active_requests:
                print(f'   - ID: {req["id"]}')
                print(f'     阶段: {req["stage"]}')
                print(f'     持续时间: {req["duration"]:.1f}秒')
                print(f'     查询: {req["query"]}')
                print()
        else:
            print(f'\n3️⃣ 当前没有活跃请求')
        
        # 4. 阶段性能统计
        stage_times = stats.get('stage_times', {})
        if stage_times:
            print(f'\n4️⃣ 阶段性能统计:')
            for stage, time_stats in stage_times.items():
                if isinstance(time_stats, dict):
                    print(f'   {stage}:')
                    print(f'     - 调用次数: {time_stats.get("count", 0)}')
                    print(f'     - 平均耗时: {time_stats.get("avg", 0):.2f}秒')
                    print(f'     - P95耗时: {time_stats.get("p95", 0):.2f}秒')
        
        # 5. 缓存统计
        cache_stats = stats.get('encode_cache', {})
        if cache_stats:
            print(f'\n5️⃣ 编码缓存:')
            print(f'   大小: {cache_stats.get("size", 0)}')
            print(f'   命中: {cache_stats.get("hits", 0)}')
            print(f'   未命中: {cache_stats.get("misses", 0)}')
            print(f'   命中率: {cache_stats.get("hit_rate", "0%")}')
        
        # 6. 分析问题
        print(f'\n6️⃣ 问题分析:')
        active_count = stats.get("active_requests", 0)
        timeout_count = stats.get("timeout_requests", 0)
        
        if active_count > 0:
            print(f'   ⚠️  有 {active_count} 个请求正在处理中')
            if active_requests:
                max_duration = max(req['duration'] for req in active_requests)
                if max_duration > 300:  # 5分钟
                    print(f'   ⚠️  最长请求已运行 {max_duration:.1f}秒 (>5分钟)')
                if max_duration > 600:  # 10分钟
                    print(f'   🚨 最长请求已运行 {max_duration:.1f}秒 (>10分钟) - 可能卡住了!')
        
        if timeout_count > 0:
            timeout_rate = timeout_count / stats.get("total_requests", 1) * 100
            print(f'   ⚠️  超时率: {timeout_rate:.1f}% ({timeout_count}/{stats.get("total_requests", 0)})')
        
        # 7. 建议
        print(f'\n7️⃣ 优化建议:')
        if active_count > 10:
            print(f'   - 活跃请求过多，考虑限制并发数')
        if timeout_count > stats.get("total_requests", 1) * 0.1:
            print(f'   - 超时率较高，考虑优化服务或增加资源')
        if stage_times:
            for stage, time_stats in stage_times.items():
                if isinstance(time_stats, dict) and time_stats.get("avg", 0) > 100:
                    print(f'   - {stage}阶段耗时过长 ({time_stats.get("avg", 0):.1f}秒)，需要优化')
        
    except Exception as e:
        print(f'   ❌ 获取统计失败: {e}')
    
    print('\n' + '=' * 80)
    print('✅ 诊断完成')

if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else '10.70.223.31'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9510
    
    diagnose_service(host, port)
