import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import requests
from time import sleep

# =============================================================================
# 步骤 1: search_answer_online 函数
# =============================================================================

def search_answer_online(query: str, top_doc_num: int = 5):

    url = "http://218.104.107.132:5002/mongodb"
    # url = "http://218.104.107.132:5002/rag_9588/"

    # 设置 HTTP 请求头
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length':'<calculated when request is sent>',
        'Accept-Encoding':'gzip, deflate, br'
    }

    # POST 请求体, 包含查询信息和返回数量
    sent_data = {'id': 102, 
                 'top_doc_num': top_doc_num,
                 'query': query}

    # 向后端服务发送 POST 请求, 设置超时为1小时, 禁用 SSL 证书验证
    res_json = requests.post(url, verify=False, headers=headers, data=sent_data,  timeout=(3600, 3600)).content.decode('utf-8')

    # 解析返回的 JSON 数据
    res_data = json.loads(res_json).get('data') # 提取 data 字段
    res_num = res_data.get('doc_num')

    results = []
    for i in range(res_num):
        ans = res_data.get('arr')[i]

        results.append([ans['ans_id'],ans['title'],ans['text'],ans['index'], ans['score']])

    if res_num < 5:
        for i in range(5-res_num):
            results.append(['null']*4+[0.0])

    # 控制访问频率 (避免频繁请求), 等待3秒
    sleep(3)

    return results


def _search_rag_api(query: str, top_doc_num: int = 5) -> dict:
    """
    改进版RAG API调用函数，返回格式化的字典结果
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
        sources = []
        chunks = []
        
        for i in range(res_num):
            ans = res_data.get('arr', [])[i]
            ans_id = ans.get('ans_id')
            title = ans.get('title')
            text = ans.get('text')
            index = ans.get('index')
            score = ans.get('score')
            
            results.append([ans_id, title, text, index, score])
            sources.append(title)
            chunks.append(text)
        
        # 填充到指定数量
        padding_needed = top_doc_num - len(results)
        if padding_needed > 0:
            results.extend([['null', 'null', 'null', 'null', 0.0]] * padding_needed)
            sources.extend(['null'] * padding_needed)
            chunks.extend(['null'] * padding_needed)
        
        sleep(3)
        
        # 构造 information 字符串
        information = "<information>\n" + \
            "".join([f"chunk:{i},source:{sources[i]}\n{chunks[i]}\n" 
                     for i in range(len(chunks)) if chunks[i] != 'null']) + \
            "</information>\n"
        
        return {
            'query': query,
            'success': True,
            'results': results,
            'sources': sources,
            'chunks': chunks,
            'information': information,
            'doc_num': res_num
        }
        
    except requests.exceptions.RequestException as e:
        print(f"❌ [TclRag Tool Error] RAG接口请求失败: {e}")
        return {
            'query': query,
            'success': False,
            'error': str(e),
            'results': [['null', 'null', 'null', 'null', 0.0]] * top_doc_num,
            'sources': ['null'] * top_doc_num,
            'chunks': ['null'] * top_doc_num,
            'information': '',
            'doc_num': 0
        }


# =============================================================================
# 步骤 2: 并发测试的主逻辑（带进度显示和文件输出）
# =============================================================================
def run_concurrency_test():
    # --- 可配置的测试参数 ---
    TOTAL_REQUESTS = 512      # 总共要发起的请求数
    MAX_CONCURRENCY = 16     # 最大并发数 (同时运行的线程数)
    OUTPUT_FILE = "rag.jsonl"  # 输出文件名
    # -------------------------

    print(f"开始并发测试: 总请求数={TOTAL_REQUESTS}, 最大并发数={MAX_CONCURRENCY}")
    print(f"输出文件: {OUTPUT_FILE}")

    # 准备测试数据
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
        print(f"❌ 找不到输入文件: {filename}")
        return
    except Exception as e:
        print(f"❌ 读取输入文件失败: {e}")
        return
    
    if not test_queries:
        print("❌ 没有找到任何查询数据")
        return
    
    # 限制请求数量
    test_queries = test_queries[:TOTAL_REQUESTS]
    actual_total = len(test_queries)
    
    print(f"实际处理的查询数: {actual_total}")

    success_count = 0
    error_count = 0
    completed_count = 0
    
    # 用于线程安全地写文件和更新计数器
    file_lock = Lock()
    counter_lock = Lock()
    
    start_time = time.time()

    # 打开输出文件（追加模式）
    output_file = open(OUTPUT_FILE, 'w', encoding='utf-8')
    
    try:
        # 创建线程池
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY) as executor:
            # 提交所有任务
            future_to_query = {
                executor.submit(_search_rag_api, query): (idx, query) 
                for idx, query in enumerate(test_queries)
            }

            # 使用 as_completed 来在任务完成时立即处理结果
            for future in as_completed(future_to_query):
                idx, query = future_to_query[future]
                
                try:
                    # 获取任务的返回结果
                    result = future.result()
                    
                    # 更新计数器
                    with counter_lock:
                        completed_count += 1
                        
                        if result['success'] and result['doc_num'] > 0:
                            success_count += 1
                            
                            # 写入文件（只写入成功且有结果的）
                            with file_lock:
                                output_data = {
                                    'index': idx,
                                    'query': query,
                                    'information': result['information'],
                                    'sources': result['sources'],
                                    'chunks': result['chunks'],
                                    'doc_num': result['doc_num']
                                }
                                output_file.write(json.dumps(output_data, ensure_ascii=False) + '\n')
                                output_file.flush()  # 立即写入磁盘
                        else:
                            error_count += 1
                        
                        # 打印进度（每完成一个任务都打印）
                        progress_percent = (completed_count / actual_total) * 100
                        elapsed_time = time.time() - start_time
                        avg_time_per_request = elapsed_time / completed_count if completed_count > 0 else 0
                        eta = avg_time_per_request * (actual_total - completed_count)
                        
                        print(f"\r进度: {completed_count}/{actual_total} ({progress_percent:.1f}%) | "
                              f"成功: {success_count} | 失败: {error_count} | "
                              f"用时: {elapsed_time:.1f}s | 预计剩余: {eta:.1f}s", 
                              end='', flush=True)
                    
                except Exception as exc:
                    # 如果函数在执行过程中抛出异常
                    with counter_lock:
                        completed_count += 1
                        error_count += 1
                        print(f"\n❌ 任务异常: 查询#{idx} 生成了异常: {exc}")
        
        print()  # 换行
        
    finally:
        output_file.close()
    
    end_time = time.time()
    total_time = end_time - start_time
    
    # --- 步骤 4: 打印测试结果 ---
    print("\n" + "="*60)
    print("--- 测试结果 ---")
    print(f"总耗时: {total_time:.2f} 秒")
    print(f"成功请求: {success_count}")
    print(f"失败请求: {error_count}")
    print(f"输出文件: {OUTPUT_FILE}")

    if total_time > 0:
        # 计算 QPS (每秒查询率)
        qps = completed_count / total_time
        print(f"实际 QPS (Queries Per Second): {qps:.2f}")
        print(f"有效 QPS (成功请求): {success_count / total_time:.2f}")
    print("="*60)


# 运行您原来的主逻辑, 确保单次调用是正常的
def run_single_test():
    print("--- 运行单次测试 ---")
    query = "在IGZO TFT中，环境气氛中的氧气是如何影响TFT的阈值电压的？"
    try:
        result = _search_rag_api(query=query)
        if result['success']:
            print("✅ 单次调用成功!")
            print(f"文档数量: {result['doc_num']}")
            print(result['information'])
        else:
            print(f"❌ 单次调用失败: {result.get('error', 'Unknown error')}")
    except Exception as e:
        print(f"❌ 单次调用失败: {e}")
    print("="*60 + "\n")


if __name__ == "__main__":
    # 1. 先跑一次单体测试, 确保函数本身没问题
    run_single_test()
    
    # 2. 再进行并发测试
    run_concurrency_test()
