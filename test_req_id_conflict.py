#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试请求ID冲突问题
"""

import time
import threading
from collections import Counter

def old_req_id_generator():
    """旧的ID生成方式"""
    return int(time.time() * 1000) % 1000000

def test_collision():
    """测试ID冲突"""
    ids = []
    
    def generate_ids():
        for _ in range(100):
            ids.append(old_req_id_generator())
    
    # 模拟并发
    threads = []
    for _ in range(10):
        t = threading.Thread(target=generate_ids)
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    # 统计冲突
    counter = Counter(ids)
    duplicates = {id: count for id, count in counter.items() if count > 1}
    
    print(f'总共生成: {len(ids)} 个ID')
    print(f'唯一ID数: {len(counter)} 个')
    print(f'冲突数量: {len(duplicates)} 个')
    print(f'冲突率: {len(duplicates) / len(counter) * 100:.2f}%')
    
    if duplicates:
        print(f'\n示例冲突:')
        for id, count in list(duplicates.items())[:5]:
            print(f'  ID {id}: 出现 {count} 次')

if __name__ == '__main__':
    test_collision()
