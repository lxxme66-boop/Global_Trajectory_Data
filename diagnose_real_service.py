#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
诊断真实服务 - 检查为什么请求没有真正执行
"""

import requests
import json
import sys
import time

def diagnose_service(host='10.70.223.31', port=9510):
    """诊断服务状态"""
    
    base_url = f'http://{host}:{port}'
    
    print(f'🔍 诊断服务: {base_url}')
    print('=' * 80)
    
    # 1. 获取当前统计
    print('\n📊 当前统计信息:')
    try:
        response = requests.get(f'{base_url}/api-rqa-search/stats', timeout=30)
        stats = response.json().get('data', {})
        
        total = stats.get('total_requests', 0)
        success = stats.get('successful_requests', 0)
        failed = stats.get('failed_requests', 0)
        timeout = stats.get('timeout_requests', 0)
        
        print(f'  总请求: {total}')
        print(f'  成功: {success}')
        print(f'  失败: {failed}')
        print(f'  超时: {timeout}')
        print(f'  未完成: {total - success - failed - timeout}')
        
        # 计算异常率
        if total > 0:
            unfinished = total - success - failed - timeout
            if unfinished > 0:
                print(f'\n⚠️  发现问题: {unfinished}/{total} ({unfinished/total*100:.1f}%) 个请求未完成！')
                print(f'  这些请求可能在参数校验阶段就被拦截了')
        
    except Exception as e:
        print(f'  ❌ 无法获取统计: {e}')
        return
    
    # 2. 发送测试请求并分析
    print('\n' + '=' * 80)
    print('🧪 发送测试请求:')
    print('=' * 80)
    
    test_cases = [
        {
            'name': '正常请求',
            'data': {
                'query': 'What is semiconductor?',
                'id': 12345,
                'top_doc_num': 5
            }
        },
        {
            'name': '缺少query',
            'data': {
                'query': '',
                'id': 12345,
                'top_doc_num': 5
            }
        },
        {
            'name': '缺少id',
            'data': {
                'query': 'test',
                'id': 0,
                'top_doc_num': 5
            }
        },
        {
            'name': '缺少top_doc_num',
            'data': {
                'query': 'test',
                'id': 12345,
                'top_doc_num': 0
            }
        },
        {
            'name': '完全空请求',
            'data': {}
        }
    ]
    
    # 记录测试前的统计
    stats_before = stats.copy()
    
    for i, test_case in enumerate(test_cases, 1):
        print(f'\n{i}. {test_case["name"]}')
        print(f'   参数: {test_case["data"]}')
        
        try:
            start_time = time.time()
            response = requests.post(
                f'{base_url}/api-rqa-search/search',
                data=test_case['data'],
                timeout=30
            )
            elapsed = time.time() - start_time
            
            result = response.json()
            print(f'   响应码: {result.get("code")}')
            print(f'   消息: {result.get("msg", "")[:100]}')
            print(f'   耗时: {elapsed:.2f}秒')
            
            # 分析响应时间
            if elapsed < 0.5:
                print(f'   ⚠️  响应太快({elapsed:.2f}s)，可能在参数校验阶段就返回了')
            
        except Exception as e:
            print(f'   ❌ 请求失败: {e}')
        
        time.sleep(0.5)
    
    # 3. 对比测试前后的统计
    print('\n' + '=' * 80)
    print('📊 测试后统计对比:')
    print('=' * 80)
    
    try:
        time.sleep(1)  # 等待统计更新
        response = requests.get(f'{base_url}/api-rqa-search/stats', timeout=30)
        stats_after = response.json().get('data', {})
        
        total_before = stats_before.get('total_requests', 0)
        total_after = stats_after.get('total_requests', 0)
        success_before = stats_before.get('successful_requests', 0)
        success_after = stats_after.get('successful_requests', 0)
        
        print(f'  总请求: {total_before} → {total_after} (+{total_after - total_before})')
        print(f'  成功: {success_before} → {success_after} (+{success_after - success_before})')
        
        # 分析
        new_requests = total_after - total_before
        new_success = success_after - success_before
        
        if new_requests > 0:
            print(f'\n  新增请求: {new_requests}')
            print(f'  新增成功: {new_success}')
            print(f'  未记录为成功: {new_requests - new_success}')
            
            if new_requests - new_success > 0:
                print(f'\n⚠️  问题确认: {new_requests - new_success} 个请求被拦截！')
                print(f'  可能原因:')
                print(f'    1. 参数校验失败 (query/id/top_doc_num 为空)')
                print(f'    2. 代码在参数校验后直接return，没有更新监控')
                print(f'    3. 监控代码未覆盖所有返回路径')
        
    except Exception as e:
        print(f'  ❌ 无法获取测试后统计: {e}')
    
    # 4. 检查阶段执行情况
    print('\n' + '=' * 80)
    print('🔬 阶段执行分析:')
    print('=' * 80)
    
    stage_times = stats_after.get('stage_times', {})
    
    if not stage_times or all(
        isinstance(v, dict) and v.get('count', 0) == 0 
        for v in stage_times.values()
    ):
        print('  ⚠️  所有阶段调用次数都是0！')
        print('  这意味着:')
        print('    - 请求进来了 (total_requests增加)')
        print('    - 但没有真正进入搜索pipeline')
        print('    - 很可能在参数校验阶段就返回了')
        print('')
        print('  建议检查:')
        print('    1. 客户端是否正确传递参数 (query, id, top_doc_num)')
        print('    2. 服务端参数解析是否正确')
        print('    3. 代码中的 MONITOR.update_stage() 是否被调用')
    else:
        print('  阶段执行情况:')
        for stage, data in stage_times.items():
            if isinstance(data, dict):
                count = data.get('count', 0)
                avg = data.get('avg', 0)
                print(f'    {stage}: {count}次调用, 平均{avg:.2f}秒')
    
    # 5. 最终建议
    print('\n' + '=' * 80)
    print('💡 诊断结论和建议:')
    print('=' * 80)
    
    if total > 0 and success < total * 0.1:
        print('\n❌ 严重问题: 大量请求未完成')
        print('')
        print('根本原因:')
        print('  代码在参数校验失败时直接return，但没有调用 MONITOR.end_request()')
        print('')
        print('修复方案:')
        print('  在所有提前返回的地方添加监控记录，例如:')
        print('')
        print('  ```python')
        print('  if query == "":')
        print('      MONITOR.end_request(req_id, success=False)')  
        print('      return json_result(-1, "query must not be null.", None)')
        print('  ```')
        print('')
        print('  或者使用try-finally确保监控总是被调用:')
        print('')
        print('  ```python')
        print('  req_id = ...')
        print('  MONITOR.start_request(req_id, query)')
        print('  try:')
        print('      # 参数校验和搜索逻辑')
        print('      ...')
        print('  finally:')
        print('      MONITOR.end_request(req_id, success=...)')
        print('  ```')

if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else '10.70.223.31'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9510
    
    diagnose_service(host, port)
