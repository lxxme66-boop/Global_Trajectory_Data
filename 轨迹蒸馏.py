import requests
import json
import os
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from typing import List, Dict, Any, Optional, Tuple # [MODIFIED] Import Tuple

import time
from time import sleep

# --- 配置区 ---
# (CONFIG 字典保持不变)
# ... (您的 CONFIG 字典)
CONFIG = {
    # vLLM (星智大模型) API 端点
    "VLLM_API_ENDPOINT": "http://127.0.0.1:8000/v1/chat/completions",
    "VLLM_MODEL_NAME": "/mnt/storage/LLM/liangyan/models/trained/lora/merge/qwen3-32b-lora_dpo_aug_reranked_v11_checkpoint-720", # 替换为vLLM加载的模型名
    "VLLM_API_KEY": "EMPTY", # vLLM OpenAI-compatible 模式的 API key

    # 输入/输出文件
    "INPUT_FILE": "qa_data.jsonl", # 假设输入是 JSONL 格式, 你的QA数据
    "OUTPUT_FILE": "inference_trajectory.jsonl", # 输出的推理轨迹

    # 管道控制
    "MAX_WORKERS": 4,  # 自定义并发数
    "MAX_HOPS": 5,     # 防止无限循环的最大RAG调用次数

    # 在没有新任务时, 脚本休眠多少秒再次检查
    'POLL_INTERVAL_SECONDS': 60
}


# =============================================================================
# Volcengine 客户端初始化 (来自 TclRag)
# =============================================================================

# --- 从环境变量读取火山引擎凭证 ---
AK = "AKLTOWZhOWQ1ZjAzZTFkNDc2NjhmNTJjMmVkYmQ2NmIzODA"
SK = "TnpsaE5tWTBaV1V4WkRCak5EZGhOVGxsTldOa09UQTNaRGMyWm1JNVlqQQ=="

if not AK or not SK:
    print("⚠️ [警告] 环境变量 VOLCENGINE_ACCESS_KEY 或 VOLCENGINE_SECRET_KEY 未设置")
    print("⚠️ [警告] RAG 工具的翻译和语种检测功能将无法使用")
    g_translate_api_instance = None
    g_lang_detect_service = None
else:
    REGION = "cn-north-1"
    
    try:
        # --- 初始化火山引擎翻译客户端 (新版SDK) ---
        import volcenginesdkcore
        import volcenginesdktranslate20250301
        from volcenginesdkcore.rest import ApiException

        configuration = volcenginesdkcore.Configuration()
        configuration.ak = AK
        configuration.sk = SK
        configuration.region = REGION
        volcenginesdkcore.Configuration.set_default(configuration)
        g_translate_api_instance = volcenginesdktranslate20250301.TRANSLATE20250301Api()
        print("✅ [TclRag 依赖] 火山翻译客户端初始化完成.")

        # --- 初始化火山引擎语种检测客户端 (旧版SDK) ---
        from volcengine.ApiInfo import ApiInfo
        from volcengine.Credentials import Credentials
        from volcengine.ServiceInfo import ServiceInfo
        from volcengine.base.Service import Service

        lang_detect_service_info = ServiceInfo(
            'translate.volcengineapi.com',
            {'Content-Type': 'application/json'},
            Credentials(AK, SK, 'translate', REGION),
            5, 5
        )
        lang_detect_api_info = {
            'langdetect': ApiInfo('POST', '/', {'Action': 'LangDetect', 'Version': '2020-06-01'}, {}, {})
        }
        g_lang_detect_service = Service(lang_detect_service_info, lang_detect_api_info)
        print("✅ [TclRag 依赖] 火山语种检测客户端初始化完成.")

    except ImportError:
        print("❌ [TclRag 依赖错误] 请先安装火山引擎SDK: `pip install volcengine volcenginesdk-translate`")
        g_translate_api_instance = None
        g_lang_detect_service = None
    except Exception as e:
        print(f"❌ [TclRag 依赖错误] 火山客户端初始化失败: {e}")
        g_translate_api_instance = None
        g_lang_detect_service = None

# --- Prompts ---
# (Prompts 保持不变)
SYSTEM_PROMPT_JUDGE = """你是一个“知识缺口分析与查询生成助手”。

你的唯一任务是：
1.  分析给定的[问题]和（可选的）[已检索的上下文]。
2.  判断你基于**你自己的内部知识**加上**已提供的上下文**，是否已经**足够**、**完整**、**准确**地回答这个[问题]。
3.  如果**必须**调用RAG (knowledge_gap: true)，请生成一个【**格式为完整、自然问句**】的搜索查询(query)，这个query应旨在获取你下一步最需要的缺失信息。

注意：你的任务不是回答问题，而是**判断知识是否足够**并**生成下一步的查询**。

你的输出**必须**严格遵循以下JSON格式：
{
  "knowledge_gap": true | false,
  "reasoning": "解释你为什么认为存在/不存在知识缺口。如果存在 (true)，请具体说明你还缺少哪些关键信息或需要验证哪些事实。",
  "query": "如果 knowledge_gap 为 true，请在此处生成一个【**具体的、自然的、完整的问句**】。这个问句应该像人类提问一样通顺，**绝对禁止**使用空格分割的“关键词短语”。如果 knowledge_gap 为 false，请将此值设为 null。"
}"""

USER_PROMPT_TEMPLATE_JUDGE = """[问题]：
"{question}"

[已检索的上下文]：
{context}

[你的任务]：
请严格按照你被设定的JSON格式进行分析。"""

SYSTEM_PROMPT_ANSWERER = """你是一个乐于助人的AI助手。"""

USER_PROMPT_ANSWERER = """{{
  "instruction":"你是一个半导体显示领域的资深专家，你掌握TFT、OLED、LCD、QLED、EE、Design等显示半导体显示领域内的相关知识。请根据输入中的切片信息和问题进行回答。切片信息是可能相关的资料，切片信息的内容庞杂，不一定会包含目标答案，可能含有与问题相近的干扰信息，请仔细阅读每个切片后再作答，不得出现错误。"
  "input": {{
    "context": "{context}"
    "question": "{question}"
  }},
  "output": {{
    "answer": "根据切片中提供的有效信息和自身知识对问题进行详尽的回答，推荐分点回答格式。"
  }},
  "requirements": {{
    "criteria": "根据提供的切片信息提取有效信息，同时结合自身已有的半导体显示知识进行完整、准确的回答",
    "format": "1、输出内容必须用中文作答且有逻辑条理性；2、输出内容不要显示引用切片，与模型原生回答格式一致。"
  }}
}}"""


# --- 日志配置 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


# --- API 调用层 (解耦) ---

# =============================================================================
# RAG 工具辅助函数 (来自 TclRag)
# =============================================================================
def _detect_language(text: str) -> str:
    # (函数保持不变)
    if not g_lang_detect_service or not text:
        return ""
    try:
        body = {'TextList': [text]}
        res = g_lang_detect_service.json('langdetect', {}, json.dumps(body))
        res_json = json.loads(res)
        return res_json['DetectedLanguageList'][0]['Language']
    except Exception as e:
        logging.error(f"❌ [RAG Tool Error] 语种检测失败: {e}")
        return ""

def _translate_text(text: str, target_language: str) -> str:
    # (函数保持不变)
    if not g_translate_api_instance or not text:
        return ""
    request = volcenginesdktranslate20250301.TranslateTextRequest(
        target_language=target_language,
        text_list=[text]
    )
    try:
        response = g_translate_api_instance.translate_text(request)
        if response and response.translation_list:
            return response.translation_list[0].translation
        return ""
    except ApiException as e:
        logging.error(f"❌ [RAG Tool Error] 文本翻译API异常: {e}")
        return ""

def _search_rag_api(query: str, top_doc_num: int = 5) -> Optional[list]: # [MODIFIED] 返回类型
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
        # [MODIFIED] 返回 None 来信号失败
        return None 
def _merge_and_deduplicate_results(results1: list, results2: list) -> list:
    """
    合并两个RAG检索结果列表, 并根据 'chunk' (text) 内容进行去重
    返回: [final_sources, final_chunks, final_chunk_indices]
    """
    try:
        source_list1 = results1[1] if results1 and len(results1) > 1 and isinstance(results1[1], list) else []
        text_list1 = results1[2] if results1 and len(results1) > 2 and isinstance(results1[2], list) else []
        index_list1 = results1[3] if results1 and len(results1) > 3 and isinstance(results1[3], list) else []
    except (TypeError, IndexError):
        source_list1, text_list1, index_list1 = [], [], []
    try:
        source_list2 = results2[1] if results2 and len(results2) > 1 and isinstance(results2[1], list) else []
        text_list2 = results2[2] if results2 and len(results2) > 2 and isinstance(results2[2], list) else []
        index_list2 = results2[3] if results2 and len(results2) > 3 and isinstance(results2[3], list) else []
    except (TypeError, IndexError):
        source_list2, text_list2, index_list2 = [], [], []
    all_sources = source_list1 + source_list2
    all_chunks = text_list1 + text_list2
    all_indices = index_list1 + index_list2
    seen_chunks = set()
    final_sources = []
    final_chunks = []
    final_chunk_indices = []
    for source, chunk, index in zip(all_sources, all_chunks, all_indices):
        if chunk and isinstance(chunk, str) and chunk not in seen_chunks:
            seen_chunks.add(chunk)
            final_sources.append(source)
            final_chunks.append(chunk)
            final_chunk_indices.append(index)
    return [final_sources, final_chunks, final_chunk_indices]

def call_vllm_api(system_prompt: str, user_prompt: str, temperature: float = 0.0) -> Optional[str]:
    # (函数保持不变)
    headers = {
        "Authorization": f"Bearer {CONFIG['VLLM_API_KEY']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": CONFIG['VLLM_MODEL_NAME'],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": temperature,
        # "max_tokens": max_tokens,
    }

    try:
        response = requests.post(CONFIG['VLLM_API_ENDPOINT'], headers=headers, json=payload, timeout=3600)
        response.raise_for_status() 
        
        data = response.json()
        content = data['choices'][0]['message']['content']
        return content.strip()
        
    except requests.exceptions.RequestException as e:
        logging.error(f"vLLM API 调用失败: {e}")
        return None
    except (KeyError, IndexError) as e:
        logging.error(f"vLLM API 响应格式错误: {e} - 响应: {response.text}")
        return None

def call_rag_tool(query: str) -> List[str]:
    """
    调用外部 RAG 接口
    此版本实现了混合搜索 (中英) 与去重逻辑 (基于 TclRag)
    返回:
        List[str]: 去重后的相关文本块 (chunks) 列表
    """
    
    # --- 参数检查 ---
    if not query or not isinstance(query, str):
        logging.warning(f"[RAG Tool] RAG 查询为空或格式不正确: {query}")
        return []
    
    try:
        # 去掉空字符, 并添加问号
        query = query.rstrip()
        if not query.endswith(('?', '？')):
                # 3. 如果不是, 则添加中文问号
                query =  query + '？'
        
        logging.info(f"🚀 [RAG Tool] 开始处理查询: '{query}'")
        
        # 步骤1: 检测语种
        lang = _detect_language(query)
        logging.info(f"🔍 [RAG Tool] 检测到语种: {lang}")
        
        original_query = query
        translated_query = ""
        
        # 步骤2: 根据语种进行翻译
        if lang == 'zh':
            translated_query = _translate_text(original_query, "en")
            logging.info(f"🌐 [RAG Tool] 中文 -> 英文: '{translated_query}'")
        elif lang == 'en':
            translated_query = _translate_text(original_query, "zh")
            logging.info(f"🌐 [RAG Tool] 英文 -> 中文: '{translated_query}'")
        else:
            logging.warning(f"⚠️ [RAG Tool] 未知或不支持的语种 ({lang}), 将只使用原文进行检索.")
            translated_query = original_query # 仍需初始化, 否则 future_translated 会失败
        
        # 如果翻译失败, translated_query 可能是空字符串.
        if not translated_query:
            logging.warning(f"⚠️ [RAG Tool] 翻译失败，将使用原文进行两次检索.")
            translated_query = original_query # 保险起见, 用原文搜两次 (合并时会去重)

        # 步骤3: 并行调用RAG接口
        logging.info("⏳ [RAG Tool] 正在并行发起原文和译文的RAG检索...")
        
        results_original = None
        results_translated = None

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_original = executor.submit(_search_rag_api, original_query)
            future_translated = executor.submit(_search_rag_api, translated_query)
            
            results_original = future_original.result()
            logging.info("✅ [RAG Tool] 原文检索完成.")
            results_translated = future_translated.result()
            logging.info("✅ [RAG Tool] 译文检索完成.")

        # 步骤4: 合并与去重
        logging.info("🔄 [RAG Tool] 正在合并与去重结果...")
        # final_result 格式: [final_sources, final_chunks, final_chunk_indices]
        final_result = _merge_and_deduplicate_results(results_original, results_translated)
        
        # 步骤5: 格式化输出 (返回 List[str])
        # final_result[1] 是 final_chunks 列表
        final_chunks = final_result[1] if final_result and len(final_result) > 1 else []
        
        if not final_chunks:
            logging.warning(f"⚠️ [RAG Tool] 未找到查询 '{query}' 的相关结果.")
            return [] # 返回空列表

        logging.info(f"✅ [RAG Tool] 处理完成, 返回 {len(final_chunks)} 个去重后的 chunks.")
        
        # 返回原始的 chunk 文本列表
        return final_chunks 
    
    except Exception as e:
        logging.error(f"❌ [RAG Tool] 执行混合搜索工作流时发生未知错误: {e}", exc_info=True)
        return None # 明确信号失败

# --- 辅助函数 ---

def load_input_data(filepath: str) -> List[Dict[str, Any]]:
    # (函数保持不变)
    if not os.path.exists(filepath):
        logging.error(f"输入文件未找到: {filepath}")
        return []
    
    data = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError:
                logging.warning(f"跳过格式错误的行: {line}")
    return data

def load_processed_ids(filepath: str) -> set:
    # (函数保持不变)
    processed_ids = set()
    if not os.path.exists(filepath):
        return processed_ids 
        
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                data = json.loads(line)
                # [MODIFIED] 增加一个检查, 确保我们只加载 *成功* 完成的任务ID
                # 我们通过 "failure_reason" 字段是否存在来判断
                if 'question_id' in data and 'failure_reason' not in data:
                    processed_ids.add(data['question_id'])
            except json.JSONDecodeError:
                continue 
    return processed_ids

def format_chunks_for_prompt(chunks: List[str]) -> str:
    # (函数保持不变)
    if not chunks:
        return "未检索到任何信息. "
    
    return "\n\n".join(f"[检索到的知识块 {i+1}]:\n{chunk}" for i, chunk in enumerate(chunks))

# --- [NEW] 辅助函数: 用于构建失败时的部分结果 ---
def _build_partial_result(qa_item: Dict, trace: List, error_message: str, rag_count: int, seen_chunks: set) -> Dict[str, Any]:
    """
    [MODIFIED] 当任务在
    中途失败时, 构造一个用于保存的部分结果.
    """
    logging.warning(f"QID {qa_item.get('question_id')}: {error_message}. 正在保存部分轨迹.")
    return {
        "question_id": qa_item.get("question_id"),
        "question": qa_item.get("question"),
        "ground_truth_answer": qa_item.get("answer"),
        "is_multi_hop": (rag_count >= 2), # 记录失败前的状态
        "rag_call_count": rag_count,
        "final_model_answer": f"推理失败: {error_message}", # 明确记录失败
        "failure_reason": error_message, # [MODIFIED] 添加一个明确的失败字段
        "full_trace": trace # 保存失败前的轨迹
    }

# --- 核心处理逻辑 ---

def process_single_question(qa_item: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]: # [MODIFIED] 返回类型
    """
    [MODIFIED] 处理单个 QA 对, 执行完整的迭代推理.
    返回 (is_success: bool, result_data: dict)
    """
    question_id = qa_item.get("question_id")
    question = qa_item.get("question")
    
    if not question_id or not question:
        logging.warning(f"跳过缺少 'question_id' 或 'question' 的项: {qa_item}")
        # 认为这种"成功"跳过, 不再重试
        return True, {"question_id": question_id, "failure_reason": "Skipped: Missing QID or Question"}

    trace = []
    accumulated_context_str = "无"
    rag_call_count = 0
    seen_chunks_set = set()

    for hop in range(1, CONFIG['MAX_HOPS'] + 1):
        
        user_prompt_judge = USER_PROMPT_TEMPLATE_JUDGE.format(
            question=question, 
            context=accumulated_context_str
        )
        
        response_text = call_vllm_api(SYSTEM_PROMPT_JUDGE, user_prompt_judge, temperature=0.7)
        
        # [MODIFIED] 失败处理
        if response_text is None:
            error_msg = f"Judge API 调用在第 {hop} 跳失败"
            return False, _build_partial_result(qa_item, trace, error_msg, rag_call_count, seen_chunks_set)

        try:
            json_content_str = response_text
            end_tag = "</think>"
            end_tag_index = response_text.rfind(end_tag) 
            
            if end_tag_index != -1:
                json_content_str = response_text[end_tag_index + len(end_tag):].strip()
            else:
                json_content_str = response_text.strip()

            if json_content_str.startswith("```json"):
                json_content_str = json_content_str[7:-3].strip()
            elif json_content_str.startswith("`json"):
                json_content_str = json_content_str[5:-1].strip()
            elif json_content_str.startswith("`"):
                json_content_str = json_content_str.strip('`')

            if not json_content_str:
                raise json.JSONDecodeError("提取的 JSON 字符串为空", json_content_str, 0)
            
            judge_output = json.loads(json_content_str)
            
            knowledge_gap = judge_output.get("knowledge_gap", False)
            reasoning = judge_output.get("reasoning", "解析 reasoning 失败")
            query = judge_output.get("query")

        except json.JSONDecodeError as e:
            logging.error(f"QID {question_id}: Judge JSON 解析在第 {hop} 跳失败. 错误: {e}")
            logging.error(f"QID {question_id}: --- 原始完整输出 ---\n{response_text}")
            error_msg = f"Judge JSON 解析在第 {hop} 跳失败: {e}"
            return False, _build_partial_result(qa_item, trace, error_msg, rag_call_count, seen_chunks_set)

        hop_record = {
            "hop": hop,
            "knowledge_gap": knowledge_gap,
            "reasoning": reasoning,
            "query": query,
            "retrieved_chunks": None 
        }

        if knowledge_gap and query:
            rag_call_count += 1
            chunks = call_rag_tool(query)
            
            # [MODIFIED] 检查 RAG 工具的显式失败
            if chunks is None:
                hop_record["retrieved_chunks"] = None
                trace.append(hop_record)
                error_msg = f"RAG 工具在第 {hop} 跳失败"
                return False, _build_partial_result(qa_item, trace, error_msg, rag_call_count, seen_chunks_set)
            
            hop_record["retrieved_chunks"] = chunks
            
            unique_new_chunks = []
            if chunks: 
                for chunk in chunks:
                    if chunk and isinstance(chunk, str) and chunk not in seen_chunks_set:
                        unique_new_chunks.append(chunk)
                        seen_chunks_set.add(chunk)
            
            logging.info(f"QID {question_id}: Hop {hop}: RAG 返回 {len(chunks)} chunks, {len(unique_new_chunks)} 个是新的.")

            if unique_new_chunks:
                new_context_str = format_chunks_for_prompt(unique_new_chunks)
                if accumulated_context_str == "无":
                    accumulated_context_str = new_context_str
                else:
                    accumulated_context_str += f"\n\n---\n[第 {hop} 轮 RAG 检索结果]:\n{new_context_str}"
            else:
                if chunks:
                    logging.info(f"QID {question_id}: Hop {hop}: RAG 成功但未找到新 chunk, 上下文未更新.")
            
            trace.append(hop_record)
            
        else:
            trace.append(hop_record)
            break 
            
        if hop == CONFIG['MAX_HOPS']:
             logging.warning(f"QID {question_id}: 达到最大跳数 {CONFIG['MAX_HOPS']}, 强制停止. ")

    # --- 迭代结束, 生成最终答案 ---
    
    user_prompt_ans = USER_PROMPT_ANSWERER.format(
        question=question,
        context=accumulated_context_str
    )

    print(f"✅ QID {question_id}: 迭代结束, 生成最终答案.")
    
    final_answer = call_vllm_api(SYSTEM_PROMPT_ANSWERER, user_prompt_ans, temperature=0.7)
    
    # [MODIFIED] 失败处理
    if final_answer is None:
        error_msg = "最终答案生成失败 (API 调用失败)"
        return False, _build_partial_result(qa_item, trace, error_msg, rag_call_count, seen_chunks_set)

    # --- [MODIFIED] 构造 *成功* 结果 ---
    result = {
        "question_id": question_id,
        "question": question,
        "ground_truth_answer": qa_item.get("answer"),
        "is_multi_hop": (rag_call_count >= 2),
        "rag_call_count": rag_call_count,
        "final_model_answer": final_answer,
        "full_trace": trace
    }
    
    # [MODIFIED] 返回成功信号和完整结果
    return True, result

# --- 主函数 (守护进程模式) ---

def main():
    """
    [MODIFIED] 主执行函数 (守护进程模式)
    将持续监视 INPUT_FILE, 处理新增的 question_id.
    失败的任务将被记录 (部分轨迹), 并在下次循环时自动重试.
    """
    logging.info("--- 管道开始运行 (守护+重试模式) ---")
    logging.info("按 Ctrl+C 停止运行.")
    
    os.makedirs(os.path.dirname(CONFIG['OUTPUT_FILE']), exist_ok=True)
    
    # [MODIFIED] processed_ids 只存储 *成功* 完成的任务
    try:
        processed_ids = load_processed_ids(CONFIG['OUTPUT_FILE'])
        if processed_ids:
            logging.info(f"已加载 {len(processed_ids)} 个 *成功* 处理的 ID. 失败的任务将被重试.")
        else:
            logging.info("未发现已成功处理的任务, 将从头开始. ")
    except Exception as e:
        logging.error(f"加载已处理 ID 时发生致命错误: {e}. 退出.")
        return

    # --------------------------------------------------
    # --- 启动主循环 ---
    # --------------------------------------------------
    try:
        while True:
            # 1. 加载 *当前* 的输入数据
            try:
                all_tasks = load_input_data(CONFIG['INPUT_FILE'])
            except Exception as e:
                logging.error(f"加载输入文件 {CONFIG['INPUT_FILE']} 失败: {e}. {CONFIG['POLL_INTERVAL_SECONDS']}秒后重试...")
                time.sleep(CONFIG['POLL_INTERVAL_SECONDS'])
                continue 

            if not all_tasks:
                logging.info(f"输入文件 {CONFIG['INPUT_FILE']} 为空. {CONFIG['POLL_INTERVAL_SECONDS']}秒后重试...")
                time.sleep(CONFIG['POLL_INTERVAL_SECONDS'])
                continue

            # 2. 筛选 *新* 任务 (关键: 与内存中的 *成功* ID 集合比较)
            tasks_to_run = [
                task for task in all_tasks 
                if task.get("question_id") is not None and task.get("question_id") not in processed_ids
            ]
            
            if not tasks_to_run:
                logging.info(f"未发现新任务或待重试任务. {CONFIG['POLL_INTERVAL_SECONDS']}秒后再次检查... (当前已成功 {len(processed_ids)} 个)")
                time.sleep(CONFIG['POLL_INTERVAL_SECONDS'])
                continue
                
            logging.info(f"总任务数: {len(all_tasks)}, 发现新/待重试任务: {len(tasks_to_run)}")
            
            # [MODIFIED] 移除 processed_ids.update(new_ids_found)
            # ID 只在 *成功* 后才添加

            # 3. 执行并发处理
            new_tasks_succeeded_this_run = 0
            new_tasks_failed_this_run = 0
            
            # [MODIFIED] 打开文件的方式改变
            # 我们需要一个临时的 'w' (写入) 文件, 在一轮结束后, 再和旧的 'a' (追加) 文件合并
            # 这是为了确保重试逻辑正确, 我们只保留每个 QID 的最新状态
            
            # 简单起见, 我们先保持 'a' 模式, 但这意味着输出文件会包含失败和成功的 *所有* 尝试
            # 我们可以通过 load_processed_ids 的逻辑来处理这个问题 (它只加载没有 'failure_reason' 的)
            
            with open(CONFIG['OUTPUT_FILE'], 'a', encoding='utf-8') as f_out:
                
                with ThreadPoolExecutor(max_workers=CONFIG['MAX_WORKERS']) as executor:
                    
                    future_to_task = {
                        executor.submit(process_single_question, task): task 
                        for task in tasks_to_run
                    }
                    
                    pbar = tqdm(as_completed(future_to_task), total=len(tasks_to_run), desc="处理新 QA")
                    
                    for future in pbar:
                        task = future_to_task[future]
                        qid = task.get('question_id')
                        
                        try:
                            # [MODIFIED] 接收 (is_success, result_data) 元组
                            is_success, result_data = future.result()
                            
                            if is_success:
                                # --- 成功路径 ---
                                logging.info(f"QID {qid}: 任务成功完成.")
                                f_out.write(json.dumps(result_data, ensure_ascii=False) + "\n")
                                f_out.flush()
                                new_tasks_succeeded_this_run += 1
                                # 关键: 添加到内存中的已处理集合
                                processed_ids.add(qid) 
                            else:
                                # --- 失败路径 (Req 1 & 2) ---
                                error_msg = result_data.get('failure_reason', 'Unknown error')
                                logging.warning(f"QID {qid}: 任务失败 ({error_msg}). 部分轨迹已保存. 将在下一周期重试.")
                                f_out.write(json.dumps(result_data, ensure_ascii=False) + "\n")
                                f_out.flush()
                                new_tasks_failed_this_run += 1
                                # 关键: *不* 添加到 processed_ids, 以便重试
                                        
                        except Exception as e:
                            # --- 灾难性失败路径 ---
                            logging.error(f"处理 QID {qid} 时发生严重并发错误: {e}", exc_info=True)
                            new_tasks_failed_this_run += 1
                            # 关键: *不* 添加到 processed_ids, 以便重试
            
            logging.info(f"--- 本轮处理完毕 ---")
            logging.info(f"  成功: {new_tasks_succeeded_this_run}")
            logging.info(f"  失败 (将重试): {new_tasks_failed_this_run}")
            logging.info(f"5秒后开始下一轮检查...")
            time.sleep(5) # 在两轮处理之间短暂休眠
    
    except KeyboardInterrupt:
        logging.info("\n--- 检测到 Ctrl+C, 正在停止运行 ---")
        logging.info("等待当前正在执行的任务完成...")
    
    finally:
        logging.info(f"--- 管道运行结束 (总共 *成功* 处理了 {len(processed_ids)} 个唯一ID) ---")


if __name__ == "__main__":
    main()