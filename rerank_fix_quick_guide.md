# Rerank 错误修复 - 快速指南

## 🎯 快速概览

**问题：** Rerank 批次处理时频繁出现 `Score count mismatch` 和 `HTTP timeout` 错误

**解决：** 已在 3 个文件中应用智能重试和降级机制

## ✅ 已修复的文件

```
✅ search_srv_pipeline_v3_for_experiment_new_finetuned_v11_sr1_robust.py
✅ search_srv_pipeline_v3_for_experiment_new_finetuned_v11_sr1_fixed.py  
✅ search_srv_pipeline_v3_turbo.py
```

## 🔧 主要修复

### 1. 批次大小优化
```python
# 修改前
batch_size = 20  # ❌ 太大，容易超时

# 修改后  
batch_size = 12  # ✅ 更稳定
```

### 2. 智能重试机制
```python
# ✅ 新增：失败时自动分批重试
if len(scores) != len(keys):
    # 分成两半重试
    mid = len(keys) // 2
    process_batch_with_retry(keys[:mid], pairs[:mid])
    process_batch_with_retry(keys[mid:], pairs[mid:])
```

### 3. 动态超时
```python
# 修改前
timeout=(10, 120)  # ❌ 固定超时

# 修改后
read_timeout = 30 + len(pairs) * 3  # ✅ 动态调整
timeout=(15, read_timeout)
```

### 4. 文本清理
```python
# ✅ 新增：清理特殊字符，智能截断
shard_text = chunk_data['shard']
if len(shard_text) > 1000:
    # 在句子边界截断
    for punct in ['。', '！', '？', '.', '!', '?', '\n']:
        last_idx = truncated.rfind(punct)
        if last_idx > 700:
            truncated = truncated[:last_idx + 1]
            break
shard_text = shard_text.replace('\x00', '').strip()
```

### 5. 优雅降级
```python
# ✅ 新增：失败时使用原始分数
if not process_batch_with_retry(keys, pairs):
    for key in keys:
        result_dict[key]['rerank_score'] = result_dict[key]['final_score']
```

## 📊 预期改善

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 批次成功率 | ~60% | ~90% |
| 超时错误 | 30% | ~10% |
| Score mismatch | 频繁 | 大幅减少 |
| 服务可用性 | 部分失败影响结果 | 失败也能返回结果 |

## 🚀 立即使用

### 方法1：直接使用修复后的文件

```bash
# 选择一个已修复的文件运行
python search_srv_pipeline_v3_for_experiment_new_finetuned_v11_sr1_robust.py --port 9510
```

### 方法2：查看修复效果

运行后查看日志：

```bash
# 查看 rerank 统计
grep "\[Rerank\] Completed:" your_log.log

# 示例输出：
# [Rerank] Completed: 23 successful, 2 failed out of 25 batches
```

## 🔍 监控命令

### 查看错误率
```bash
# 统计失败批次
grep "\[Rerank\] Batch.*error:" log.txt | wc -l

# 统计成功批次  
grep "\[Rerank\] Completed:" log.txt | tail -10
```

### 实时监控
```bash
# 实时查看 rerank 日志
tail -f your_log.log | grep "\[Rerank\]"
```

## ⚙️ 可调参数

如果需要进一步优化，可以调整：

```python
# 在 rerank_pipeline 函数中
batch_size = 12      # 批次大小：8-15 之间
max_retries = 2      # 重试次数：1-3 之间
read_timeout = 30 + len(pairs) * 3  # 基础超时 + 每对额外时间
```

**建议值：**
- 高负载：`batch_size=8, max_retries=3`
- 正常负载：`batch_size=12, max_retries=2`（当前）
- 低负载：`batch_size=15, max_retries=1`

## 🐛 故障排查

### 1. 仍有较多失败？

**检查 rerank 服务：**
```bash
curl -X POST http://8.130.183.20:8032/query_bge_reranker/ \
  -H "Content-Type: application/json" \
  -d '{"type": "multi", "multi_data": [["测试", "测试文档"]]}'
```

**如果服务正常，减小批次：**
```python
batch_size = 8  # 或更小
```

### 2. 超时仍然频繁？

**增加超时时间：**
```python
read_timeout = 45 + len(pairs) * 4  # 更长的超时
```

**或减小批次：**
```python
batch_size = 10
```

### 3. Score count mismatch 仍存在？

**查看详细日志：**
```python
# 在 process_batch_with_retry 中添加
print(f'Request size: {len(json.dumps(bge_multi_data))} bytes')
print(f'Expected scores: {len(keys)}, Got: {len(bge_rerank_score_list)}')
```

**可能需要：**
- 检查 rerank 服务端日志
- 检查文本是否有特殊字符
- 进一步减小批次大小

## 📝 核心优势

✅ **更稳定：** 减少批次大小，降低失败率  
✅ **更智能：** 自动重试和分批处理  
✅ **更灵活：** 动态超时适应不同场景  
✅ **更可靠：** 失败降级，保证服务可用  
✅ **更清晰：** 详细日志便于监控

## 📞 需要帮助？

1. 查看详细报告：`rerank_batch_errors_fix_report.md`
2. 检查日志中的错误统计
3. 调整参数后重新测试

---

**修复版本：** v2.0  
**更新日期：** 2025-12-10
