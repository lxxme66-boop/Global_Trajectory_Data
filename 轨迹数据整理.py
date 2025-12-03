import json
import os
import random
import logging
from typing import List, Dict, Any 

# --- 1. 配置 ---

# !! 您的工具定义 (英文版)
TOOL_DEFINITION_JSON = """
[
    {
        "type": "function",
        "function": {
            "name": "tcl_rag",
            "description": "Searches for information related to the query in the vertical domain knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question to search for. It must be a 【**specific, natural, and complete question**】. This question should be fluent like a human question, and 【**absolutely must not**】 be a 'keyword phrase' separated by spaces."
                    }
                },
                "required": ["query"]
            }
        }
    }
]
"""

# !! 整合后的新 System Prompt
SYSTEM_PROMPT_TEMPLATE = f"""You are a specialized technical assistant. Your sole function is to provide accurate answers by systematically searching a specific internal knowledge base using the available tool.

You must follow an iterative "Think-Act-Review-Answer" reasoning process that may require multiple rounds of tool usage.

# Tools
You have *only one* tool available, named 'tcl_rag'. You must use this tool to answer all user queries that require knowledge base access. Do not attempt to call any other functions.

The tool definition is provided within <tools></tools> XML tags:
<tools>
{TOOL_DEFINITION_JSON}
</tools>

# Output Format and Reasoning Process
You must strictly follow this iterative process:

1. **Initial Thinking:**
   Begin every response with <think> tags containing your reasoning:
   - Analyze the user's query and identify what information is needed
   - Determine if tool usage is required
   - If multiple aspects need investigation, plan your search strategy
   - Formulate specific, natural questions for the tool

   <think>
   Your detailed reasoning here...
   </think>

2. **Tool Call (When needed):**
   If you determine that searching is necessary, immediately follow with:
   
   <tool_call>
   {{"name": "tcl_rag", "arguments": {{"query": "your well-formed query string here"}}}}
   </tool_call>

3. **Review and Iterate:**
   After receiving tool responses, you MUST again engage in thinking:
   - Evaluate whether the retrieved information sufficiently answers the original question
   - Identify any gaps, ambiguities, or new questions that emerged
   - Decide if additional tool calls are needed with refined queries
   - If more information is needed, repeat the planning and tool call steps with refined queries.
   
   Example of iterative thinking after tool response:

   <think>
   The tool returned information about X, but I still need to clarify Y and Z. 
   I should search for more specific information about Y.
   </think>

   <tool_call>
   {{"name": "tcl_rag", "arguments": {{"query": "refined question about Y"}}}}
   </tool_call>

4. **Final Answer (Only when fully prepared):**
   Only when you have gathered ALL necessary information through sufficient tool usage, or if you have exhausted reasonable search attempts:

   <think>
   I have conducted thorough searches and now have complete information to answer the user's question.
   </think>

   <answer>
   Your comprehensive final answer based on all retrieved information.
   </answer>

# Critical Guidelines
- You MUST use multiple tool calls when the problem requires investigating different aspects or when previous results are incomplete.
- Each tool call should build upon previous results - refine your queries based on what you've learned.
- Do not proceed to final answer until you are confident you have exhaustively searched all relevant aspects.
- If previous search results are insufficient, unclear, or raise new questions, you MUST perform additional searches.
"""

SOURCE_FILE = "trajectory.jsonl"  # 您的原始数据文件
TRAIN_FILE = "train_agent_data.jsonl"
EVAL_FILE = "eval_agent_data.jsonl"
EVAL_SPLIT_RATIO = 0.05  # 5% 作为评估集

# --- 2. 核心处理函数 ---

def process_line(line_data: dict) -> dict:
    """
    将单行原始数据转换为 LlamaFactory ShareGPT 格式
    """
    conversations = []
    has_answered = False
    
    # 改进 2: 用于跨轮次去重的 set
    seen_chunks = set()
    
    # 1. 添加 System Prompt
    conversations.append({
        "role": "system",
        "content": SYSTEM_PROMPT_TEMPLATE
    })

    # 2. 添加初始 User Question
    conversations.append({
        "role": "user",
        "content": line_data["question"]
    })

    full_trace = line_data.get("full_trace", [])
    win_answer = line_data["win_answer"]
    last_hop_num = 0

    # 3. 遍历思考/工具调用轨迹
    for hop_data in full_trace:
        hop = hop_data.get("hop", 0)
        last_hop_num = hop  # 记录最后处理的 hop 编号
        reasoning = hop_data["reasoning"]
        knowledge_gap = hop_data["knowledge_gap"]

        if knowledge_gap:
            # === Assistant Turn (Think + Tool Call) ===
            assistant_think = f"<think>\n{reasoning}\n</think>\n\n"
            
            query = hop_data["query"]
            tool_call_dict = {
                "name": "tcl_rag",
                "arguments": {"query": query}
            }
            # 必须使用 ensure_ascii=False 来正确处理中文
            tool_call_json = json.dumps(tool_call_dict, ensure_ascii=False)
            assistant_tool_call = f"<tool_call>{tool_call_json}</tool_call>"
            
            conversations.append({
                "role": "assistant",
                "content": f"{assistant_think}{assistant_tool_call}"
            })
            
            # === Tool Turn (Tool Response) ===
            chunks = hop_data.get("retrieved_chunks", [])
            
            # 改进 2: 过滤掉已见过的 chunks
            new_unique_chunks = []
            for chunk in chunks:
                if chunk not in seen_chunks:
                    new_unique_chunks.append(chunk)
                    seen_chunks.add(chunk)
            
            # 将本轮 *新发现的* 块作为工具返回
            # LlamaFactory 的 qwen3 模板期望工具返回是原始字符串
            # 将去重后的列表转为 JSON 字符串是最好的方式
            tool_response_content = json.dumps(new_unique_chunks, ensure_ascii=False)
            
            conversations.append({
                "role": "tool",
                "content": tool_response_content
            })
            
        else:
            # knowledge_gap == false, 意味着这是最后一轮思考, 准备回答
            final_think = f"<think>\n{reasoning}\n</think>\n\n"
            final_answer = f"<answer>\n{win_answer}\n</answer>\n"
            
            conversations.append({
                "role": "assistant",
                "content": f"{final_think}{final_answer}"
            })
            has_answered = True
            break # 已经回答, 停止处理此轨迹

    # 4. 兜底逻辑 (处理循环正常结束, 但未回答的情况)
    #    这包括: 
    #    a) 轨迹为空
    #    b) 轨迹最后一步是工具调用 (hop 1, 2, 3, 4 或 5)
    if not has_answered:
        
        # 改进 1: 检查最后处理的 hop 是否为 5
        if last_hop_num == 5:
            final_think_content = "当前已达到最大调用工具次数: 5, 我需要整合上述所有信息进行最终回答"

        else:
            final_think_content = "我已收集到足够信息，现在可以整合并回答用户的问题。"
        
        final_think = f"<think>\n{final_think_content}\n</think>\n\n"
        final_answer = f"<answer>\n{win_answer}\n</answer>\n"
        
        conversations.append({
            "role": "assistant",
            "content": f"{final_think}{final_answer}"
        })

    return {"conversations": conversations}

# --- 3. 主执行函数 ---

def main():
    if not os.path.exists(SOURCE_FILE):
        print(f"错误: 找不到源文件 '{SOURCE_FILE}'")
        print(f"请将您的数据文件命名为 '{SOURCE_FILE}' 并放在同一目录")
        return

    print(f"正在从 '{SOURCE_FILE}' 读取数据...")
    processed_lines = []
    with open(SOURCE_FILE, 'r', encoding='utf-8') as f_in:
        for i, line in enumerate(f_in):
            if not line.strip():
                continue
            try:
                line_data = json.loads(line)
                converted_data = process_line(line_data)
                processed_lines.append(converted_data)
            except json.JSONDecodeError:
                print(f"警告: 跳过第 {i+1} 行无效的 JSON: {line[:50]}...")
            except Exception as e:
                print(f"警告: 处理行 {line_data.get('question_id', 'N/A')} (第 {i+1} 行) 失败: {e}")

    print(f"成功转换 {len(processed_lines)} 条数据。")

    # 随机打乱
    random.shuffle(processed_lines)

    # 切分
    split_index = int(len(processed_lines) * (1 - EVAL_SPLIT_RATIO))
    train_data = processed_lines[:split_index]
    eval_data = processed_lines[split_index:]

    print(f"切分数据集: {len(train_data)} (训练) / {len(eval_data)} (评估)")

    try:
        # --- 写入训练集 ---
        
        # [!] 改进: 自动创建父目录 (如果它不存在)
        # os.path.dirname() 获取文件所在的目录
        # os.makedirs() 递归创建所有不存在的中间目录
        # exist_ok=True 确保如果目录已存在, 脚本不会报错
        parent_dir_train = os.path.dirname(TRAIN_FILE)
        if parent_dir_train: # 只有在路径包含目录时才创建 (避免为 "" 创建)
            os.makedirs(parent_dir_train, exist_ok=True)
        
        with open(TRAIN_FILE, 'w', encoding='utf-8') as f_out:
            for item in train_data:
                f_out.write(json.dumps(item, ensure_ascii=False) + '\n')
        
        logging.info(f"成功写入 {len(train_data)} 条训练数据到 {TRAIN_FILE}")

    except Exception as e:
        logging.error(f"写入训练集 {TRAIN_FILE} 失败: {e}", exc_info=True)


    try:
        # --- 写入评估集 ---
        
        # [!] 改进: 自动创建父目录 (如果它不存在)
        parent_dir_eval = os.path.dirname(EVAL_FILE)
        if parent_dir_eval:
            os.makedirs(parent_dir_eval, exist_ok=True)
                
        with open(EVAL_FILE, 'w', encoding='utf-8') as f_out:
            for item in eval_data:
                f_out.write(json.dumps(item, ensure_ascii=False) + '\n')
                
        logging.info(f"成功写入 {len(eval_data)} 条评估数据到 {EVAL_FILE}")

    except Exception as e:
        logging.error(f"写入评估集 {EVAL_FILE} 失败: {e}", exc_info=True)

        print(f"处理完成! 输出文件:")
        print(f"训练集: {TRAIN_FILE}")
        print(f"评估集: {EVAL_FILE}")

if __name__ == "__main__":
    main()