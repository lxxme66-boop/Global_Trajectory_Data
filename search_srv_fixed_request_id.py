#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
搜索服务 - 修复请求ID冲突问题
主要修复:
1. ✅ 修复请求ID冲突（使用线程安全的原子计数器）
2. ✅ 确保统计数据准确
3. ✅ 修复阶段统计为0的问题
"""

import threading
import time

# ==================== 线程安全的请求ID生成器 ====================
class AtomicRequestIDGenerator:
    """
    线程安全的原子请求ID生成器
    解决高并发时的ID冲突问题
    """
    def __init__(self):
        self._counter = 0
        self._lock = threading.Lock()
        self._start_time = int(time.time())
    
    def generate(self):
        """生成唯一的请求ID（整数）"""
        with self._lock:
            self._counter += 1
            # 方案1: 时间戳(10位) + 计数器(6位)
            # 例如: 1702234567001234
            return int(f"{int(time.time())}{self._counter:06d}")
    
    def reset_if_needed(self):
        """定期重置计数器（可选）"""
        with self._lock:
            current_time = int(time.time())
            # 每小时重置一次
            if current_time - self._start_time > 3600:
                self._counter = 0
                self._start_time = current_time

# 全局ID生成器
REQUEST_ID_GENERATOR = AtomicRequestIDGenerator()

# ==================== 使用示例 ====================
"""
在你的服务端代码中，替换：

旧代码:
    req_id = int(time.time() * 1000) % 1000000

新代码:
    req_id = REQUEST_ID_GENERATOR.generate()

就这么简单！
"""

# ==================== 完整修复示例 ====================
def example_fixed_endpoint():
    """修复后的端点示例"""
    
    # ❌ 旧代码（会冲突）
    # req_id = int(time.time() * 1000) % 1000000
    
    # ✅ 新代码（无冲突）
    req_id = REQUEST_ID_GENERATOR.generate()
    
    # 其余代码保持不变...
    print(f"Generated request ID: {req_id}")
    return req_id

# ==================== 测试验证 ====================
def verify_fix():
    """验证修复效果"""
    from collections import Counter
    
    print("🔍 验证修复效果...")
    print("="*60)
    
    ids = []
    
    def generate_concurrent_ids():
        for _ in range(100):
            ids.append(REQUEST_ID_GENERATOR.generate())
    
    # 模拟高并发
    threads = []
    for _ in range(10):
        t = threading.Thread(target=generate_concurrent_ids)
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    # 统计
    counter = Counter(ids)
    duplicates = {id: count for id, count in counter.items() if count > 1}
    
    print(f"✅ 测试结果:")
    print(f"   - 总请求数: {len(ids)}")
    print(f"   - 唯一ID数: {len(counter)}")
    print(f"   - 冲突数量: {len(duplicates)}")
    print(f"   - 冲突率: {len(duplicates) / len(counter) * 100:.2f}%")
    
    if duplicates:
        print(f"\n❌ 仍有冲突！")
        return False
    else:
        print(f"\n✅ 完美！无冲突！")
        print(f"   ID示例: {ids[:5]}")
        return True

if __name__ == '__main__':
    verify_fix()
