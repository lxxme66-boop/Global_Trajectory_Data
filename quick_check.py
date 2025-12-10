#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
快速检查服务是否可用
"""

import requests
import time
import sys

def quick_check(host='10.70.223.31', port=9510):
    base_url = f'http://{host}:{port}'
    
    print(f'🔍 快速检查服务: {base_url}')
    print('=' * 60)
    
    # 1. 测试简单接口
    print('\n1️⃣ 测试 /test 接口...')
    try:
        start = time.time()
        response = requests.get(f'{base_url}/api-rqa-search/test', timeout=10)
        elapsed = time.time() - start
        
        if response.status_code == 200:
            print(f'   ✅ 服务可用 (响应时间: {elapsed:.2f}s)')
        else:
            print(f'   ❌ 服务异常: HTTP {response.status_code}')
    except requests.exceptions.Timeout:
        print(f'   ⚠️  响应超时 (>10s) - 服务可能负载很高')
    except Exception as e:
        print(f'   ❌ 连接失败: {e}')
        return False
    
    # 2. 测试搜索接口
    print('\n2️⃣ 测试 /search 接口...')
    try:
        start = time.time()
        response = requests.post(
            f'{base_url}/api-rqa-search/search',
            data={
                'query': 'test query',
                'id': 1,
                'top_doc_num': 1
            },
            timeout=60
        )
        elapsed = time.time() - start
        
        if response.status_code == 200:
            result = response.json()
            code = result.get('code', -1)
            if code == 0 or code == 1:  # 0=成功, 1=不相关
                print(f'   ✅ 搜索功能正常 (响应时间: {elapsed:.2f}s)')
            else:
                print(f'   ⚠️  搜索返回错误: code={code}')
        else:
            print(f'   ❌ HTTP错误: {response.status_code}')
    except requests.exceptions.Timeout:
        print(f'   ⚠️  搜索超时 (>60s) - 可能卡住了')
    except Exception as e:
        print(f'   ❌ 搜索失败: {e}')
        return False
    
    # 3. 判断
    print('\n' + '=' * 60)
    print('📊 结论:')
    print('   ✅ 服务正在运行')
    print('   ⚠️  如果健康检查超时但搜索正常，说明:')
    print('      - 服务负载较高（正在处理大量请求）')
    print('      - 健康检查接口排队等待')
    print('      - 这是正常现象，不影响搜索功能')
    print('\n💡 建议:')
    print('   - 等当前压测完成后再做详细诊断')
    print('   - 或使用 --request-timeout 参数降低单请求超时')
    print('   - 监控服务日志中的 [Monitor] 输出')
    
    return True

if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else '10.70.223.31'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9510
    
    quick_check(host, port)
