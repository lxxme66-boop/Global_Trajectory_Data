#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
启动服务（带强制输出）
"""

import sys
import os
import subprocess
import time

# 强制禁用输出缓冲
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

print('=' * 80, flush=True)
print('🚀 搜索服务启动器（强制输出版本）', flush=True)
print('=' * 80, flush=True)

# 设置环境变量
os.environ['PYTHONUNBUFFERED'] = '1'

print('\n📋 配置信息:', flush=True)
print(f'   - Python版本: {sys.version.split()[0]}', flush=True)
print(f'   - 工作目录: {os.getcwd()}', flush=True)
print(f'   - 输出缓冲: 已禁用', flush=True)

print('\n🔧 启动参数:', flush=True)
args = [
    'python', '-u',
    'search_srv_pipeline_v3_adaptive_timeout.py',
    '--port', '9510',
    '--host', '10.70.223.31',
    '--workers', '4',
    '--request-timeout', '300'
]
print(f'   {" ".join(args)}', flush=True)

print('\n' + '=' * 80, flush=True)
print('📊 服务日志输出:', flush=True)
print('=' * 80, flush=True)
print('', flush=True)

# 启动服务
try:
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        universal_newlines=True
    )
    
    print('✅ 服务进程已启动 (PID: {})'.format(process.pid), flush=True)
    print('', flush=True)
    
    # 实时输出日志
    for line in iter(process.stdout.readline, ''):
        if line:
            print(line.rstrip(), flush=True)
    
    process.wait()
    
except KeyboardInterrupt:
    print('\n\n⚠️  收到中断信号，正在关闭服务...', flush=True)
    process.terminate()
    process.wait()
    print('✅ 服务已关闭', flush=True)
    
except Exception as e:
    print(f'\n❌ 启动失败: {e}', flush=True)
    sys.exit(1)
