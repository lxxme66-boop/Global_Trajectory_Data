import time
import json
import requests
from time import sleep

# =============================================================================
# RAG API 调用函数
# =============================================================================

def _search_rag_api(query: str, top_doc_num: int = 5) -> dict:
    """
    RAG API调用函数，返回格式化的字典结果
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
        
        sleep(3)  # 控制访问频率
        
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
        print(f"❌ [RAG API Error] 请求失败: {e}")
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
# 串行处理主逻辑（逐一调用）
# =============================================================================
def process_questions_sequentially():
    """
    从文件中读取问题，逐一调用RAG API，并保存有效结果
    """
    # --- 配置参数 ---
    INPUT_FILE = "sampled_data.jsonl"
    OUTPUT_FILE = "rag.jsonl"
    TARGET_COLUMN = 'question'
    # ----------------

    print("="*60)
    print("开始处理问题...")
    print(f"输入文件: {INPUT_FILE}")
    print(f"输出文件: {OUTPUT_FILE}")
    print("="*60)

    # 1. 读取输入文件
    questions = []
    try:
        with open(INPUT_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    question = data.get(TARGET_COLUMN)
                    if question:
                        questions.append(question)
    except FileNotFoundError:
        print(f"❌ 找不到输入文件: {INPUT_FILE}")
        return
    except Exception as e:
        print(f"❌ 读取输入文件失败: {e}")
        return
    
    if not questions:
        print("❌ 没有找到任何问题")
        return
    
    total_count = len(questions)
    print(f"\n总共读取到 {total_count} 个问题\n")

    # 2. 逐一处理并保存结果
    success_count = 0
    error_count = 0
    start_time = time.time()

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as output_file:
        for idx, question in enumerate(questions):
            # 打印当前进度
            print(f"[{idx+1}/{total_count}] 正在处理问题...")
            print(f"问题: {question[:80]}..." if len(question) > 80 else f"问题: {question}")
            
            # 调用 RAG API
            result = _search_rag_api(query=question)
            
            # 判断是否成功
            if result['success'] and result['doc_num'] > 0:
                success_count += 1
                
                # 保存到文件
                output_data = {
                    'index': idx,
                    'query': question,
                    'information': result['information'],
                    'sources': result['sources'],
                    'chunks': result['chunks'],
                    'doc_num': result['doc_num']
                }
                output_file.write(json.dumps(output_data, ensure_ascii=False) + '\n')
                output_file.flush()  # 立即写入磁盘
                
                print(f"✅ 成功! 返回 {result['doc_num']} 个文档")
            else:
                error_count += 1
                error_msg = result.get('error', '无返回结果')
                print(f"❌ 失败: {error_msg}")
            
            # 计算进度统计
            elapsed_time = time.time() - start_time
            avg_time_per_request = elapsed_time / (idx + 1)
            remaining_requests = total_count - (idx + 1)
            eta = avg_time_per_request * remaining_requests
            
            print(f"进度: {idx+1}/{total_count} ({(idx+1)/total_count*100:.1f}%) | "
                  f"成功: {success_count} | 失败: {error_count} | "
                  f"已用时: {elapsed_time:.1f}s | 预计剩余: {eta:.1f}s")
            print("-" * 60)
    
    # 3. 打印最终统计
    end_time = time.time()
    total_time = end_time - start_time
    
    print("\n" + "="*60)
    print("--- 处理完成 ---")
    print(f"总耗时: {total_time:.2f} 秒 ({total_time/60:.2f} 分钟)")
    print(f"总问题数: {total_count}")
    print(f"成功: {success_count}")
    print(f"失败: {error_count}")
    print(f"成功率: {success_count/total_count*100:.1f}%")
    print(f"输出文件: {OUTPUT_FILE}")
    if total_time > 0:
        print(f"平均处理速度: {total_count/total_time:.2f} 问题/秒")
    print("="*60)


# =============================================================================
# 单次测试函数
# =============================================================================
def run_single_test():
    """运行单次测试，确保API调用正常"""
    print("--- 运行单次测试 ---")
    query = "在IGZO TFT中，环境气氛中的氧气是如何影响TFT的阈值电压的？"
    try:
        result = _search_rag_api(query=query)
        if result['success']:
            print("✅ 单次调用成功!")
            print(f"文档数量: {result['doc_num']}")
            print(f"\n{result['information']}")
        else:
            print(f"❌ 单次调用失败: {result.get('error', 'Unknown error')}")
    except Exception as e:
        print(f"❌ 单次调用异常: {e}")
    print("="*60 + "\n")


# =============================================================================
# 主程序入口
# =============================================================================
if __name__ == "__main__":
    # 1. 先跑一次单体测试
    run_single_test()
    
    # 2. 逐一处理所有问题
    process_questions_sequentially()
