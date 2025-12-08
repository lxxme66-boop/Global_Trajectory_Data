import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import json
from time import sleep


def search_answer_online(query: str, top_doc_num: int = 5):
    """原始的搜索函数"""
    url = "http://218.104.107.132:5002/mongodb"
    
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': '<calculated when request is sent>',
        'Accept-Encoding': 'gzip, deflate, br'
    }
    
    sent_data = {
        'id': 102, 
        'top_doc_num': top_doc_num,
        'query': query
    }
    
    res_json = requests.post(url, verify=False, headers=headers, data=sent_data, timeout=(3600, 3600)).content.decode('utf-8')
    res_data = json.loads(res_json).get('data')
    res_num = res_data.get('doc_num')
    
    results = []
    for i in range(res_num):
        ans = res_data.get('arr')[i]
        results.append([ans['ans_id'], ans['title'], ans['text'], ans['index'], ans['score']])
    
    if res_num < 5:
        for i in range(5 - res_num):
            results.append(['null'] * 4 + [0.0])
    
    sleep(3)
    
    return results[0]


def _search_rag_api(query: str, top_doc_num: int = 5) -> list:
    """改进版搜索函数"""
    url = "http://218.104.107.132:5002/mongodb"
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    sent_data = {'id': 102, 'top_doc_num': top_doc_num, 'query': query}
    try:
        res = requests.post(url, verify=False, headers=headers, data=sent_data, timeout=(3600, 3600))
        res.raise_for_status()
        res_data = res.json().get('data', {})
        res_num = res_data.get('doc_num', 0)
        results = []
        for i in range(res_num):
            ans = res_data.get('arr', [])[i]
            results.append([ans.get('ans_id'), ans.get('title'), ans.get('text'), ans.get('index'), ans.get('score')])
        padding_needed = top_doc_num - len(results)
        if padding_needed > 0:
            results.extend([['null', 'null', 'null', 'null', 0.0]] * padding_needed)
        sleep(3)
        return results[0]
    except requests.exceptions.RequestException as e:
        print(f"❌ [TclRag Tool Error] RAG接口请求失败: {e}")
        return ['null', 'null', 'null', 'null', 0.0]


def run_concurrency_test():
    """并发测试 - 实时打印每个请求的耗时"""
    # --- 可配置的测试参数 ---
    TOTAL_REQUESTS = 512      # 总共要发起的请求数
    MAX_CONCURRENCY = 16     # 最大并发数
    # -------------------------
    
    print(f"开始并发测试: 总请求数={TOTAL_REQUESTS}, 最大并发数={MAX_CONCURRENCY}")
    print("=" * 80)
    
    # 读取测试数据
    filename = "sampled_data.jsonl"
    target_column = 'question'
    
    test_queries = []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    test_queries.append(data.get(target_column))
    except FileNotFoundError:
        print(f"❌ 文件 {filename} 不存在，使用默认测试查询")
        test_queries = [f"测试查询 {i}" for i in range(TOTAL_REQUESTS)]
    
    success_count = 0
    error_count = 0
    request_times = []  # 存储每个请求的耗时
    completed_count = 0  # 已完成的请求数
    
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as executor:
        # 提交所有任务，并记录提交时间
        future_to_info = {}
        for idx, query in enumerate(test_queries):
            submit_time = time.time()  # 记录提交时间
            future = executor.submit(search_answer_online, query)
            future_to_info[future] = {
                'query': query,
                'submit_time': submit_time,
                'index': idx + 1  # 请求编号（从1开始）
            }
        
        # 任务完成时实时打印
        for future in as_completed(future_to_info):
            complete_time = time.time()  # 记录完成时间
            info = future_to_info[future]
            query = info['query']
            submit_time = info['submit_time']
            request_index = info['index']
            
            duration = complete_time - submit_time  # 单个请求耗时
            elapsed_total = complete_time - start_time  # 已经过的总时间
            completed_count += 1
            
            try:
                result = future.result()
                if result and isinstance(result, list) and len(result) > 0:
                    success_count += 1
                    request_times.append(duration)
                    
                    # 实时打印成功信息
                    print(f"✅ [{completed_count}/{len(test_queries)}] "
                          f"请求#{request_index} | "
                          f"耗时: {duration:.2f}s | "
                          f"总进度: {elapsed_total:.1f}s | "
                          f"查询: {query[:40]}...")
                else:
                    error_count += 1
                    print(f"❌ [{completed_count}/{len(test_queries)}] "
                          f"请求#{request_index} | "
                          f"耗时: {duration:.2f}s | "
                          f"失败(结果为空) | "
                          f"查询: {query[:40]}...")
            except Exception as exc:
                error_count += 1
                print(f"❌ [{completed_count}/{len(test_queries)}] "
                      f"请求#{request_index} | "
                      f"耗时: {duration:.2f}s | "
                      f"异常: {str(exc)[:30]}...")
    
    end_time = time.time()
    total_time = end_time - start_time
    
    # --- 打印最终统计结果 ---
    print("\n" + "=" * 80)
    print("🎯 并发测试完成 - 最终统计")
    print("=" * 80)
    print(f"总耗时: {total_time:.2f} 秒 ({total_time/60:.2f} 分钟)")
    print(f"成功请求: {success_count}")
    print(f"失败请求: {error_count}")
    print(f"总请求数: {len(test_queries)}")
    
    if request_times:
        avg_time = sum(request_times) / len(request_times)
        min_time = min(request_times)
        max_time = max(request_times)
        
        print(f"\n⏱️  单个请求耗时统计:")
        print(f"  - 平均耗时: {avg_time:.2f} 秒")
        print(f"  - 最快请求: {min_time:.2f} 秒")
        print(f"  - 最慢请求: {max_time:.2f} 秒")
        
        # 计算百分位数
        sorted_times = sorted(request_times)
        p50_idx = int(len(sorted_times) * 0.5)
        p95_idx = int(len(sorted_times) * 0.95)
        p99_idx = int(len(sorted_times) * 0.99)
        
        print(f"\n📊 响应时间分布:")
        print(f"  - P50 (中位数): {sorted_times[p50_idx]:.2f} 秒")
        print(f"  - P95: {sorted_times[p95_idx]:.2f} 秒")
        print(f"  - P99: {sorted_times[p99_idx]:.2f} 秒")
    
    if total_time > 0:
        qps = success_count / total_time
        print(f"\n🚀 实际 QPS (每秒查询数): {qps:.2f} 请求/秒")
        print(f"📈 理论最大 QPS (基于平均耗时): {MAX_CONCURRENCY/avg_time:.2f} 请求/秒")
    
    print("=" * 80)


def run_single_test():
    """运行单次测试"""
    print("--- 运行单次测试 ---")
    query = "在IGZO TFT中，环境气氛中的氧气是如何影响TFT的阈值电压的？"
    try:
        start = time.time()
        result = _search_rag_api(query=query)
        end = time.time()
        
        print(f"✅ 单次调用成功! 耗时: {end - start:.2f} 秒")
        print(f"结果: {result}")
    except Exception as e:
        print(f"❌ 单次调用失败: {e}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    # 1. 先跑一次单体测试
    # run_single_test()
    
    # 2. 进行并发测试（实时打印）
    run_concurrency_test()
