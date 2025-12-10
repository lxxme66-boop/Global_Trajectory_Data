#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
分析连接问题
"""

import requests
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

def test_connection_methods(host='10.70.223.31', port=9510):
    base_url = f'http://{host}:{port}'
    
    print('🔬 连接方式对比实验')
    print('=' * 80)
    
    # 方法1: 直接请求（每次新建连接）
    print('\n1️⃣ 方法1: 直接请求（每次新建TCP连接）')
    success = 0
    failed = 0
    for i in range(5):
        try:
            start = time.time()
            response = requests.get(f'{base_url}/api-rqa-search/test', timeout=5)
            elapsed = time.time() - start
            if response.status_code == 200:
                success += 1
                print(f'   尝试 {i+1}: ✅ 成功 ({elapsed:.2f}s)')
            else:
                failed += 1
                print(f'   尝试 {i+1}: ❌ HTTP {response.status_code}')
        except requests.exceptions.Timeout:
            failed += 1
            print(f'   尝试 {i+1}: ⏱️  连接超时 (>5s)')
        except Exception as e:
            failed += 1
            print(f'   尝试 {i+1}: ❌ {str(e)[:50]}')
        time.sleep(0.5)
    
    print(f'   结果: {success}/5 成功, {failed}/5 失败')
    
    # 方法2: Session复用连接
    print('\n2️⃣ 方法2: Session复用（复用TCP连接）')
    session = requests.Session()
    success = 0
    failed = 0
    for i in range(5):
        try:
            start = time.time()
            response = session.get(f'{base_url}/api-rqa-search/test', timeout=5)
            elapsed = time.time() - start
            if response.status_code == 200:
                success += 1
                print(f'   尝试 {i+1}: ✅ 成功 ({elapsed:.2f}s)')
            else:
                failed += 1
                print(f'   尝试 {i+1}: ❌ HTTP {response.status_code}')
        except requests.exceptions.Timeout:
            failed += 1
            print(f'   尝试 {i+1}: ⏱️  连接超时 (>5s)')
        except Exception as e:
            failed += 1
            print(f'   尝试 {i+1}: ❌ {str(e)[:50]}')
        time.sleep(0.5)
    
    print(f'   结果: {success}/5 成功, {failed}/5 失败')
    
    # 方法3: 并发测试（模拟高负载）
    print('\n3️⃣ 方法3: 高并发测试（32个并发连接）')
    print('   模拟压测场景...')
    
    def make_request(i):
        try:
            response = requests.get(f'{base_url}/api-rqa-search/test', timeout=5)
            return (i, response.status_code == 200, None)
        except Exception as e:
            return (i, False, str(e)[:30])
    
    success = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(make_request, i) for i in range(32)]
        for future in as_completed(futures, timeout=30):
            try:
                idx, ok, error = future.result()
                if ok:
                    success += 1
                else:
                    failed += 1
                    if error:
                        print(f'   请求 {idx}: ❌ {error}')
            except Exception as e:
                failed += 1
    
    print(f'   结果: {success}/32 成功, {failed}/32 失败')
    
    # 分析
    print('\n' + '=' * 80)
    print('📊 分析结论:')
    
    if failed > 0 and success == 0:
        print('   🚨 服务完全无法连接')
        print('   原因: 服务未启动或网络不通')
    elif failed > success:
        print('   ⚠️  服务负载极高，新连接困难')
        print('   原因: Flask单进程 + 监听队列满')
        print('   解决: 1) 降低并发数  2) 使用连接池  3) 改用gunicorn多进程')
    elif success > failed:
        print('   ✅ 服务基本正常，偶尔超时')
        print('   原因: 瞬时负载波动')
        print('   解决: 增加超时时间或等待负载降低')
    else:
        print('   ✅ 服务正常运行')
    
    print('\n💡 为什么压测能跑但诊断超时？')
    print('   1. 压测脚本可能使用了Session（连接复用）')
    print('   2. 压测脚本在负载低时建立了连接')
    print('   3. 诊断脚本在负载高时尝试新建连接被拒绝')
    print('   4. Flask单进程accept()队列有限（默认128）')
    
    print('\n🔧 解决方案:')
    print('   1. 使用Session复用连接')
    print('   2. 增加诊断脚本超时时间（30-60秒）')
    print('   3. 在压测间隙进行诊断')
    print('   4. 改用gunicorn多进程部署：')
    print('      gunicorn -w 4 -b 10.70.223.31:9510 --timeout 300 app:app')

if __name__ == '__main__':
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else '10.70.223.31'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9510
    
    test_connection_methods(host, port)
