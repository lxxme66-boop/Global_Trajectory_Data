#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试客户端 - 验证服务参数传递
"""

import requests
import sys
import time

def test_search(host='10.70.223.31', port=9510):
    """测试搜索服务"""
    base_url = f'http://{host}:{port}'
    
    print(f'🧪 测试搜索服务: {base_url}')
    print('=' * 80)
    
    # 获取测试前的统计
    print('\n📊 测试前统计:')
    try:
        response = requests.get(f'{base_url}/api-rqa-search/stats', timeout=5)
        stats_before = response.json().get('data', {})
        print(f'  总请求: {stats_before.get("total_requests", 0)}')
        print(f'  成功: {stats_before.get("successful_requests", 0)}')
    except Exception as e:
        print(f'  ❌ 获取统计失败: {e}')
        stats_before = {}
    
    print('\n' + '=' * 80)
    print('🔬 测试不同的参数传递方式:')
    print('=' * 80)
    
    # 测试1: 正确的方式（使用form data）
    print('\n✅ 测试1: 正确方式 (data + form)')
    try:
        start_time = time.time()
        response = requests.post(
            f'{base_url}/api-rqa-search/search',
            data={  # ← 使用 data (form-encoded)
                'query': 'What is semiconductor?',
                'id': 123,
                'top_doc_num': 5
            },
            timeout=60
        )
        elapsed = time.time() - start_time
        
        result = response.json()
        print(f'  响应码: {result.get("code")}')
        print(f'  消息: {result.get("msg", "")[:100]}')
        print(f'  结果数: {result.get("data", {}).get("doc_num", 0)}')
        print(f'  耗时: {elapsed:.2f}秒')
        
        if elapsed < 1.0:
            print(f'  ⚠️  响应太快({elapsed:.2f}s)，可能有问题')
        else:
            print(f'  ✅ 耗时正常，请求可能真正执行了')
            
    except Exception as e:
        print(f'  ❌ 请求失败: {e}')
    
    time.sleep(1)
    
    # 测试2: 错误的方式（使用json）
    print('\n❌ 测试2: 错误方式 (json)')
    try:
        start_time = time.time()
        response = requests.post(
            f'{base_url}/api-rqa-search/search',
            json={  # ← 使用 json（可能导致参数无法解析）
                'query': 'What is semiconductor?',
                'id': 123,
                'top_doc_num': 5
            },
            timeout=60
        )
        elapsed = time.time() - start_time
        
        result = response.json()
        print(f'  响应码: {result.get("code")}')
        print(f'  消息: {result.get("msg", "")[:100]}')
        print(f'  耗时: {elapsed:.2f}秒')
        
        if result.get('code') == -1:
            print(f'  ⚠️  参数校验失败！这可能就是512个请求失败的原因')
            
    except Exception as e:
        print(f'  ❌ 请求失败: {e}')
    
    time.sleep(1)
    
    # 测试3: 空参数
    print('\n❌ 测试3: 空参数')
    try:
        start_time = time.time()
        response = requests.post(
            f'{base_url}/api-rqa-search/search',
            data={},
            timeout=60
        )
        elapsed = time.time() - start_time
        
        result = response.json()
        print(f'  响应码: {result.get("code")}')
        print(f'  消息: {result.get("msg", "")[:100]}')
        print(f'  耗时: {elapsed:.2f}秒')
        
        if elapsed < 0.1:
            print(f'  ✅ 快速返回，符合参数校验失败的预期')
            
    except Exception as e:
        print(f'  ❌ 请求失败: {e}')
    
    time.sleep(1)
    
    # 测试4: 缺少id
    print('\n❌ 测试4: 缺少id')
    try:
        start_time = time.time()
        response = requests.post(
            f'{base_url}/api-rqa-search/search',
            data={
                'query': 'test',
                # id: 缺少
                'top_doc_num': 5
            },
            timeout=60
        )
        elapsed = time.time() - start_time
        
        result = response.json()
        print(f'  响应码: {result.get("code")}')
        print(f'  消息: {result.get("msg", "")[:100]}')
        print(f'  耗时: {elapsed:.2f}秒')
            
    except Exception as e:
        print(f'  ❌ 请求失败: {e}')
    
    # 获取测试后的统计
    print('\n' + '=' * 80)
    print('📊 测试后统计:')
    print('=' * 80)
    
    try:
        time.sleep(1)
        response = requests.get(f'{base_url}/api-rqa-search/stats', timeout=5)
        stats_after = response.json().get('data', {})
        
        total_before = stats_before.get('total_requests', 0)
        total_after = stats_after.get('total_requests', 0)
        success_before = stats_before.get('successful_requests', 0)
        success_after = stats_after.get('successful_requests', 0)
        
        print(f'  总请求: {total_before} → {total_after} (+{total_after - total_before})')
        print(f'  成功: {success_before} → {success_after} (+{success_after - success_before})')
        
        new_requests = total_after - total_before
        new_success = success_after - success_before
        
        print('\n🔍 分析结果:')
        print(f'  本次测试发送了 4 个请求')
        print(f'  服务记录了 {new_requests} 个新请求')
        print(f'  其中成功了 {new_success} 个')
        
        if new_requests > 4:
            print(f'  ⚠️  记录的请求数({new_requests})多于发送数(4)，可能有其他客户端在调用')
        
        if new_success == 0:
            print(f'  ❌ 没有请求成功！可能的原因:')
            print(f'    1. 参数传递方式错误（使用了json而不是data）')
            print(f'    2. 服务端参数解析有问题')
            print(f'    3. 监控代码有bug')
        elif new_success == 1:
            print(f'  ✅ 只有1个请求成功，应该是"测试1: 正确方式"')
            print(f'  ⚠️  其他3个请求失败，证明参数传递方式很重要！')
        elif new_success > 1:
            print(f'  ✅ 有 {new_success} 个请求成功')
        
        # 检查阶段执行
        stage_times = stats_after.get('stage_times', {})
        has_stage_execution = False
        for stage, data in stage_times.items():
            if isinstance(data, dict) and data.get('count', 0) > 0:
                has_stage_execution = True
                break
        
        if new_success > 0 and not has_stage_execution:
            print(f'  ⚠️  有成功请求但阶段调用次数为0，监控代码可能有问题')
        elif new_success > 0 and has_stage_execution:
            print(f'  ✅ 阶段监控正常工作')
            
    except Exception as e:
        print(f'  ❌ 获取统计失败: {e}')
    
    # 最终结论
    print('\n' + '=' * 80)
    print('💡 结论和建议:')
    print('=' * 80)
    
    print('\n如果测试1成功，测试2失败:')
    print('  → 说明你的客户端可能使用了错误的参数传递方式（json）')
    print('  → 修复: 使用 data={...} 而不是 json={...}')
    print('')
    print('如果所有测试都失败:')
    print('  → 说明服务端可能有问题')
    print('  → 检查: 参数解析、编码服务、数据库连接等')
    print('')
    print('如果监控统计不准确:')
    print('  → 说明监控代码有bug')
    print('  → 修复: 在所有return语句前添加 MONITOR.end_request()')

if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else '10.70.223.31'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9510
    
    test_search(host, port)
