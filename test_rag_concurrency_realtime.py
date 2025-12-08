import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import json
from time import sleep

# =============================================================================
# 步骤 1: RAG API 调用函数
# =============================================================================

def _search_rag_api(query: str, top_doc_num: int = 5) -> list:
    """
    调用 RAG API 接口
    """
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
            results.append([
                ans.get('ans_id'), 
                ans.get('title'), 
                ans.get('text'), 
                ans.get('index'), 
                ans.get('score')
            ])
        
        # 填充到指定数量
        padding_needed = top_doc_num - len(results)
        if padding_needed > 0:
            results.extend([['null', 'null', 'null', 'null', 0.0]] * padding_needed)
        
        sleep(3)  # 控制请求频率
        return results[0]
    
    except requests.exceptions.RequestException as e:
        print(f"❌ [RAG API Error] 请求失败: {e}")
        return ['null', 'null', 'null', 'null', 0.0]


# =============================================================================
# 步骤 2: 实时显示QPS的并发测试
# =============================================================================

def run_concurrency_test_realtime():
    """
    并发测试，每完成一个请求就更新一次QPS显示
    """
    # --- 可配置的测试参数 ---
    TOTAL_REQUESTS = 512      # 总共要发起的请求数
    MAX_CONCURRENCY = 64     # 最大并发数
    # -------------------------

    print(f"🚀 开始并发测试: 总请求数={TOTAL_REQUESTS}, 最大并发数={MAX_CONCURRENCY}")
    print("="*80)

    # 加载测试数据
    filename = "/mnt/workspace/LLM/ldd/轨迹生成/飞飞新的轨迹数据/sampled_data.jsonl"
    target_column = 'question'
    
    test_queries = []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    test_queries.append(data.get(target_column))
    except FileNotFoundError:
        print(f"⚠️  文件未找到: {filename}")
        print("使用默认测试查询...")
        test_queries = [f"测试查询 {i}" for i in range(TOTAL_REQUESTS)]
    
    # 限制请求数量
    test_queries = test_queries[:TOTAL_REQUESTS]
    
    success_count = 0
    error_count = 0
    completed_count = 0
    
    start_time = time.time()

    # 创建线程池并提交所有任务
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as executor:
        future_to_query = {
            executor.submit(_search_rag_api, query): query 
            for query in test_queries
        }

        # 实时处理完成的任务
        for future in as_completed(future_to_query):
            query = future_to_query[future]
            completed_count += 1
            current_time = time.time()
            elapsed_time = current_time - start_time
            
            try:
                result = future.result()
                # 判断是否成功
                if result and isinstance(result, list):
                    success_count += 1
                    status = "✅"
                else:
                    error_count += 1
                    status = "❌"
            except Exception as exc:
                error_count += 1
                status = "❌"
                # 异常时换行显示详细信息
                print(f"\n{status} 异常: {str(exc)[:100]}")
            
            # 实时计算 QPS
            current_qps = success_count / elapsed_time if elapsed_time > 0 else 0
            
            # 计算进度百分比
            progress_percent = (completed_count / len(test_queries)) * 100
            
            # 计算预计剩余时间
            if completed_count > 0:
                avg_time_per_request = elapsed_time / completed_count
                remaining_requests = len(test_queries) - completed_count
                eta_seconds = avg_time_per_request * remaining_requests
                eta_minutes = eta_seconds / 60
            else:
                eta_minutes = 0
            
            # 实时进度显示（使用 \r 覆盖同一行，flush=True 立即刷新）
            print(
                f"\r📊 进度: {completed_count}/{len(test_queries)} ({progress_percent:.1f}%) | "
                f"✅ 成功: {success_count} | ❌ 失败: {error_count} | "
                f"⏱️  耗时: {elapsed_time:.1f}s | "
                f"🚀 实时QPS: {current_qps:.2f} | "
                f"⏳ 预计剩余: {eta_minutes:.1f}分钟",
                end='', 
                flush=True
            )
    
    end_time = time.time()
    total_time = end_time - start_time
    
    # 换行，打印最终结果
    print("\n" + "="*80)
    print("📈 最终测试结果")
    print("="*80)
    print(f"⏱️  总耗时: {total_time:.2f} 秒 ({total_time/60:.2f} 分钟)")
    print(f"✅ 成功请求: {success_count}")
    print(f"❌ 失败请求: {error_count}")
    print(f"📝 总请求数: {len(test_queries)}")
    print(f"✔️  成功率: {(success_count/len(test_queries)*100):.2f}%")

    if total_time > 0:
        final_qps = success_count / total_time
        avg_time = total_time / len(test_queries)
        print(f"🚀 最终 QPS (每秒查询数): {final_qps:.2f}")
        print(f"⏱️  平均每个请求耗时: {avg_time:.2f} 秒")
        
        # 理论最大QPS（不考虑sleep的情况）
        theoretical_qps = MAX_CONCURRENCY / 3  # 每个请求有3秒sleep
        print(f"📊 理论最大QPS (考虑3秒sleep): ~{theoretical_qps:.2f}")
    
    print("="*80)


# =============================================================================
# 步骤 3: 单次测试（用于验证功能）
# =============================================================================

def run_single_test():
    """
    运行单次测试，验证 API 是否正常工作
    """
    print("--- 运行单次测试 ---")
    query = "在IGZO TFT中，环境气氛中的氧气是如何影响TFT的阈值电压的？"
    try:
        result = _search_rag_api(query=query)
        print("✅ 单次调用成功!")
        print(f"结果: {result}")
    except Exception as e:
        print(f"❌ 单次调用失败: {e}")
    print("="*50 + "\n")


# =============================================================================
# 主程序入口
# =============================================================================

if __name__ == "__main__":
    # 1. 可选：先跑一次单体测试，确保函数本身没问题
    # run_single_test()
    
    # 2. 运行实时显示的并发测试
    run_concurrency_test_realtime()
