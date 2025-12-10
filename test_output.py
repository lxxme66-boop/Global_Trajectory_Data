#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试输出是否正常
"""

import sys
import time

print('=' * 80, flush=True)
print('🔍 测试控制台输出', flush=True)
print('=' * 80, flush=True)

# 测试1: 基本输出
print('\n1️⃣ 基本输出测试...', flush=True)
for i in range(5):
    print(f'   输出 {i+1}/5', flush=True)
    time.sleep(0.5)

print('   ✅ 基本输出正常', flush=True)

# 测试2: 实时输出
print('\n2️⃣ 实时输出测试（模拟监控）...', flush=True)
for i in range(5):
    sys.stdout.write(f'\r   进度: {i+1}/5 ({(i+1)*20}%)')
    sys.stdout.flush()
    time.sleep(0.5)
print('\n   ✅ 实时输出正常', flush=True)

# 测试3: 错误输出
print('\n3️⃣ 错误输出测试...', flush=True)
sys.stderr.write('   这是错误输出\n')
sys.stderr.flush()
print('   ✅ 错误输出正常', flush=True)

print('\n' + '=' * 80, flush=True)
print('✅ 输出测试完成', flush=True)
print('=' * 80, flush=True)

print('\n如果你能看到这些输出，说明控制台正常工作', flush=True)
print('如果看不到，可能原因：', flush=True)
print('  1. 进程在后台运行（使用 fg 调到前台）', flush=True)
print('  2. 输出被重定向到文件', flush=True)
print('  3. 终端问题（尝试重启终端）', flush=True)
