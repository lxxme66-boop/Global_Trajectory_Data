# API调用流程可视化示例

## 🎯 实际运行示例：处理一个问题

### 输入数据
```json
{
  "question_id": "qa_001",
  "question": "什么是OLED显示技术？",
  "answer": "OLED是有机发光二极管显示技术"
}
```

---

## 📡 API调用时间线

### ⏱️ T0 - 开始处理

```
[轨迹蒸馏.py] 读取问题: "什么是OLED显示技术？"
[轨迹蒸馏.py] 初始上下文: "无"
[轨迹蒸馏.py] 开始 Hop 1...
```

---

### ⏱️ T1 - Hop 1: vLLM Judge (第1次调用vLLM)

**请求 → vLLM API**:
```http
POST http://127.0.0.1:8000/v1/chat/completions
Content-Type: application/json

{
  "model": "/path/to/qwen3-32b",
  "messages": [
    {
      "role": "system",
      "content": "你是一个知识缺口分析助手..."
    },
    {
      "role": "user",
      "content": "[问题]: 什么是OLED显示技术？\n[已检索的上下文]: 无"
    }
  ]
}
```

**响应 ← vLLM API**:
```json
{
  "knowledge_gap": true,
  "reasoning": "用户询问OLED显示技术，我需要从知识库检索相关信息",
  "query": "OLED显示技术的基本原理是什么？"
}
```

---

### ⏱️ T2 - Hop 1: RAG检索 (双语增强)

#### 步骤1: 火山API - 语种检测
```python
火山API请求: 检测 "OLED显示技术的基本原理是什么？" 的语种
火山API响应: "zh" (中文)
```

#### 步骤2: 火山API - 翻译
```python
火山API请求: 翻译到英文
火山API响应: "What is the basic principle of OLED display technology?"
```

#### 步骤3: 并行RAG检索 (2次)

**原文检索**:
```http
POST http://218.104.107.132:5002/mongodb
Content-Type: application/x-www-form-urlencoded

id=102&top_doc_num=5&query=OLED显示技术的基本原理是什么？
```

**译文检索**:
```http
POST http://218.104.107.132:5002/mongodb
Content-Type: application/x-www-form-urlencoded

id=102&top_doc_num=5&query=What is the basic principle of OLED display technology?
```

**RAG响应 (合并去重后)**:
```python
[
  "OLED(Organic Light-Emitting Diode)是一种有机发光二极管...",
  "OLED的工作原理是在阳极和阴极之间夹有机材料层...",
  "与LCD不同，OLED每个像素可以独立发光..."
]
```

---

### ⏱️ T3 - Hop 2: vLLM Judge (第2次调用vLLM)

**请求 → vLLM API**:
```http
POST http://127.0.0.1:8000/v1/chat/completions

{
  "messages": [
    {
      "role": "user",
      "content": "[问题]: 什么是OLED显示技术？\n[已检索的上下文]: \n[检索到的知识块1]: OLED是有机发光二极管...\n[检索到的知识块2]: OLED的工作原理是..."
    }
  ]
}
```

**响应 ← vLLM API**:
```json
{
  "knowledge_gap": true,
  "reasoning": "已了解基本原理，但需要了解OLED相比传统技术的优势",
  "query": "OLED相比LCD有哪些优势？"
}
```

---

### ⏱️ T4 - Hop 2: RAG检索 (重复T2流程)

```
火山API检测语种 → zh
火山API翻译 → "What are the advantages of OLED over LCD?"
并行RAG检索 (原文 + 译文)
合并去重 → 新增3个知识块
```

---

### ⏱️ T5 - Hop 3: vLLM Judge (第3次调用vLLM)

**响应 ← vLLM API**:
```json
{
  "knowledge_gap": false,
  "reasoning": "我已经收集到足够的信息，包括OLED的原理和优势",
  "query": null
}
```

✅ `knowledge_gap=false` → 停止迭代，进入最终答案生成

---

### ⏱️ T6 - 最终答案: vLLM Answerer (第4次调用vLLM)

**请求 → vLLM API**:
```http
POST http://127.0.0.1:8000/v1/chat/completions

{
  "messages": [
    {
      "role": "system",
      "content": "你是一个半导体显示领域的资深专家..."
    },
    {
      "role": "user",
      "content": "问题: 什么是OLED显示技术？\n上下文: [所有6个检索到的知识块]"
    }
  ]
}
```

**响应 ← vLLM API**:
```
OLED(有机发光二极管)是一种显示技术，它使用有机材料在电流作用下发光。
与传统LCD显示器不同，OLED不需要背光源，每个像素可以独立发光，因此可以
实现真正的黑色和更高的对比度。主要优势包括：
1. 更快的响应时间
2. 更广的视角
3. 更轻薄的设计
4. 更低的功耗
```

---

### ⏱️ T7 - 保存结果

**输出到 `inference_trajectory.jsonl`**:
```json
{
  "question_id": "qa_001",
  "question": "什么是OLED显示技术？",
  "ground_truth_answer": "OLED是有机发光二极管显示技术",
  "is_multi_hop": true,
  "rag_call_count": 2,
  "final_model_answer": "OLED(有机发光二极管)是一种显示技术...",
  "full_trace": [
    {
      "hop": 1,
      "knowledge_gap": true,
      "reasoning": "用户询问OLED显示技术，我需要从知识库检索相关信息",
      "query": "OLED显示技术的基本原理是什么？",
      "retrieved_chunks": ["...", "...", "..."]
    },
    {
      "hop": 2,
      "knowledge_gap": true,
      "reasoning": "已了解基本原理，但需要了解OLED相比传统技术的优势",
      "query": "OLED相比LCD有哪些优势？",
      "retrieved_chunks": ["...", "...", "..."]
    },
    {
      "hop": 3,
      "knowledge_gap": false,
      "reasoning": "我已经收集到足够的信息",
      "query": null,
      "retrieved_chunks": null
    }
  ]
}
```

---

## 📊 API调用统计

| 服务 | 调用次数 | 作用 | 耗时(估算) |
|-----|---------|------|----------|
| **vLLM** | **4次** | 2次Judge + 1次Judge(停止) + 1次Answer | ~8-12秒 |
| 火山翻译 | 2次 | 中→英翻译 | ~0.5秒 |
| 火山检测 | 2次 | 语种检测 | ~0.3秒 |
| RAG检索 | 4次 | 2次原文 + 2次译文 | ~4秒 (每次1秒) |
| **总耗时** | - | - | **~13-17秒** |

---

## 🔑 核心要点

### 1️⃣ vLLM是主角，火山API是配角

```
vLLM (大脑): 4次调用，决定"查什么"、"够不够"、"答什么"
   ↓
火山API (翻译): 4次调用，辅助RAG检索
   ↓
RAG (知识库): 4次调用，提供知识块
```

### 2️⃣ 输入只需3个字段

```json
{
  "question_id": "必需",
  "question": "必需",
  "answer": "可选(仅用于对比)"
}
```

### 3️⃣ 火山API可以禁用

如果火山API不可用：
- ✅ 脚本仍然可以运行
- ⚠️ 只会用原文进行RAG检索(单语模式)
- 📉 可能导致召回率下降(找不到英文文档)

### 4️⃣ vLLM不能禁用

如果vLLM不可用：
- ❌ 脚本无法运行
- 因为vLLM负责所有的推理决策

---

## 🧪 快速验证

### 测试vLLM是否可用
```bash
curl http://127.0.0.1:8000/v1/models
# 期望输出: {"object":"list","data":[{"id":"/path/to/qwen3-32b",...}]}
```

### 测试火山API是否可用
```python
python3 -c "
from volcengine.ApiInfo import ApiInfo
print('✅ 火山SDK已安装')
"
```

### 测试RAG检索是否可用
```bash
curl -X POST http://218.104.107.132:5002/mongodb \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "id=102&top_doc_num=1&query=测试"
```

---

## 📚 相关文件

- `轨迹蒸馏输入说明.md` - 详细的输入格式和API说明
- `运行说明.md` - 完整的运行指南
- `qa_data.jsonl` - 示例输入文件
