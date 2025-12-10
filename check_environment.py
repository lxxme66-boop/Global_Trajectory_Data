#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
检查环境问题
"""

import sys
import os

print('=' * 80)
print('🔍 环境诊断')
print('=' * 80)

print('\n1️⃣ Python环境:')
print(f'   版本: {sys.version}')
print(f'   可执行文件: {sys.executable}')
print(f'   编码: stdout={sys.stdout.encoding}, stderr={sys.stderr.encoding}')

print('\n2️⃣ 输出设备:')
print(f'   stdout是终端: {sys.stdout.isatty()}')
print(f'   stderr是终端: {sys.stderr.isatty()}')
print(f'   stdout缓冲: {sys.stdout.line_buffering}')

print('\n3️⃣ 环境变量:')
unbuffered = os.environ.get('PYTHONUNBUFFERED', '未设置')
print(f'   PYTHONUNBUFFERED: {unbuffered}')

print('\n4️⃣ 测试输出:')
print('   测试print...', end='', flush=True)
print(' ✅')
sys.stdout.write('   测试stdout...')
sys.stdout.flush()
sys.stdout.write(' ✅\n')
sys.stderr.write('   测试stderr... ✅\n')

print('\n5️⃣ 诊断结果:')
issues = []

if not sys.stdout.isatty():
    issues.append('stdout不是终端（可能被重定向）')
if os.environ.get('PYTHONUNBUFFERED') != '1':
    issues.append('PYTHONUNBUFFERED未设置（可能有缓冲）')
if sys.stdout.encoding.lower() not in ['utf-8', 'utf8']:
    issues.append(f'stdout编码不是UTF-8 (当前: {sys.stdout.encoding})')

if issues:
    print('   ⚠️  发现问题:')
    for issue in issues:
        print(f'      - {issue}')
else:
    print('   ✅ 环境正常')

print('\n6️⃣ 建议:')
if not sys.stdout.isatty():
    print('   - 不要使用 nohup 或 & 后台运行')
    print('   - 不要重定向输出 (> log.txt)')
    print('   - 直接在终端前台运行')
if os.environ.get('PYTHONUNBUFFERED') != '1':
    print('   - 设置环境变量: export PYTHONUNBUFFERED=1')
    print('   - 或使用: python -u script.py')

print('\n' + '=' * 80)
