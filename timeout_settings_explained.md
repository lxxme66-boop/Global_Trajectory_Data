# ⏱️ Timeout 设置详解 - 三层架构

## 🔍 架构图

```
客户端 
  ↓ (HTTP请求)
  ↓ timeout_1: 客户端超时
  ↓
Nginx (nginx_search.conf)
  ↓ (反向代理)
  ↓ timeout_2: proxy_read_timeout = 300s
  ↓
Python 搜索服务 (search_srv_pipeline_v3_*.py)
  ↓ (HTTP调用)
  ↓ timeout_3: requests timeout = (15, read_timeout)
  ↓
Rerank 服务 (8.130.183.20:8032)
```

---

## 📍 1. Nginx 层超时（**这是关键！**）

### 位置：`nginx_search.conf`

```nginx
location /api-rqa-search/ {
    proxy_pass http://search_backend;
    
    # ⚠️ 重要：Nginx 到 Python 服务的超时
    proxy_connect_timeout 300s;  # 连接超时：5分钟
    proxy_send_timeout 300s;     # 发送超时：5分钟
    proxy_read_timeout 300s;     # 读取超时：5分钟 ← 这个最重要！
}
```

**作用：**
- 控制 Nginx 等待 Python 搜索服务响应的最长时间
- 如果 Python 服务在 300 秒内没有返回完整响应，Nginx 会返回 **504 Gateway Timeout**

**当前设置：** 300 秒（5分钟）

---

## 📍 2. Python 到 Rerank 服务的超时（**您遇到的问题在这里！**）

### 位置：`search_srv_pipeline_v3_*.py` 中的 `rerank_pipeline` 函数

#### 修复前（旧代码）：
```python
RERANKER_URL = 'http://8.130.183.20:8032/query_bge_reranker/'

# 固定超时 60 秒
response = HTTP_SESSION.post(
    RERANKER_URL,
    json=request_data,
    timeout=(10, 60)  # ❌ (连接超时10s, 读取超时60s)
)
```

**问题：**
- 批次大小 20，处理时间可能超过 60 秒
- 导致 `HTTPConnectionPool Read timed out (read timeout=60)`

#### 修复后（新代码）：
```python
RERANKER_URL = 'http://8.130.183.20:8032/query_bge_reranker/'

# 动态超时：根据批次大小调整
read_timeout = 30 + len(pairs) * 3  # 基础30秒 + 每对3秒

response = HTTP_SESSION.post(
    RERANKER_URL,
    json=request_data,
    timeout=(15, read_timeout)  # ✅ (连接15s, 读取动态)
)

# 示例：
# - 批次 12：超时 = 30 + 12×3 = 66 秒
# - 批次 20：超时 = 30 + 20×3 = 90 秒
# - 批次 6：超时 = 30 + 6×3 = 48 秒
```

**这就是您日志中看到的超时错误的来源！**

---

## 🎯 您的问题答案

### Q: Read timeout 在哪设置？

**A: 在 Python 代码中设置！**

```python
# 文件：search_srv_pipeline_v3_for_experiment_new_finetuned_v11_sr1_robust.py
# 位置：rerank_pipeline 函数，约 838 行

response = HTTP_SESSION.post(
    bge_server_url,  # http://8.130.183.20:8032/query_bge_reranker/
    data=json.dumps(bge_multi_data),
    timeout=(15, read_timeout)  # ← 这里！
    #        ^^  ^^^^^^^^^^^^^
    #        |   |
    #        |   └─ 读取超时（动态计算）
    #        └───── 连接超时（15秒）
)
```

### Q: 是 rerank 本身还是 nginx？

**A: 都不是！是 Python 搜索服务调用 rerank 时设置的！**

- **不是 rerank 服务端**设置的（rerank 服务端有自己的处理超时，但那是另一回事）
- **不是 nginx** 设置的（nginx 的 300s 是 nginx 到 Python 的超时，更长）
- **是 Python 搜索服务**中 `requests.post()` 的 `timeout` 参数

---

## 🔧 三层超时关系

### 完整的请求链路：

```
客户端
  ↓ (没有明确超时，通常很长，如2分钟)
Nginx
  ↓ proxy_read_timeout = 300s (5分钟)
Python 搜索服务
  ↓ 
  ├─ 召回阶段：timeout = (10, 120)
  ├─ 编码服务：timeout = (10, 120)
  └─ Rerank 服务：timeout = (15, read_timeout) ← 您的错误在这里
     └─ 修复前：固定 60s ❌
     └─ 修复后：动态 66s (批次12) ✅
```

### 超时优先级（从内到外）：

1. **最内层**：Python → Rerank 的 timeout
   - 修复前：60 秒
   - 修复后：30 + 批次大小×3 秒
   
2. **中间层**：Nginx → Python 的 proxy_read_timeout
   - 当前：300 秒（足够）
   
3. **最外层**：客户端 → Nginx
   - 取决于客户端设置

**关键原则：** 内层超时必须 < 外层超时

---

## 📝 您日志中的错误分析

```
[Rerank] Batch 15 error: HTTPConnectionPool(host='8.130.183.20', port=8032): 
Read timed out. (read timeout=60)
```

**解读：**
- `8.130.183.20:8032` = Rerank 服务地址
- `Read timed out` = Python 等待 Rerank 响应超时
- `read timeout=60` = 超时阈值 60 秒
- **不是 Nginx 的问题，是 Python 代码中设置的！**

---

## ✅ 修复方案总结

### 1. 已修复的代码（自动生效）

运行修复后的文件，自动使用动态超时：

```bash
python search_srv_pipeline_v3_for_experiment_new_finetuned_v11_sr1_robust.py --port 9511
```

### 2. 如果需要进一步调整

#### 方案 A：调整 Python 代码中的超时计算

```python
# 在 rerank_pipeline 函数中修改
read_timeout = 45 + len(pairs) * 4  # 增加基础超时和倍数
```

#### 方案 B：调整 Nginx 超时（如果整体请求很慢）

```nginx
# 编辑 /etc/nginx/conf.d/search.conf
location /api-rqa-search/ {
    proxy_read_timeout 600s;  # 改为10分钟
}

# 重载 Nginx
sudo nginx -s reload
```

但**通常不需要调整 Nginx**，因为：
- Nginx 的 300s 已经很长了
- 问题在 Python → Rerank 的 60s 超时（已修复）

---

## 🎯 推荐配置

### 当前优化后的配置（已应用）

| 层级 | 位置 | 超时设置 | 说明 |
|------|------|----------|------|
| **客户端→Nginx** | 客户端 | 120-180s | 通常由客户端控制 |
| **Nginx→Python** | nginx.conf | **300s** | 足够长，无需修改 |
| **Python→Rerank** | Python代码 | **30+3×N秒** | ✅ 已优化，动态调整 |
| **Python→Milvus** | Python代码 | 120s | 向量检索超时 |
| **Python→Encoder** | Python代码 | 120s | 编码服务超时 |

### 批次大小与超时对照表

| 批次大小 | Python→Rerank超时 | 是否足够 |
|----------|-------------------|----------|
| 20（旧） | 60s | ❌ 不够 |
| 12（新） | 66s | ✅ 足够 |
| 10 | 60s | ✅ 足够 |
| 8 | 54s | ✅ 足够 |
| 6 | 48s | ✅ 足够 |

---

## 🔍 如何验证超时设置

### 1. 查看当前代码中的超时

```bash
# 查看 rerank 超时设置
grep -A 3 "read_timeout = " search_srv_pipeline_v3_for_experiment_new_finetuned_v11_sr1_robust.py

# 输出：
# read_timeout = 30 + len(pairs) * 3
# response = HTTP_SESSION.post(
#     bge_server_url,
#     timeout=(15, read_timeout)
```

### 2. 查看 Nginx 超时设置

```bash
grep "proxy_read_timeout" /etc/nginx/conf.d/search.conf

# 输出：
# proxy_read_timeout 300s;
```

### 3. 运行时监控

```bash
# 查看实际超时情况
tail -f log.txt | grep -E "\[Rerank\]|timeout"
```

---

## 💡 关键要点

1. ✅ **您遇到的 timeout 错误是 Python 代码中设置的**
   - 不是 Nginx
   - 不是 Rerank 服务端

2. ✅ **已经修复**
   - 批次大小：20 → 12
   - 超时时间：固定 60s → 动态 66s
   - 加上智能重试机制

3. ✅ **Nginx 的 300s 足够长，无需修改**
   - Nginx 超时 > Python 总处理时间
   - 只有在整个搜索请求超过 5 分钟时才需要调整

4. ✅ **如果还有超时，调整 Python 代码，不是 Nginx**
   ```python
   read_timeout = 45 + len(pairs) * 4  # 增加基础时间
   ```

---

**总结：您的超时问题在 Python 代码的 `timeout=(15, 60)` 参数，不是 Nginx 或 Rerank 服务端设置的，已通过修复解决！** 🎉
