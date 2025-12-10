#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
修复请求ID生成器
解决高并发时的ID冲突问题
"""

import threading
import time
import uuid

# ==================== 方案1: 线程安全的原子计数器 ====================
class AtomicRequestIDGenerator:
    """线程安全的原子计数器"""
    def __init__(self, prefix=None):
        self._counter = 0
        self._lock = threading.Lock()
        self._prefix = prefix or int(time.time())
    
    def generate(self):
        """生成唯一的请求ID"""
        with self._lock:
            self._counter += 1
            # 使用时间戳前缀 + 计数器，确保唯一性
            return f"{self._prefix}_{self._counter}"
    
    def generate_int(self):
        """生成整数型ID（适配现有代码）"""
        with self._lock:
            self._counter += 1
            # 时间戳(10位) + 计数器(6位) = 16位数字
            return int(f"{int(time.time())}{self._counter:06d}")

# ==================== 方案2: UUID（最安全但较长）====================
def generate_uuid_request_id():
    """使用UUID生成唯一ID"""
    return str(uuid.uuid4())

def generate_short_uuid():
    """生成短UUID（只取前8位）"""
    return str(uuid.uuid4())[:8]

# ==================== 方案3: 时间戳+随机数+线程ID ====================
import random

class HybridRequestIDGenerator:
    """混合ID生成器（时间戳+随机数+线程ID）"""
    def __init__(self):
        self._lock = threading.Lock()
        self._last_timestamp = 0
        self._sequence = 0
    
    def generate(self):
        """生成唯一ID"""
        with self._lock:
            timestamp = int(time.time() * 1000)  # 毫秒
            
            # 如果在同一毫秒内，增加序列号
            if timestamp == self._last_timestamp:
                self._sequence += 1
            else:
                self._sequence = 0
                self._last_timestamp = timestamp
            
            # 格式: 时间戳(13位) + 序列号(4位) + 线程ID(4位)
            thread_id = threading.get_ident() % 10000
            return f"{timestamp}{self._sequence:04d}{thread_id:04d}"
    
    def generate_int(self):
        """生成整数ID"""
        return int(self.generate())

# ==================== 全局实例 ====================
ATOMIC_ID_GENERATOR = AtomicRequestIDGenerator()
HYBRID_ID_GENERATOR = HybridRequestIDGenerator()

# ==================== 测试代码 ====================
def test_generators():
    """测试各种生成器"""
    from collections import Counter
    
    def test_generator(name, generator_func, iterations=1000, threads=10):
        print(f"\n{'='*60}")
        print(f"测试: {name}")
        print(f"{'='*60}")
        
        ids = []
        
        def generate_ids():
            for _ in range(iterations // threads):
                ids.append(generator_func())
        
        # 并发测试
        start = time.time()
        thread_list = []
        for _ in range(threads):
            t = threading.Thread(target=generate_ids)
            thread_list.append(t)
            t.start()
        
        for t in thread_list:
            t.join()
        
        duration = time.time() - start
        
        # 统计
        counter = Counter(ids)
        duplicates = {id: count for id, count in counter.items() if count > 1}
        
        print(f"生成数量: {len(ids)}")
        print(f"唯一数量: {len(counter)}")
        print(f"冲突数量: {len(duplicates)}")
        print(f"冲突率: {len(duplicates) / len(counter) * 100:.2f}%" if counter else "N/A")
        print(f"耗时: {duration:.4f}秒")
        print(f"QPS: {len(ids) / duration:.0f}")
        
        if duplicates:
            print(f"\n⚠️  发现冲突:")
            for id, count in list(duplicates.items())[:3]:
                print(f"  ID {id}: 出现 {count} 次")
        else:
            print(f"✅ 无冲突")
        
        return len(duplicates) == 0
    
    # 测试旧方案
    def old_generator():
        return int(time.time() * 1000) % 1000000
    
    # 开始测试
    results = {}
    
    results['旧方案（有问题）'] = test_generator(
        '旧方案: int(time.time() * 1000) % 1000000',
        old_generator
    )
    
    results['方案1: 原子计数器'] = test_generator(
        '方案1: 原子计数器 (推荐)',
        ATOMIC_ID_GENERATOR.generate
    )
    
    results['方案2: 混合ID'] = test_generator(
        '方案2: 混合ID (时间戳+序列号+线程ID)',
        HYBRID_ID_GENERATOR.generate
    )
    
    results['方案3: UUID'] = test_generator(
        '方案3: UUID (最安全)',
        generate_uuid_request_id
    )
    
    results['方案4: 短UUID'] = test_generator(
        '方案4: 短UUID',
        generate_short_uuid
    )
    
    # 总结
    print(f"\n{'='*60}")
    print(f"测试总结")
    print(f"{'='*60}")
    for name, passed in results.items():
        status = "✅ 通过" if passed else "❌ 失败"
        print(f"{name}: {status}")
    
    print(f"\n推荐方案: 方案1 (原子计数器) 或 方案2 (混合ID)")
    print(f"原因: 无冲突 + 高性能 + 易于调试")

if __name__ == '__main__':
    test_generators()
