#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试诊断服务 - 模拟各种请求场景
"""

import requests
import time

def test_search_service(host='localhost', port=9599):
    """测试搜索服务并诊断问题"""
    base_url = f'http://{host}:{port}'
    
    print(f'🧪 测试服务: {base_url}')
    print('=' * 80)
    
    # 场景1: 正常请求
    print('\n场景1: 正常请求')
    response = requests.post(f'{base_url}/api-rqa-search/search', data={
        'query': 'What is semiconductor?',
        'id': 123,
        'top_doc_num': 5
    })
    print(f'  结果: {response.json()}')
    
    # 场景2: 缺少query
    print('\n场景2: 缺少query')
    response = requests.post(f'{base_url}/api-rqa-search/search', data={
        'query': '',  # 空query
        'id': 123,
        'top_doc_num': 5
    })
    print(f'  结果: {response.json()}')
    
    # 场景3: 缺少id
    print('\n场景3: 缺少id')
    response = requests.post(f'{base_url}/api-rqa-search/search', data={
        'query': 'test query',
        'id': 0,  # id为0
        'top_doc_num': 5
    })
    print(f'  结果: {response.json()}')
    
    # 场景4: 缺少top_doc_num
    print('\n场景4: 缺少top_doc_num')
    response = requests.post(f'{base_url}/api-rqa-search/search', data={
        'query': 'test query',
        'id': 123,
        'top_doc_num': 0  # top_doc_num为0
    })
    print(f'  结果: {response.json()}')
    
    # 场景5: 完全缺少参数
    print('\n场景5: 完全缺少参数')
    response = requests.post(f'{base_url}/api-rqa-search/search', data={})
    print(f'  结果: {response.json()}')
    
    # 等待一下
    time.sleep(1)
    
    # 获取诊断报告
    print('\n' + '=' * 80)
    print('📊 诊断报告:')
    print('=' * 80)
    response = requests.get(f'{base_url}/api-rqa-search/diagnose')
    report = response.json()['data']
    
    print(f'\n总记录请求数: {report["total_logged_requests"]}')
    
    print(f'\n事件统计:')
    for event, count in report['event_counts'].items():
        print(f'  {event}: {count}')
    
    print(f'\n拒绝原因统计:')
    for reason, count in report['rejection_reasons'].items():
        print(f'  {reason}: {count}')
    
    print(f'\n最近10条请求:')
    for req in report['recent_requests']:
        print(f'  [{req["req_id"]}] {req["event"]}: {req["detail"]}')
    
    print('\n' + '=' * 80)
    print('✅ 测试完成')
    
    # 分析结果
    print('\n🔍 问题分析:')
    if 'REJECTED' in report['event_counts']:
        reject_count = report['event_counts']['REJECTED']
        total_count = report['event_counts'].get('RECEIVED', 0)
        if reject_count > 0:
            print(f'  ⚠️ 有 {reject_count}/{total_count} 个请求被拒绝')
            print(f'  主要原因:')
            for reason, count in report['rejection_reasons'].items():
                print(f'    - {reason}: {count}次')
    
    if 'COMPLETED' in report['event_counts']:
        success_count = report['event_counts']['COMPLETED']
        print(f'  ✅ 有 {success_count} 个请求成功完成')
    else:
        print(f'  ⚠️ 没有请求成功完成！')

if __name__ == '__main__':
    test_search_service()
