#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
快速验证服务版本
"""

import requests
import sys

def verify_version(host='10.70.223.31', port=9510):
    """验证服务版本"""
    print('🔍 验证服务版本...')
    print('=' * 70)
    
    try:
        # 检查健康接口
        response = requests.get(f'http://{host}:{port}/api-rqa-search/health', timeout=5)
        health = response.json().get('data', {})
        
        version = health.get('version', 'unknown')
        request_id_fix = health.get('request_id_fix', 'unknown')
        
        print(f'服务地址: http://{host}:{port}')
        print(f'版本: {version}')
        print(f'请求ID修复: {request_id_fix}')
        print()
        
        # 判断是否是新版本
        if 'fixed' in version and request_id_fix == 'atomic_counter':
            print('✅ 正在运行优化后的新版本！')
            print('✅ 请求ID冲突已修复！')
            return True
        else:
            print('❌ 正在运行旧版本！')
            print()
            print('📝 请执行以下步骤：')
            print('   1. 停止旧服务: pkill -9 -f search_srv_pipeline')
            print('   2. 启动新服务: python3 search_srv_pipeline_optimized.py --port 9510')
            print('   3. 重新测试')
            return False
            
    except Exception as e:
        print(f'❌ 无法连接到服务: {e}')
        print()
        print('📝 请检查：')
        print('   1. 服务是否已启动')
        print('   2. 端口是否正确')
        print('   3. 防火墙是否开放')
        return False

if __name__ == '__main__':
    host = sys.argv[1] if len(sys.argv) > 1 else '10.70.223.31'
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9510
    
    success = verify_version(host, port)
    sys.exit(0 if success else 1)
