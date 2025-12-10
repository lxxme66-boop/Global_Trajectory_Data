# 快速参考 - v2.4

**最后更新：2025-12-10 18:00**

---

## ✅ 核心修复状态

| 问题 | 状态 | 版本 |
|------|------|------|
| 请求ID冲突 | ✅ 已修复 | v2.0 |
| 统计数据准确性 | ✅ 已修复 | v2.0 |
| 错误日志丢失 | ✅ 已修复 | v2.2 |
| 编码结果验证 | ✅ 已修复 | v2.3 |
| 请求完成日志 | ✅ 已优化 | v2.4 |

---

## 🚀 快速操作

### 一键重启并测试
```bash
chmod +x restart_and_test_v24.sh
./restart_and_test_v24.sh
```

### 手动重启
```bash
# 停止旧服务
pkill -f search_srv_pipeline_optimized.py

# 启动新服务
nohup python3 search_srv_pipeline_optimized.py > service.log 2>&1 &

# 验证版本
curl http://10.70.223.31:9510/api-rqa-search/health | grep version
```

### 运行测试
```bash
python3 test_optimized_service.py 10.70.223.31 9510
```

### 查看日志
```bash
# 实时日志
tail -f service.log

# 查看最近错误
grep "⚠️\|❌\|⏱️" service.log

# 查看成功请求
grep "✅ Completed" service.log

# 查看所有完成请求
grep "Completed in" service.log
```

---

## 📊 测试结果解读

### 成功标准

```bash
✅ HTTP成功: 20/20              # 所有请求到达服务器
✅ 业务成功: ≥14/20 (70%)       # 大部分请求成功返回结果
✅ 统计准确性: 100%              # 请求数统计准确
✅ 请求ID冲突: 0                # 无ID冲突
```

### 日志标识

```
✅ = 成功 (code=0)
⚠️  = 警告 (code=1, 查询不相关)
⏱️  = 超时 (code=-2)
❌ = 错误 (code<0, 非超时错误)
```

---

## 🔍 故障排查

### 问题1：版本不对

**症状：**
```bash
curl http://10.70.223.31:9510/api-rqa-search/health
# 输出: "version": "2023091110_adaptive"  # ❌ 旧版本
```

**解决：**
```bash
# 确认当前运行的进程
ps aux | grep search_srv_pipeline

# 杀死所有相关进程
pkill -f search_srv_pipeline

# 确认端口没有被占用
lsof -i :9510

# 重新启动正确版本
cd /path/to/optimized/code
nohup python3 search_srv_pipeline_optimized.py > service.log 2>&1 &
```

### 问题2：业务成功率低

**症状：**
```
✅ HTTP成功: 20/20
✅ 业务成功: 7/20  # ❌ <50%
```

**排查步骤：**

1. **查看失败原因**
```bash
grep "Completed in" service.log | grep -v "code=0"
```

2. **检查重排服务**
```bash
# 如果看到大量 "Reranking completed in 20+s"
# 说明重排服务是瓶颈
grep "Reranking completed" service.log
```

3. **检查编码服务**
```bash
# 查看编码失败
grep "Encoding.*failed\|Invalid embedding" service.log
```

4. **检查召回服务**
```bash
# 查看召回失败
grep "No chunks recalled" service.log
```

**优化建议：**
```bash
# 降低并发数
# 编辑 test_optimized_service.py
num_requests=10  # 从20降到10
max_workers=3    # 从5降到3
```

### 问题3：阶段统计为空

**症状：**
```
⚠️  阶段统计数据为空
```

**可能原因：**
1. 测试请求太少（<10个）
2. 请求太快结束（还没来得及统计）

**解决：**
```bash
# 多测试几次
for i in {1..3}; do
    python3 test_optimized_service.py 10.70.223.31 9510
    sleep 5
done

# 再查看统计
curl http://10.70.223.31:9510/api-rqa-search/stats
```

### 问题4：请求超时

**症状：**
```
[Request 123] ⏱️  Completed in 90.12s, code=-2, msg="Request timeout"
```

**原因分析：**
- 重排服务慢（10-22秒/批次）
- 召回服务慢（偶尔10+秒）
- 并发过高导致排队

**解决：**

1. **增加超时时间**
```python
# test_optimized_service.py
timeout=120  # 从90秒增到120秒
```

2. **降低并发**
```python
max_workers=3  # 从5降到3
```

3. **优化服务配置**
```python
# search_srv_pipeline_optimized.py
rerank_limit = 200  # 从300降到200
recall_limit = 2000  # 从3000降到2000
```

---

## 📈 性能优化

### 当前性能指标（v2.4）

```
平均响应时间: 2.55秒
业务成功率: 35%
重排耗时: 10-22秒（瓶颈）
召回耗时: 0.5-10秒
编码耗时: 0.1-0.2秒
```

### 优化目标

```
平均响应时间: <5秒
业务成功率: >80%
重排耗时: <10秒
召回耗时: <2秒
编码耗时: <0.5秒
```

### 优化方案

#### 1. 立即可行（无需改代码）

```bash
# 1. 降低测试并发
num_requests=10
max_workers=3

# 2. 增加超时
timeout=120

# 3. 减少返回文档
top_doc_num=2  # 从3降到2
```

#### 2. 服务端配置调整

```python
# search_srv_pipeline_optimized.py

# 减少重排数量
rerank_limit = 200  # 第248行

# 减少召回数量
limit = min(recall_limit, 2000)  # 第397行

# 降低过载阈值
if current_active > 30:  # 从50降到30（第588行）
```

#### 3. 外部服务优化

```bash
# 1. 扩容重排服务（最重要！）
# - 增加实例数
# - 或优化算法

# 2. 优化Milvus
# - 调整索引参数
# - 增加内存

# 3. 扩容编码服务
# - 增加实例
# - 添加负载均衡
```

---

## 📋 版本对比

| 功能 | v2.0之前 | v2.0 | v2.1 | v2.2 | v2.3 | v2.4 |
|------|----------|------|------|------|------|------|
| 请求ID生成 | ❌ 冲突 | ✅ 原子 | ✅ | ✅ | ✅ | ✅ |
| 统计准确性 | ❌ 0.2% | ✅ 100% | ✅ | ✅ | ✅ | ✅ |
| 详细日志 | ❌ 无 | ❌ | ✅ | ✅ | ✅ | ✅ |
| 错误可见 | ❌ 静默 | ❌ | ❌ | ✅ | ✅ | ✅ |
| 数据验证 | ❌ 无 | ❌ | ❌ | ❌ | ✅ | ✅ |
| 过载保护 | ❌ 无 | ❌ | ❌ | ✅ | ✅ | ✅ |
| 完成日志 | ❌ 简单 | ❌ | ❌ | ❌ | ❌ | ✅ |
| 业务成功率 | 5% | 5% | 15% | 25% | 35% | 35%+ |

---

## 🎯 当前状态

### ✅ 已完成
- [x] 修复请求ID冲突（v2.0）
- [x] 确保统计准确性（v2.0）
- [x] 添加详细日志（v2.1）
- [x] 强制错误打印（v2.2）
- [x] 严格数据验证（v2.3）
- [x] 请求过载保护（v2.3）
- [x] 完善完成日志（v2.4）

### 🔄 进行中
- [ ] 提升业务成功率（35% → 80%）
- [ ] 优化重排性能（10-22秒 → <10秒）
- [ ] 完善阶段统计

### 📅 计划中
- [ ] 添加请求队列
- [ ] 添加结果缓存
- [ ] 分布式部署

---

## 📞 支持信息

### 主要文件

```
search_srv_pipeline_optimized.py  # 主服务（v2.4）
test_optimized_service.py         # 测试脚本（v1.2）
restart_and_test_v24.sh           # 一键重启脚本
TEST_RESULTS_ANALYSIS.md          # 测试结果分析
QUICK_REFERENCE_v24.md            # 本文件
```

### 常用命令

```bash
# 查看服务状态
curl http://10.70.223.31:9510/api-rqa-search/health | jq

# 查看统计数据
curl http://10.70.223.31:9510/api-rqa-search/stats | jq

# 手动测试单个请求
curl -X POST http://10.70.223.31:9510/api-rqa-search/search \
  -d "query=semiconductor&id=1&top_doc_num=3"

# 查看进程
ps aux | grep search_srv_pipeline

# 查看端口
lsof -i :9510
```

---

**文档版本：v2.4**
**更新时间：2025-12-10 18:00**
**状态：✅ 可用**
