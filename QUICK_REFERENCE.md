# 🚀 快速参考指南

**版本：v2.3 (2025-12-10 17:00)**

---

## 📦 文件清单

| 文件 | 版本 | 说明 |
|------|------|------|
| `search_srv_pipeline_optimized.py` | v2.3 | ✅ 优化后的服务端代码（主文件） |
| `test_optimized_service.py` | v1.2 | ✅ 测试代码 |
| `VERSION_CHANGELOG.md` | - | 版本更新日志 |
| `QUICK_REFERENCE.md` | - | 本文档 |

---

## 🔧 快速操作

### 1. 启动服务

```bash
# 停止旧服务
pkill -9 -f search_srv_pipeline

# 启动新服务
cd /workspace
python3 search_srv_pipeline_optimized.py --port 9510 --host 10.70.223.31

# 后台启动
nohup python3 search_srv_pipeline_optimized.py --port 9510 --host 10.70.223.31 > service.log 2>&1 &
```

### 2. 验证服务

```bash
# 健康检查
curl http://10.70.223.31:9510/api-rqa-search/health

# 查看统计
curl http://10.70.223.31:9510/api-rqa-search/stats

# 检查版本
curl http://10.70.223.31:9510/api-rqa-search/health | grep version
# 应该显示: "v2.3_20251210_1700"
```

### 3. 运行测试

```bash
python3 test_optimized_service.py 10.70.223.31 9510
```

### 4. 查看日志

```bash
# 实时查看
tail -f service.log

# 查看最近100行
tail -100 service.log

# 搜索错误
grep "❌" service.log
grep "⚠️" service.log
```

---

## 📊 监控指标

### 关键日志标识

| 符号 | 含义 | 示例 |
|------|------|------|
| ✅ | 成功 | `✅ Encoding completed in 2.3s` |
| ❌ | 失败 | `❌ Encoding failed: timeout` |
| ⚠️ | 警告 | `⚠️ High load: 51 active requests` |
| 🎯 | 结果 | `🎯 Final results: 3 docs` |
| 📊 | 监控 | `📊 [Monitor] Active: 5` |

### 正常请求流程

```
[Request 123] Started, query: xxx, active: 5
[Request 123] Stage 1: Encoding started
[Request 123] ✅ Encoding completed in 2.3s
[Request 123] Stage 2: Recall started
[Request 123] ✅ Recall completed in 5.6s, found 3000 chunks
[Request 123] Stage 3: Ranking started
[Request 123] ✅ Ranking completed in 8.2s
[Request 123] Stage 4: Reranking started
[Request 123] ✅ Reranking completed in 15.4s
[Request 123] Stage 5: Concatenating started
[Request 123] ✅ Concatenation completed in 0.5s, got 10 results
[Request 123] 🎯 Final results: 3 docs (after score filter)
[Request 123] Completed in 32.0s, code=0, results: 3
```

### 异常请求流程

```
[Request 456] Started, query: xxx, active: 52
[Request 456] ⚠️ High load: 52 active requests, rejecting
# 返回 code=-3 (Server busy)

或者：

[Request 789] Started, query: xxx, active: 10
[Request 789] Stage 1: Encoding started
❌ [Encode] Timeout after 60s: Read timed out
⚠️ [SafeExecute] Encoding failed: Encoding service timeout
[Request 789] ❌ Encoding failed: returned None after 60.2s
[Request 789] Completed in 60.3s, code=-2, results: 0
# 返回 code=-2 (Timeout)
```

---

## 🔍 常见问题诊断

### 问题1：所有请求返回空结果

**症状：**
```python
['null', 'null', 'null', 'null', 0.0]
```

**诊断步骤：**

1. **查看日志中的错误**
   ```bash
   grep "❌" service.log | tail -20
   ```

2. **检查编码服务**
   ```bash
   curl http://8.130.183.20:8031/encode -X POST -H "Content-Type: application/json" -d '{"queries":["test"]}'
   ```

3. **检查活跃请求数**
   ```bash
   curl http://10.70.223.31:9510/api-rqa-search/stats | grep active_requests
   ```

**常见原因：**
- 编码服务超时/不可用
- 并发数过高（>50）
- Milvus服务响应慢

**解决方案：**
- 降低并发数到20-30
- 重启编码服务
- 增加超时时间

---

### 问题2：请求卡住不返回

**症状：**
- 客户端一直等待
- 服务端没有"Completed"日志

**诊断步骤：**

1. **查看活跃请求**
   ```bash
   curl http://10.70.223.31:9510/api-rqa-search/stats | jq '.data.active_requests_detail'
   ```

2. **查看卡住的请求**
   ```bash
   curl http://10.70.223.31:9510/api-rqa-search/stats | jq '.data.stuck_requests'
   ```

3. **查看日志最后阶段**
   ```bash
   grep "Request.*Stage" service.log | tail -20
   ```

**常见原因：**
- 重排服务卡死
- MongoDB查询超时
- Milvus查询卡住

**解决方案：**
- 重启服务
- 检查外部服务状态
- 降低批处理大小

---

### 问题3：统计数据不准确

**症状：**
```
总请求数: 100
成功请求: 1
失败请求: 0
```

**诊断：**
- ✅ 已在v2.0中修复（请求ID冲突）
- ✅ 已在v2.1中优化（确保end_request调用）

**验证修复：**
```bash
# 检查版本
curl http://10.70.223.31:9510/api-rqa-search/health | grep version
# 应该显示 v2.3 或更高版本

# 发送测试请求
python3 test_optimized_service.py 10.70.223.31 9510

# 检查统计
curl http://10.70.223.31:9510/api-rqa-search/stats
```

---

## 🎯 性能优化建议

### 客户端优化

```python
# ❌ 不推荐：64个并发
ThreadPoolExecutor(max_workers=64)

# ✅ 推荐：20-30个并发
ThreadPoolExecutor(max_workers=20)

# ❌ 不推荐：短超时
timeout=30

# ✅ 推荐：充足超时
timeout=90

# ✅ 推荐：添加请求间隔
time.sleep(0.1)
```

### 服务端优化

```bash
# 增加工作线程
python3 search_srv_pipeline_optimized.py --workers 8

# 增加批处理大小
python3 search_srv_pipeline_optimized.py --batch-size 200

# 增加请求超时
python3 search_srv_pipeline_optimized.py --request-timeout 600
```

---

## 📈 监控命令

### 实时监控

```bash
# 持续监控统计
watch -n 5 'curl -s http://10.70.223.31:9510/api-rqa-search/stats | jq ".data | {total:.total_requests, success:.successful_requests, active:.active_requests}"'

# 监控错误
tail -f service.log | grep --line-buffered -E "❌|⚠️"

# 监控完成情况
tail -f service.log | grep --line-buffered "Completed"
```

### 性能分析

```bash
# 阶段耗时统计
curl -s http://10.70.223.31:9510/api-rqa-search/stats | jq '.data.stage_times'

# 缓存命中率
curl -s http://10.70.223.31:9510/api-rqa-search/stats | jq '.data.encode_cache'

# 活跃请求详情
curl -s http://10.70.223.31:9510/api-rqa-search/stats | jq '.data.active_requests_detail'
```

---

## 🔗 相关链接

- 编码服务：`http://8.130.183.20:8031/encode`
- Milvus服务：`http://8.130.183.20:8033/api-vec-search/search`
- 重排服务：`http://8.130.183.20:8032/query_bge_reranker/`
- MongoDB：`mongodb://10.70.223.31:27017`

---

## 📞 紧急处理

### 服务无响应

```bash
# 1. 强制停止
pkill -9 -f search_srv_pipeline

# 2. 清理进程
ps aux | grep search_srv_pipeline | awk '{print $2}' | xargs kill -9

# 3. 重启服务
cd /workspace
python3 search_srv_pipeline_optimized.py --port 9510 --host 10.70.223.31 &

# 4. 验证
curl http://10.70.223.31:9510/api-rqa-search/health
```

### 性能严重下降

```bash
# 1. 查看负载
curl http://10.70.223.31:9510/api-rqa-search/stats | jq '.data.active_requests'

# 2. 查看卡住的请求
curl http://10.70.223.31:9510/api-rqa-search/stats | jq '.data.stuck_requests'

# 3. 如果请求过多，等待完成或重启
# 等待：观察日志，等待请求完成
# 重启：强制停止并重启
```

---

**最后更新：2025-12-10 17:00**
**当前版本：v2.3**
