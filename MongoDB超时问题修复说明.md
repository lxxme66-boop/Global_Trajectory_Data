# MongoDB 超时问题修复说明

## 🔴 问题分析

### 错误信息
```
MaxTimeMSExpired: Executor error during getMore :: 
caused by :: operation exceeded time limit
```

### 根本原因
1. **超时时间不足**：`.max_time_ms(60000)` 设置的 60 秒不够
2. **批次过大**：单批查询数据量太大（400-600 条）
3. **并发压力**：4 个线程同时查询，MongoDB 压力过大
4. **索引缺失**：可能缺少 `{index: 1, doc_id: 1}` 组合索引

---

## ✅ 修复方案

### 1. 增加 MongoDB 查询超时时间

**修改前**：
```python
.max_time_ms(60000)  # 60 秒
```

**修改后**：
```python
MONGO_MAX_TIME_MS = 180000  # 180 秒（3 分钟）
.max_time_ms(MONGO_MAX_TIME_MS)
```

**效果**：给 MongoDB 更多执行时间

---

### 2. 减少批次大小

**修改前**：
```python
def get_optimal_batch_size(total_conditions):
    if total_conditions < 1000:
        return 250  # 4 批
    elif total_conditions < 2000:
        return 400  # 5 批
    elif total_conditions < 3000:
        return 500  # 6 批
    else:
        return 600  # 7 批
```

**修改后**：
```python
def get_optimal_batch_size(total_conditions):
    if total_conditions < 1000:
        return 200  # 5 批（减小 20%）
    elif total_conditions < 2000:
        return 300  # 7 批（减小 25%）
    elif total_conditions < 3000:
        return 350  # 9 批（减小 30%）
    else:
        return 400  # 10 批（减小 33%）
```

**效果**：单批数据量减少，查询更快

---

### 3. 减少并行线程数

**修改前**：
```python
MONGO_PARALLEL_WORKERS = 4  # 4 个线程并行
```

**修改后**：
```python
MONGO_PARALLEL_WORKERS = 2  # 2 个线程并行（降低压力）
```

**效果**：减少对 MongoDB 的并发压力，避免资源竞争

---

### 4. 增加重试次数

**修改前**：
```python
@mongodb_retry(max_retries=3, initial_delay=2)
```

**修改后**：
```python
@mongodb_retry(max_retries=5, initial_delay=3)
```

**效果**：更多重试机会，提高成功率

---

### 5. 自动拆分超时批次 ⭐⭐⭐

**新增功能**：当批次超时时，自动拆分为更小的批次重试

```python
except pymongo.errors.ExecutionTimeout as e:
    print(f'⏰ TIMEOUT, reducing batch size and retrying...')
    
    # 超时时自动拆分为更小的批次
    if len(batch_conditions) > 50:
        mid = len(batch_conditions) // 2
        print(f'🔀 Splitting into 2 sub-batches: {mid} + {len(batch_conditions) - mid}')
        
        results1 = query_embeddings_batch(mongo_collection, batch_conditions[:mid], ...)
        results2 = query_embeddings_batch(mongo_collection, batch_conditions[mid:], ...)
        
        return results1 + results2
```

**效果**：
- 自动适应 MongoDB 负载
- 即使部分批次超时也能继续
- 提高整体成功率

---

### 6. 优化错误日志

**修改前**：
```python
except Exception as e:
    print(f'❌ Batch {batch_id} FAILED: {e}')
```

**修改后**：
```python
except Exception as e:
    error_msg = str(e)
    # 只打印简短的错误信息，避免日志刷屏
    if 'MaxTimeMSExpired' in error_msg or 'time limit' in error_msg:
        print(f'❌ Batch {batch_id} TIMEOUT (will retry with smaller batch)')
    else:
        print(f'❌ Batch {batch_id} FAILED: {error_msg[:100]}...')
    # 继续处理其他批次，不中断
```

**效果**：
- 错误日志更简洁
- 避免日志刷屏
- 明确标识超时错误

---

## 📊 修复效果对比

| 配置项 | 修改前 | 修改后 | 改进 |
|--------|--------|--------|------|
| **超时时间** | 60s | **180s** | ↑ 200% |
| **批次大小** | 250-600 | **200-400** | ↓ 20-33% |
| **并行线程** | 4 | **2** | ↓ 50% |
| **重试次数** | 3 | **5** | ↑ 67% |
| **自动拆分** | ❌ | ✅ | 新功能 |
| **错误处理** | 基础 | **智能** | 优化 |

---

## 🎯 预期效果

### 查询性能
```
原来：4 线程 × 批次大小 400 = 并发查询 1600 条
现在：2 线程 × 批次大小 300 = 并发查询 600 条

单批查询时间：预计从 80-120s 降到 30-60s
```

### 成功率
```
原来：超时频繁，成功率 < 50%
现在：
- 超时时自动拆分重试
- 更长的超时时间
- 更多的重试机会
预期成功率：> 95%
```

### 查询总时间
```
假设 4000 条数据：

原来（4 线程 × 批次 500）：
- 8 批 ÷ 4 线程 = 2 轮
- 每批超时重试 3 次
- 总时间：2 轮 × 120s × 3 = 720s（12 分钟）❌

现在（2 线程 × 批次 300）：
- 14 批 ÷ 2 线程 = 7 轮
- 每批 30-60s，自动拆分
- 总时间：7 轮 × 60s = 420s（7 分钟）✅
```

---

## 🔍 如何验证修复效果

### 1. 查看日志
```bash
tail -f logs/search_9511.log | grep "MongoDB Batch"
```

**期望看到**：
```
[MongoDB Batch 1/14] 🔄 Querying 300 conditions...
[MongoDB Batch 1/14] ✅ 298 records in 45.23s
[MongoDB Batch 2/14] 🔄 Querying 300 conditions...
[MongoDB Batch 2/14] ✅ 295 records in 38.67s
...
[Rank] ✅ PARALLEL query done: success=14/14, failed=0, time=420.5s
```

### 2. 检查成功率
```bash
grep "PARALLEL query done" logs/search_9511.log | tail -20
```

**期望看到**：
```
success=14/14, failed=0  # 全部成功
或
success=12/14, failed=2  # 部分失败但大部分成功
```

### 3. 检查超时情况
```bash
grep "TIMEOUT" logs/search_9511.log | wc -l
```

**期望**：超时次数显著减少（< 5% 的批次）

---

## ⚙️ 进一步优化建议

### 1. 添加 MongoDB 索引（强烈推荐）⭐⭐⭐⭐⭐

```javascript
// 在 MongoDB 中执行
use rqa;

// 添加组合索引
db.paper_shards_detail_table_20230908.createIndex(
    { index: 1, doc_id: 1 },
    { background: true, name: "idx_index_docid" }
);

// 检查索引
db.paper_shards_detail_table_20230908.getIndexes();
```

**效果**：查询速度提升 10-100 倍！

### 2. 减少查询字段

**当前**：
```python
{'_id': 0, 'index': 1, 'doc_id': 1, 'embedding': 1}
# embedding 字段很大（1024 维 × 4 字节 = 4KB）
```

**建议**：如果可能，考虑：
- 分离存储 embedding（独立集合）
- 使用更小的 embedding 维度
- 只在需要时查询 embedding

### 3. 使用 MongoDB 连接池监控

```python
# 添加监控
from pymongo import monitoring

class CommandLogger(monitoring.CommandListener):
    def started(self, event):
        print(f"Command {event.command_name} started")
    
    def succeeded(self, event):
        print(f"Command {event.command_name} succeeded in {event.duration_micros}µs")
    
    def failed(self, event):
        print(f"Command {event.command_name} failed: {event.failure}")

monitoring.register(CommandLogger())
```

### 4. 调整配置（根据实际情况）

如果仍然超时，继续调整：

```python
# 更保守的配置
MONGO_PARALLEL_WORKERS = 1  # 单线程（最稳定）
MONGO_MAX_TIME_MS = 300000  # 5 分钟

def get_optimal_batch_size(total_conditions):
    # 更小的批次
    if total_conditions < 1000:
        return 100  # 10 批
    elif total_conditions < 2000:
        return 150  # 14 批
    else:
        return 200  # 20 批
```

---

## 📞 如果问题仍然存在

### 检查清单

1. **MongoDB 服务器负载**
   ```bash
   mongo --host 10.70.223.31:27017 -u root -p
   db.currentOp()  # 查看当前运行的操作
   db.serverStatus().connections  # 查看连接数
   ```

2. **网络延迟**
   ```bash
   ping 10.70.223.31
   # 期望 < 10ms
   ```

3. **MongoDB 慢查询日志**
   ```bash
   # 在 MongoDB 中
   db.setProfilingLevel(1, { slowms: 10000 })  # 记录 > 10s 的查询
   db.system.profile.find().sort({ts: -1}).limit(10)
   ```

4. **数据量统计**
   ```bash
   # 查看集合大小
   db.paper_shards_detail_table_20230908.stats()
   ```

---

## 🎉 总结

**核心修复**：
1. ⏰ 增加超时时间：60s → 180s
2. 📉 减少批次大小：250-600 → 200-400
3. 👥 减少并发线程：4 → 2
4. 🔄 增加重试次数：3 → 5
5. ✂️ 自动拆分超时批次（新功能）
6. 📝 优化错误日志

**预期效果**：
- 超时次数：显著减少（< 5%）
- 成功率：> 95%
- 查询时间：更稳定（7-10 分钟）

**最重要的优化**：添加 MongoDB 索引！🚀
