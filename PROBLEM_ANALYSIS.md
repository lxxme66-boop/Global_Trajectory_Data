# 问题分析报告

## 症状

从诊断数据可以看到：
```
总请求数: 513
成功: 1  ← 只有1次成功！
失败: 0
超时: 0
所有阶段调用次数: 0  ← 从未进入pipeline！
缓存命中率: 0.2% (1/512)
```

## 根本原因

**512个请求在参数校验阶段就被拦截了，根本没有执行搜索逻辑！**

### 代码缺陷

查看搜索接口代码：

```python
@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    req_id = int(time.time() * 1000) % 1000000
    
    try:
        form = request.form
        query = form.get('query', '', str)
        id = form.get('id', 0, int)
        top_doc_num = form.get('top_doc_num', 0, int)
        
        MONITOR.start_request(req_id, query)  # ← 这里记录了请求开始
        TIMEOUT_MANAGER.enter_request()
        
        # ❌ 问题所在：参数校验失败时直接return，没有更新监控！
        if not query:
            return json_result(-1, 'query must not be null.', None)  # ← 直接返回
        if not id:
            return json_result(-1, 'id must not be null.', None)      # ← 直接返回
        if not top_doc_num:
            return json_result(-1, 'top_doc_num must not be null.', None)  # ← 直接返回
        
        # 只有通过参数校验的请求才会执行到这里
        # ...搜索逻辑...
        
        MONITOR.end_request(req_id, success=True)
    except:
        MONITOR.end_request(req_id, success=False)
```

### 问题链

1. **请求进来** → `MONITOR.start_request()` 被调用 → `total_requests++`
2. **参数校验失败** → 直接 `return` → **没有调用 `MONITOR.end_request()`**
3. **监控统计不完整** → 看起来有513个请求，但只有1个成功
4. **阶段统计全是0** → 因为请求根本没进入搜索pipeline

## 为什么调用速度很快？

因为请求在参数校验阶段（毫秒级）就返回了，根本没有执行：
- 编码服务调用（秒级）
- 向量召回（秒级）
- 数据库查询（秒级）
- 重排序（秒级）

所以感觉"速度提升了几十倍"，实际上是**什么都没做就返回了**！

## 可能的客户端问题

最可能的原因是：**客户端没有正确传递参数**

```python
# 错误的调用方式（可能）：
response = requests.get('http://10.70.223.31:9510/api-rqa-search/stats')
# ↑ 这个调用没问题，所以看到了统计数据

# 但搜索调用可能有问题：
response = requests.post('http://10.70.223.31:9510/api-rqa-search/search', 
                         json={'query': 'test'})  # ❌ 使用了json而不是form data

# 或者
response = requests.post('http://10.70.223.31:9510/api-rqa-search/search', 
                         data={})  # ❌ 没有传参数
```

服务端期望的是：
```python
response = requests.post('http://10.70.223.31:9510/api-rqa-search/search',
                         data={           # ← 注意是 data 不是 json
                             'query': 'What is semiconductor?',
                             'id': 123,
                             'top_doc_num': 5
                         })
```

## 修复方案

### 方案1: 修复监控代码（推荐）

确保所有返回路径都更新监控：

```python
@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    req_id = int(time.time() * 1000) % 1000000
    
    MONITOR.start_request(req_id, '')
    TIMEOUT_MANAGER.enter_request()
    
    try:
        form = request.form
        query = form.get('query', '', str)
        id = form.get('id', 0, int)
        top_doc_num = form.get('top_doc_num', 0, int)
        
        # 参数校验 - 失败时也要记录
        if not query:
            MONITOR.end_request(req_id, success=False)
            return json_result(-1, 'query must not be null.', None)
        
        if not id:
            MONITOR.end_request(req_id, success=False)
            return json_result(-1, 'id must not be null.', None)
        
        if not top_doc_num:
            MONITOR.end_request(req_id, success=False)
            return json_result(-1, 'top_doc_num must not be null.', None)
        
        # 更新query
        MONITOR.active_requests[req_id]['query'] = query[:100]
        
        # ...搜索逻辑...
        
        MONITOR.end_request(req_id, success=True)
        return json_result(0, '', data)
        
    except Exception as e:
        MONITOR.end_request(req_id, success=False)
        return json_result(-1, str(e), None)
    finally:
        TIMEOUT_MANAGER.exit_request()
```

### 方案2: 使用try-finally（更安全）

```python
@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    req_id = int(time.time() * 1000) % 1000000
    success = False
    
    MONITOR.start_request(req_id, '')
    TIMEOUT_MANAGER.enter_request()
    
    try:
        # 参数解析和校验
        form = request.form
        query = form.get('query', '', str)
        id = form.get('id', 0, int)
        top_doc_num = form.get('top_doc_num', 0, int)
        
        if not query:
            return json_result(-1, 'query must not be null.', None)
        if not id:
            return json_result(-1, 'id must not be null.', None)
        if not top_doc_num:
            return json_result(-1, 'top_doc_num must not be null.', None)
        
        # 搜索逻辑
        # ...
        
        success = True
        return json_result(0, '', data)
        
    except Exception as e:
        return json_result(-1, str(e), None)
        
    finally:
        # 无论如何都会执行
        MONITOR.end_request(req_id, success=success)
        TIMEOUT_MANAGER.exit_request()
```

### 方案3: 检查客户端调用

创建测试脚本验证参数是否正确传递：

```python
import requests

# 测试1: 正确的调用
response = requests.post('http://10.70.223.31:9510/api-rqa-search/search',
                         data={  # ← 使用 data
                             'query': 'What is semiconductor?',
                             'id': 123,
                             'top_doc_num': 5
                         })
print(f'Test 1 (正确): {response.json()}')

# 测试2: 错误的调用（使用json）
response = requests.post('http://10.70.223.31:9510/api-rqa-search/search',
                         json={  # ← 使用 json（错误）
                             'query': 'What is semiconductor?',
                             'id': 123,
                             'top_doc_num': 5
                         })
print(f'Test 2 (json): {response.json()}')

# 测试3: 空参数
response = requests.post('http://10.70.223.31:9510/api-rqa-search/search',
                         data={})
print(f'Test 3 (空): {response.json()}')
```

## 建议

1. **立即检查客户端代码**，确认参数传递方式是否正确
2. **修复服务端监控代码**，使用上述方案1或方案2
3. **添加请求日志**，记录每个请求的参数和返回状态
4. **使用测试脚本**验证修复效果

## 预期结果

修复后，监控数据应该显示：
```
总请求数: 513
成功: X  ← 实际成功的请求数
失败: Y  ← 参数校验失败的请求数（应该是512）
超时: 0
阶段调用次数: > 0  ← 成功的请求会执行各阶段
```
