import json
import os
import logging
from typing import Dict, Any, Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
import requests

# --- 配置区 ---
CONFIG = {
    # vLLM API配置 (用于质量评估)
    "VLLM_API_ENDPOINT": "http://127.0.0.1:8000/v1/chat/completions",
    "VLLM_MODEL_NAME": "/mnt/storage/LLM/liangyan/models/trained/lora/merge/qwen3-32b-lora_dpo_aug_reranked_v11_checkpoint-720",
    "VLLM_API_KEY": "EMPTY",
    
    # 输入/输出文件
    "INPUT_FILE": "inference_trajectory.jsonl",  # 轨迹蒸馏的输出
    "OUTPUT_HIGH_QUALITY": "high_quality_trajectory.jsonl",  # 高质量数据
    "OUTPUT_MEDIUM_QUALITY": "medium_quality_trajectory.jsonl",  # 中等质量数据
    "OUTPUT_LOW_QUALITY": "low_quality_trajectory.jsonl",  # 低质量数据
    "OUTPUT_FILTERED": "filtered_trajectory.jsonl",  # 筛选后的数据 (仅high)
    
    # 并发控制
    "MAX_WORKERS": 4,
}

# --- 日志配置 ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# --- 评估系统提示词 ---
SYSTEM_PROMPT_EVALUATOR = """你是一名资深显示技术领域专家。请先仔细思考，然后严格评估以下显示技术相关的问答对是否适合用于监督微调（SFT）的数据集构建。

[问题]：
{question_text}

[推理轨迹]：
{reasoning_text}

[答案]：
{answer_text}

[标准答案]：
{ground_truth_text}

##核心维度，评估需基于以下9个核心维度：

1.  **问题通用性(question_universality)**: 问题是否是针对文本生成的？是否能用文本回答？问题是否具有实际意义？问题是否通用？问题是否特指论文？在问题中是否引用文献或文章自定义的专有名词（确保不读论文也能理解问题含义）？问题是否包含多个子问题（2个及以上）（严格执行）
2.  **回答相关性 (Relevance)**：回答是否精准聚焦问题核心？是否存在答非所问、偏离主题或遗漏关键点？是否答案只是仅引导句未提供实质性内容？答案是否只回答了部分问题（只回答了某一个子问题）？答案是否基于检索的知识回答的？判断专有性分析是否合理？判断全面性分析是否合理？（严格执行）
3.  **逻辑一致性 (Logical_Consistency)**：回答的推理过程是否清晰、连贯、无矛盾？是否存在逻辑跳跃、断裂或自相矛盾？是否存在答案中断？答案是否和问题不相关？答案是否只是泛泛而谈？
4.  **术语使用 (Terminology_Usage)**：专业术语的使用是否准确、恰当、完整？是否存在术语误用、滥用、缺失或概念性错误？
5.  **事实正确性 (Factual_Correctness)**：回答中的技术细节、参数、原理、行业现状等是否符合已知事实和行业共识？是否存在事实性错误或过时信息？
6.  **推理轨迹合理性(Reasoning_Trajectory_Validity)**：推理轨迹中每一跳的查询是否合理？检索到的知识块是否相关？推理过程是否呈现递进关系？是否存在无效检索或重复检索？
7.  **答案与标准答案一致性(Answer_Consistency)**：最终答案是否与标准答案在核心观点、技术要点上一致？是否存在重大偏差？（如果没有标准答案，此维度评为high）
8.  **答案通用性(Answer_Universality)**：答案是否特指论文？答案是否具有通用性？答案包含的通用性分析是否合理？
9.  **答案准确完整性(Answer_Completeness)**：答案是否准确回答了问题？答案是否完整回答了问题（回答了各个子问题）？答案包含的准确性分析是否合理？

##总体质量评分标准：
*   `low`：**存在严重缺陷**（如问题通用性低质量、问题特指论文、问题有多个子问题（2个及以上的子问题）、引用文献或文章自定义的专有名词、明显事实错误、完全偏离主题、逻辑混乱、关键术语错误、答案不完整（仅引导句）未提供实质性内容（答案完整性不过关）、推理轨迹严重不合理、答案与标准答案严重不符、答案中引用文献或文章自定义的专有名词、不读论文不能理解答案含义、专有性分析不合理、答案特指论文、答案不具有通用性、答案包含的通用性分析不合理、答案没有准确回答问题、答案包含的准确性分析不合理），**不适合**用于SFT。
*   `medium`：**存在轻微问题或可优化项**（如部分表述不清、个别术语不严谨、次要逻辑不完美、相关性略有不足、通用性略有不足、推理轨迹有小瑕疵、答案与标准答案略有差异、只回答了部分子问题没有完整回答问题，全面性分析不合理），需修改后方可考虑使用。
*   `high`：**无明显错误**，内容**(问题通用性高质量、问题不是特指论文、问题不包含2个及以上的子问题、问题中未引用文献或文章自定义的专有名词（不读论文也能理解问题含义）（严格执行）、准确、专业、逻辑清晰、紧扣主题、准确且完整地回答了问题（严格执行）、推理轨迹合理且有效、答案与标准答案基本一致、答案具有通用性（不是特指论文）、在答案中未引用文献或文章自定义的专有名词、不读论文也能理解答案含义（严格执行）、专有性分析合理、答案包含的全面性分析合理、答案完整回答了各个子问题、答案包含的准确性分析合理、答案包含的通用性分析合理)**，**适合**直接用于SFT。

##你的任务：
1.  对每个维度进行独立评分 (`high`/`medium`/`low`)。
2.  给出基于9个维度的**总体质量评分** (`high`/`medium`/`low`)，其中若答案完整性不过关（只是仅引导句未提供实质性内容）、推理轨迹严重不合理、答案与标准答案严重不符、答案为无法作答、问题或答案通用性不过关、问题或者答案中引用文献或文章自定义的专有名词（脱离文本无法理解）、问题或答案特指论文、问题包含两个及以上的子问题、答案只回答部分问题（只回答某一个子问题）、答案准确性不足（答案没有准确回答问题），满足其一直接一票否决，判为低质量（严格执行）。
3.  对于评分非`high`的维度，**必须具体指出**存在的问题及其**类型**（例如："术语误用：将'OLED'错误称为'LED'"；"事实错误：声称当前主流Mini-LED背光分区数普遍超过5000区"）。
4.  基于你的专业知识和评估结果，**提供具体、可操作的改进建议**，以提升该问答对的质量。
5.  对于评分`high`的维度，必须说明9个维度的判断情况。（全部通过才能是high）

#输出格式要求(严格遵循JSON):
{{
    "quality_rating": {{
        "overall": "high/medium/low",
        "detailed_scores": {{
            "question_universality": {{"score": "high/medium/low", "issues": ["具体问题描述1", "具体问题描述2", ...]}},
            "relevance": {{"score": "high/medium/low", "issues": []}},
            "logical_consistency": {{"score": "high/medium/low", "issues": [...]}},
            "terminology_usage": {{"score": "high/medium/low", "issues": [...]}},
            "factual_correctness": {{"score": "high/medium/low", "issues": [...]}},
            "reasoning_trajectory_validity": {{"score": "high/medium/low", "issues": [...]}},
            "answer_consistency": {{"score": "high/medium/low", "issues": [...]}},
            "answer_universality": {{"score": "high/medium/low", "issues": [...]}},
            "answer_completeness": {{"score": "high/medium/low", "issues": [...]}}
        }}
    }},
    "improvement_suggestions": ["具体建议1", "具体建议2", ...]
}}
"""

# --- API调用函数 ---
def call_vllm_api(system_prompt: str, user_prompt: str = "", temperature: float = 0.0) -> Optional[str]:
    """
    调用vLLM API进行质量评估
    """
    headers = {
        "Authorization": f"Bearer {CONFIG['VLLM_API_KEY']}",
        "Content-Type": "application/json",
    }
    
    messages = [{"role": "system", "content": system_prompt}]
    if user_prompt:
        messages.append({"role": "user", "content": user_prompt})
    
    payload = {
        "model": CONFIG['VLLM_MODEL_NAME'],
        "messages": messages,
        "temperature": temperature,
    }

    try:
        response = requests.post(
            CONFIG['VLLM_API_ENDPOINT'], 
            headers=headers, 
            json=payload, 
            timeout=3600
        )
        response.raise_for_status()
        
        data = response.json()
        content = data['choices'][0]['message']['content']
        return content.strip()
        
    except requests.exceptions.RequestException as e:
        logging.error(f"vLLM API 调用失败: {e}")
        return None
    except (KeyError, IndexError) as e:
        logging.error(f"vLLM API 响应格式错误: {e}")
        return None

# --- 轨迹格式化函数 ---
def format_trajectory(full_trace: List[Dict]) -> str:
    """
    将推理轨迹格式化为易读的文本
    """
    if not full_trace:
        return "无推理轨迹"
    
    trajectory_text = []
    for hop_data in full_trace:
        hop = hop_data.get("hop", 0)
        knowledge_gap = hop_data.get("knowledge_gap", False)
        reasoning = hop_data.get("reasoning", "无推理")
        query = hop_data.get("query")
        chunks = hop_data.get("retrieved_chunks", [])
        
        trajectory_text.append(f"### 第 {hop} 跳:")
        trajectory_text.append(f"**推理**: {reasoning}")
        
        if knowledge_gap and query:
            trajectory_text.append(f"**查询**: {query}")
            if chunks:
                trajectory_text.append(f"**检索到的知识块数量**: {len(chunks)}")
                # 只显示前3个chunk的摘要
                for i, chunk in enumerate(chunks[:3]):
                    preview = chunk[:100] + "..." if len(chunk) > 100 else chunk
                    trajectory_text.append(f"  - 知识块{i+1}: {preview}")
                if len(chunks) > 3:
                    trajectory_text.append(f"  - ...还有 {len(chunks)-3} 个知识块")
            else:
                trajectory_text.append("**检索结果**: 未检索到相关知识")
        else:
            trajectory_text.append("**决策**: 信息充足，准备生成答案")
        
        trajectory_text.append("")  # 空行分隔
    
    return "\n".join(trajectory_text)

# --- 评估单条轨迹 ---
def evaluate_single_trajectory(trajectory_item: Dict[str, Any]) -> Dict[str, Any]:
    """
    评估单条推理轨迹的质量
    返回: 包含评估结果的字典
    """
    question_id = trajectory_item.get("question_id")
    question = trajectory_item.get("question", "")
    ground_truth = trajectory_item.get("ground_truth_answer", "")
    final_answer = trajectory_item.get("final_model_answer", "")
    full_trace = trajectory_item.get("full_trace", [])
    
    # 格式化推理轨迹
    reasoning_text = format_trajectory(full_trace)
    
    # 构建评估prompt
    evaluation_prompt = SYSTEM_PROMPT_EVALUATOR.format(
        question_text=question,
        reasoning_text=reasoning_text,
        answer_text=final_answer,
        ground_truth_text=ground_truth if ground_truth else "无标准答案"
    )
    
    # 调用vLLM进行评估
    logging.info(f"QID {question_id}: 正在评估质量...")
    response_text = call_vllm_api(evaluation_prompt, temperature=0.3)
    
    if response_text is None:
        logging.error(f"QID {question_id}: 评估API调用失败")
        return {
            "question_id": question_id,
            "evaluation_status": "failed",
            "error": "API调用失败",
            "original_data": trajectory_item
        }
    
    # 解析评估结果
    try:
        # 尝试提取JSON
        json_content = response_text
        
        # 处理markdown代码块
        if "```json" in json_content:
            start = json_content.find("```json") + 7
            end = json_content.rfind("```")
            json_content = json_content[start:end].strip()
        elif "```" in json_content:
            start = json_content.find("```") + 3
            end = json_content.rfind("```")
            json_content = json_content[start:end].strip()
        
        evaluation_result = json.loads(json_content)
        
        # 提取总体评分
        overall_rating = evaluation_result.get("quality_rating", {}).get("overall", "low")
        
        logging.info(f"QID {question_id}: 评估完成，总体评分: {overall_rating}")
        
        return {
            "question_id": question_id,
            "evaluation_status": "success",
            "overall_rating": overall_rating,
            "evaluation_result": evaluation_result,
            "original_data": trajectory_item
        }
        
    except json.JSONDecodeError as e:
        logging.error(f"QID {question_id}: 评估结果JSON解析失败: {e}")
        logging.error(f"原始响应: {response_text[:500]}...")
        return {
            "question_id": question_id,
            "evaluation_status": "parse_error",
            "error": f"JSON解析失败: {e}",
            "raw_response": response_text,
            "original_data": trajectory_item
        }

# --- 主函数 ---
def main():
    """
    主流程：读取轨迹数据，进行质量评估，按质量分类输出
    """
    logging.info("=" * 60)
    logging.info("轨迹质量筛选系统启动")
    logging.info("=" * 60)
    
    # 检查输入文件
    if not os.path.exists(CONFIG['INPUT_FILE']):
        logging.error(f"输入文件不存在: {CONFIG['INPUT_FILE']}")
        logging.info("请先运行轨迹蒸馏.py生成推理轨迹")
        return
    
    # 加载轨迹数据
    logging.info(f"正在加载轨迹数据: {CONFIG['INPUT_FILE']}")
    trajectories = []
    with open(CONFIG['INPUT_FILE'], 'r', encoding='utf-8') as f:
        for line in f:
            try:
                trajectories.append(json.loads(line))
            except json.JSONDecodeError:
                logging.warning(f"跳过无效的JSON行: {line[:50]}...")
    
    logging.info(f"加载了 {len(trajectories)} 条轨迹数据")
    
    if not trajectories:
        logging.warning("没有找到有效的轨迹数据，退出")
        return
    
    # 并发评估
    evaluated_results = []
    
    logging.info(f"开始并发评估 (workers={CONFIG['MAX_WORKERS']})...")
    
    with ThreadPoolExecutor(max_workers=CONFIG['MAX_WORKERS']) as executor:
        future_to_trajectory = {
            executor.submit(evaluate_single_trajectory, traj): traj 
            for traj in trajectories
        }
        
        pbar = tqdm(as_completed(future_to_trajectory), total=len(trajectories), desc="评估轨迹")
        
        for future in pbar:
            try:
                result = future.result()
                evaluated_results.append(result)
            except Exception as e:
                logging.error(f"评估任务执行失败: {e}", exc_info=True)
    
    # 分类统计
    high_quality = []
    medium_quality = []
    low_quality = []
    failed = []
    
    for result in evaluated_results:
        status = result.get("evaluation_status")
        
        if status == "success":
            rating = result.get("overall_rating", "low")
            if rating == "high":
                high_quality.append(result)
            elif rating == "medium":
                medium_quality.append(result)
            else:  # low
                low_quality.append(result)
        else:
            failed.append(result)
    
    # 输出统计
    logging.info("=" * 60)
    logging.info("评估完成，统计结果:")
    logging.info(f"  高质量 (high):   {len(high_quality)} 条 ({len(high_quality)/len(trajectories)*100:.1f}%)")
    logging.info(f"  中等质量 (medium): {len(medium_quality)} 条 ({len(medium_quality)/len(trajectories)*100:.1f}%)")
    logging.info(f"  低质量 (low):    {len(low_quality)} 条 ({len(low_quality)/len(trajectories)*100:.1f}%)")
    logging.info(f"  评估失败:        {len(failed)} 条 ({len(failed)/len(trajectories)*100:.1f}%)")
    logging.info("=" * 60)
    
    # 写入分类文件
    def write_results(results: List[Dict], output_file: str):
        with open(output_file, 'w', encoding='utf-8') as f:
            for result in results:
                f.write(json.dumps(result, ensure_ascii=False) + '\n')
        logging.info(f"已写入 {len(results)} 条数据到: {output_file}")
    
    if high_quality:
        write_results(high_quality, CONFIG['OUTPUT_HIGH_QUALITY'])
    
    if medium_quality:
        write_results(medium_quality, CONFIG['OUTPUT_MEDIUM_QUALITY'])
    
    if low_quality:
        write_results(low_quality, CONFIG['OUTPUT_LOW_QUALITY'])
    
    # 写入筛选后的数据 (仅high)
    if high_quality:
        filtered_data = [result['original_data'] for result in high_quality]
        with open(CONFIG['OUTPUT_FILTERED'], 'w', encoding='utf-8') as f:
            for item in filtered_data:
                f.write(json.dumps(item, ensure_ascii=False) + '\n')
        logging.info(f"已写入 {len(filtered_data)} 条高质量轨迹到: {CONFIG['OUTPUT_FILTERED']}")
    
    logging.info("=" * 60)
    logging.info("质量筛选完成!")
    logging.info(f"建议使用: {CONFIG['OUTPUT_FILTERED']} 作为训练数据")
    logging.info("=" * 60)

if __name__ == "__main__":
    main()
