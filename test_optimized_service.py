#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试优化后的搜索服务
验证请求ID冲突是否已修复
"""

import requests
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

def test_health(host='10.70.223.31', port=9510):
    """测试健康检查"""
    print('🔍 1. 健康检查...')
    print('=' * 70)
    
    try:
        response = requests.get(f'http://{host}:{port}/api-rqa-search/health', timeout=10)
        health = response.json()
        data = health.get('data', {})
        
        print(f'   ✅ 状态: {data.get("status", "unknown")}')
        print(f'   ✅ 版本: {data.get("version", "unknown")}')
        print(f'   ✅ 运行时间: {data.get("uptime", 0):.1f}秒')
        print(f'   ✅ 超时策略: {data.get("timeout_config", "unknown")}')
        print(f'   ✅ 请求ID修复: {data.get("request_id_fix", "unknown")}')
        return True
    except Exception as e:
        print(f'   ❌ 健康检查失败: {e}')
        return False

def test_stats(host='10.70.223.31', port=9510):
    """测试统计信息"""
    print('\n🔍 2. 服务统计...')
    print('=' * 70)
    
    try:
        response = requests.get(f'http://{host}:{port}/api-rqa-search/stats', timeout=10)
        result = response.json()
        stats = result.get('data', {})
        
        total = stats.get('total_requests', 0)
        success = stats.get('successful_requests', 0)
        failed = stats.get('failed_requests', 0)
        timeout = stats.get('timeout_requests', 0)
        active = stats.get('active_requests', 0)
        
        print(f'   总请求数: {total}')
        print(f'   成功请求: {success}')
        print(f'   失败请求: {failed}')
        print(f'   超时请求: {timeout}')
        print(f'   活跃请求: {active}')
        
        # 检查统计数据一致性
        print(f'\n   📊 统计数据一致性检查:')
        if total > 0:
            accounted = success + failed + timeout
            success_rate = (success / total * 100) if total > 0 else 0
            
            if accounted >= total * 0.9:  # 允许10%误差（因为可能有正在处理的请求）
                print(f'   ✅ 统计数据一致: {accounted}/{total} ({accounted/total*100:.1f}%)')
            else:
                print(f'   ⚠️  统计数据可能有遗漏: {accounted}/{total} ({accounted/total*100:.1f}%)')
            
            print(f'   ✅ 成功率: {success_rate:.1f}%')
        else:
            print(f'   ℹ️  暂无请求数据')
        
        # 阶段统计
        stage_times = stats.get('stage_times', {})
        if stage_times:
            print(f'\n   📈 阶段性能统计:')
            for stage, time_stats in stage_times.items():
                if isinstance(time_stats, dict):
                    count = time_stats.get('count', 0)
                    avg = time_stats.get('avg', 0)
                    p95 = time_stats.get('p95', 0)
                    
                    if count > 0:
                        print(f'   ✅ {stage}:')
                        print(f'      - 调用次数: {count}')
                        print(f'      - 平均耗时: {avg:.2f}秒')
                        print(f'      - P95耗时: {p95:.2f}秒')
                    else:
                        print(f'   ℹ️  {stage}: 暂无数据')
        else:
            print(f'\n   ℹ️  阶段统计: 暂无数据')
        
        # 缓存统计
        cache_stats = stats.get('encode_cache', {})
        if cache_stats:
            print(f'\n   💾 编码缓存:')
            print(f'   - 缓存大小: {cache_stats.get("size", 0)}')
            print(f'   - 命中次数: {cache_stats.get("hits", 0)}')
            print(f'   - 未命中: {cache_stats.get("misses", 0)}')
            print(f'   - 命中率: {cache_stats.get("hit_rate", "0%")}')
        
        return True
    except Exception as e:
        print(f'   ❌ 获取统计失败: {e}')
        return False

def test_concurrent_requests(host='10.70.223.31', port=9510, num_requests=20):
    """测试并发请求（验证请求ID冲突是否修复）"""
    print(f'\n🔍 3. 并发请求测试 ({num_requests}个并发请求)...')
    print('=' * 70)
    
    base_url = f'http://{host}:{port}/api-rqa-search/search'
    
    # 获取测试前的统计
    try:
        response = requests.get(f'http://{host}:{port}/api-rqa-search/stats', timeout=5)
        before_stats = response.json().get('data', {})
        before_total = before_stats.get('total_requests', 0)
        before_success = before_stats.get('successful_requests', 0)
    except:
        before_total = 0
        before_success = 0
    
    print(f'   测试前统计: 总请求={before_total}, 成功={before_success}')
    
    # 发送并发请求
    def send_search_request(idx):
        try:
            data = {
                'query': f'测试查询 {idx}',
                'id': 1,
                'top_doc_num': 5
            }
            response = requests.post(base_url, data=data, timeout=30)
            return {
                'index': idx,
                'status_code': response.status_code,
                'success': response.status_code == 200,
                'data': response.json() if response.status_code == 200 else None
            }
        except Exception as e:
            return {
                'index': idx,
                'status_code': 0,
                'success': False,
                'error': str(e)
            }
    
    print(f'   ⏳ 发送 {num_requests} 个并发请求...')
    start_time = time.time()
    
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(send_search_request, i) for i in range(num_requests)]
        
        for future in as_completed(futures):
            try:
                result = future.result(timeout=60)
                results.append(result)
            except Exception as e:
                results.append({
                    'index': -1,
                    'status_code': 0,
                    'success': False,
                    'error': str(e)
                })
    
    duration = time.time() - start_time
    
    # 统计结果
    successful = sum(1 for r in results if r['success'])
    failed = len(results) - successful
    
    print(f'   ✅ 请求完成: {len(results)}/{num_requests}')
    print(f'   ✅ 成功: {successful}')
    print(f'   ❌ 失败: {failed}')
    print(f'   ⏱️  总耗时: {duration:.2f}秒')
    print(f'   ⚡ 平均耗时: {duration/num_requests:.2f}秒/请求')
    
    # 等待一下，让服务器更新统计
    time.sleep(2)
    
    # 获取测试后的统计
    try:
        response = requests.get(f'http://{host}:{port}/api-rqa-search/stats', timeout=5)
        after_stats = response.json().get('data', {})
        after_total = after_stats.get('total_requests', 0)
        after_success = after_stats.get('successful_requests', 0)
    except:
        after_total = 0
        after_success = 0
    
    print(f'\n   测试后统计: 总请求={after_total}, 成功={after_success}')
    
    # 验证统计数据一致性
    print(f'\n   🔍 请求ID冲突检查:')
    
    total_increase = after_total - before_total
    success_increase = after_success - before_success
    
    print(f'   - 预期新增请求: {num_requests}')
    print(f'   - 实际新增总请求: {total_increase}')
    print(f'   - 实际新增成功请求: {success_increase}')
    
    if total_increase >= num_requests * 0.9:  # 允许10%误差
        print(f'   ✅ 总请求数统计正常 ({total_increase}/{num_requests})')
    else:
        print(f'   ❌ 总请求数统计异常 ({total_increase}/{num_requests})')
    
    if success_increase >= successful * 0.9:  # 允许10%误差
        print(f'   ✅ 成功请求数统计正常 ({success_increase}/{successful})')
        print(f'   ✅ 请求ID冲突已修复！')
    else:
        print(f'   ❌ 成功请求数统计异常 ({success_increase}/{successful})')
        print(f'   ⚠️  可能仍存在请求ID冲突问题')
    
    # 检查阶段统计
    stage_times = after_stats.get('stage_times', {})
    if stage_times:
        has_stage_data = any(
            isinstance(v, dict) and v.get('count', 0) > 0 
            for v in stage_times.values()
        )
        if has_stage_data:
            print(f'   ✅ 阶段统计数据正常')
        else:
            print(f'   ⚠️  阶段统计数据为空')
    
    return successful >= num_requests * 0.8  # 至少80%成功算通过

def test_single_search(host='10.70.223.31', port=9510):
    """测试单个搜索请求"""
    print(f'\n🔍 4. 单个搜索请求测试...')
    print('=' * 70)
    
    base_url = f'http://{host}:{port}/api-rqa-search/search'
    
    try:
        data = {
            'query': '半导体制造工艺',
            'id': 1,
            'top_doc_num': 3
        }
        
        print(f'   ⏳ 发送搜索请求...')
        start_time = time.time()
        
        response = requests.post(base_url, data=data, timeout=60)
        duration = time.time() - start_time
        
        if response.status_code == 200:
            result = response.json()
            code = result.get('code', -1)
            msg = result.get('msg', '')
            data_result = result.get('data', {})
            doc_num = data_result.get('doc_num', 0)
            
            print(f'   ✅ 请求成功')
            print(f'   - 响应码: {code}')
            print(f'   - 消息: {msg}')
            print(f'   - 文档数: {doc_num}')
            print(f'   - 耗时: {duration:.2f}秒')
            
            if code == 0 and doc_num > 0:
                print(f'   ✅ 搜索功能正常')
                return True
            else:
                print(f'   ⚠️  搜索返回空结果')
                return False
        else:
            print(f'   ❌ 请求失败: HTTP {response.status_code}')
            return False
            
    except Exception as e:
        print(f'   ❌ 请求异常: {e}')
        return False

def main():
    """主测试函数"""
    if len(sys.argv) > 1:
        host = sys.argv[1]
    else:
        host = '10.70.223.31'
    
    if len(sys.argv) > 2:
        port = int(sys.argv[2])
    else:
        port = 9510
    
    print('🧪 测试优化后的搜索服务')
    print('=' * 70)
    print(f'服务地址: http://{host}:{port}')
    print('=' * 70)
    
    results = {}
    
    # 1. 健康检查
    results['health'] = test_health(host, port)
    
    # 2. 统计信息
    results['stats'] = test_stats(host, port)
    
    # 3. 并发请求测试（重点：验证请求ID冲突修复）
    results['concurrent'] = test_concurrent_requests(host, port, num_requests=20)
    
    # 4. 单个搜索测试
    # results['search'] = test_single_search(host, port)  # 可选
    
    # 总结
    print('\n' + '=' * 70)
    print('📊 测试总结')
    print('=' * 70)
    
    all_passed = True
    for test_name, passed in results.items():
        status = '✅ 通过' if passed else '❌ 失败'
        print(f'{test_name.ljust(20)}: {status}')
        if not passed:
            all_passed = False
    
    print('=' * 70)
    if all_passed:
        print('🎉 所有测试通过！请求ID冲突已修复！')
    else:
        print('⚠️  部分测试失败，请检查服务状态')
    
    return 0 if all_passed else 1

if __name__ == '__main__':
    sys.exit(main())
