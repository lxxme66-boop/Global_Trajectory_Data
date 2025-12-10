# 搜索服务诊断和修复工具包

## 问题摘要

你的搜索服务显示：
- **总请求数: 513**
- **成功: 1** ❌
- **所有阶段调用次数: 0** ❌

**根本原因：512个请求在参数校验阶段就被拦截了，根本没有执行搜索逻辑！**

调用速度很快的原因是：**请求在参数校验阶段（毫秒级）就返回了**，所以感觉"速度提升了几十倍"，实际上是**什么都没做就返回了**！

## 快速开始

### 1. 一键诊断（推荐）

```bash
chmod +x quick_diagnose.sh
./quick_diagnose.sh 10.70.223.31 9510
```

这个脚本会：
- ✅ 检查服务统计数据
- ✅ 测试不同的参数传递方式
- ✅ 自动分析问题原因
- ✅ 给出修复建议

### 2. 详细诊断

如果有Python环境：

```bash
python3 diagnose_real_service.py 10.70.223.31 9510
```

如果有requests模块：

```bash
python3 test_client.py 10.70.223.31 9510
```

## 文件说明

### 诊断工具

| 文件 | 说明 | 用途 |
|------|------|------|
| `quick_diagnose.sh` | 一键诊断脚本（bash） | 快速检查问题，无需Python |
| `diagnose_real_service.py` | 服务诊断工具（Python） | 详细的问题分析和测试 |
| `test_client.py` | 客户端测试工具（Python） | 测试不同的参数传递方式 |

### 文档

| 文件 | 说明 |
|------|------|
| `README_诊断和修复.md` | 本文件，总览 |
| `PROBLEM_ANALYSIS.md` | 详细的问题分析报告 |
| `修复说明.md` | 修复方案和验证方法 |

### 代码示例

| 文件 | 说明 |
|------|------|
| `search_srv_FIXED.py` | 修复后的服务代码（关键部分） |
| `search_srv_diagnose.py` | 简化的诊断服务（用于测试） |

## 可能的问题和解决方案

### 问题1: 客户端使用了错误的参数格式

**症状：** 几乎所有请求都失败

**原因：** 使用了 `json={...}` 而不是 `data={...}`

**解决方案：**

```python
# ❌ 错误
response = requests.post(url, json={'query': 'test', 'id': 123, 'top_doc_num': 5})

# ✅ 正确
response = requests.post(url, data={'query': 'test', 'id': 123, 'top_doc_num': 5})
```

### 问题2: 监控代码未覆盖所有返回路径

**症状：** `total_requests` > `successful_requests + failed_requests + timeout_requests`

**原因：** 参数校验失败时直接 `return`，没有调用 `MONITOR.end_request()`

**解决方案：** 使用 `try-finally` 模式

```python
@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    req_id = int(time.time() * 1000) % 1000000
    success = False  # ← 关键：默认失败
    
    MONITOR.start_request(req_id, '')
    TIMEOUT_MANAGER.enter_request()
    
    try:
        # 参数校验（失败时可以直接return）
        if not query:
            return json_result(-1, 'query must not be null.', None)
        
        # 搜索逻辑...
        
        success = True  # ← 关键：成功时设置
        return json_result(0, '', data)
        
    finally:  # ← 关键：无论如何都会执行
        MONITOR.end_request(req_id, success=success)
        TIMEOUT_MANAGER.exit_request()
```

### 问题3: 服务端依赖服务不可用

**症状：** 请求耗时长，最终超时

**原因：** 编码服务、数据库、Milvus等依赖服务不可用

**解决方案：**
1. 检查编码服务：`curl http://8.130.183.20:8031/`
2. 检查数据库连接：查看服务日志
3. 检查Milvus：`curl http://8.130.183.20:8033/`

## 验证修复

### 步骤1: 运行一键诊断

```bash
./quick_diagnose.sh 10.70.223.31 9510
```

预期输出：
```
📊 步骤1: 获取当前统计...
  总请求: 513
  成功: 1
  失败: 0
  超时: 0
  已统计: 1
  未统计: 512  ← 这个应该是0

❌ 发现问题: 512 个请求未被正确统计！
```

### 步骤2: 测试参数传递

```bash
# 测试正确的方式
curl -X POST "http://10.70.223.31:9510/api-rqa-search/search" \
  -d "query=What is semiconductor?" \
  -d "id=123" \
  -d "top_doc_num=5"

# 测试错误的方式
curl -X POST "http://10.70.223.31:9510/api-rqa-search/search" \
  -H "Content-Type: application/json" \
  -d '{"query":"What is semiconductor?","id":123,"top_doc_num":5}'
```

如果第一个成功，第二个失败，说明是客户端参数传递问题。

### 步骤3: 检查你的客户端代码

找到你的客户端代码（可能是 `test.py` 或其他文件），检查：

```python
# 找到类似这样的代码
response = requests.post(
    'http://10.70.223.31:9510/api-rqa-search/search',
    ???={'query': '...', 'id': 123, 'top_doc_num': 5}
    #   ↑ 这里是 data 还是 json？
)
```

如果是 `json={...}`，改成 `data={...}`。

### 步骤4: 修复后再次诊断

修复后，统计应该显示：

```
总请求: 520
成功: 8  ← 实际成功的请求
失败: 512  ← 参数校验失败的请求（如果确实失败了）
超时: 0
已统计: 520  ← 应该等于总请求数
未统计: 0  ← 应该是0
```

## 常见问题

### Q1: 为什么我看到的请求数一直在增加？

A: 有其他客户端也在调用服务，或者有定时任务。你需要：
1. 记录测试前的请求数
2. 发送测试请求
3. 对比测试后的请求数
4. 计算增量

### Q2: 我的客户端代码是对的，为什么还是失败？

A: 可能的原因：
1. 参数值本身有问题（比如 `id=0` 或 `top_doc_num=0`）
2. 服务端依赖服务不可用
3. 服务端代码有其他bug

检查方法：
```bash
# 完全模拟你的客户端调用
curl -X POST "http://10.70.223.31:9510/api-rqa-search/search" \
  -d "query=你的查询" \
  -d "id=你的id" \
  -d "top_doc_num=你的值"
```

### Q3: 修复后监控还是不准确怎么办？

A: 检查代码中是否有其他提前返回的地方：

```python
# 搜索所有 return 语句
grep -n "return json_result" your_service.py

# 确保每个 return 之前都更新了监控
```

或者使用 `try-finally` 模式（推荐）。

## 联系和支持

如果以上方法都无法解决问题：

1. 查看服务端日志，找到详细错误信息
2. 检查所有依赖服务是否正常
3. 联系开发人员，提供：
   - 一键诊断的完整输出
   - 客户端代码
   - 服务端日志

## 总结

| 文件 | 运行命令 | 输出 |
|------|----------|------|
| quick_diagnose.sh | `./quick_diagnose.sh` | 快速诊断结果 |
| diagnose_real_service.py | `python3 diagnose_real_service.py` | 详细诊断报告 |
| test_client.py | `python3 test_client.py` | 客户端测试结果 |

推荐顺序：
1. ✅ 先运行 `quick_diagnose.sh`（不需要Python）
2. ✅ 如果有Python，再运行 `test_client.py`
3. ✅ 根据结果修复客户端或服务端
4. ✅ 再次运行诊断验证修复

祝顺利解决问题！ 🎉
