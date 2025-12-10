#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
搜索服务诊断版本 - 添加详细的请求追踪
用于诊断为什么大量请求没有真正执行
"""

import sys
import time
from flask import Flask, request, jsonify
import threading

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# ==================== 增强型监控 ====================
class DetailedMonitor:
    """详细监控器 - 记录所有请求路径"""
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        self.rejection_reasons = {}
        
    def log_request(self, req_id, event, detail=''):
        """记录请求事件"""
        with self.lock:
            timestamp = time.time()
            self.requests.append({
                'req_id': req_id,
                'event': event,
                'detail': detail,
                'timestamp': timestamp
            })
            
            # 只保留最近100条
            if len(self.requests) > 100:
                self.requests = self.requests[-100:]
    
    def log_rejection(self, reason):
        """记录拒绝原因"""
        with self.lock:
            self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1
    
    def get_report(self):
        """获取诊断报告"""
        with self.lock:
            # 统计事件类型
            event_counts = {}
            for req in self.requests:
                event = req['event']
                event_counts[event] = event_counts.get(event, 0) + 1
            
            return {
                'total_logged_requests': len(self.requests),
                'event_counts': event_counts,
                'rejection_reasons': self.rejection_reasons.copy(),
                'recent_requests': self.requests[-10:]  # 最近10条
            }

DETAILED_MONITOR = DetailedMonitor()

# ==================== 模拟主搜索函数 ====================
@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    """搜索服务 - 诊断版本"""
    req_id = int(time.time() * 1000) % 1000000
    
    DETAILED_MONITOR.log_request(req_id, 'RECEIVED', 'Request received')
    
    try:
        form = request.form
        query = form.get('query', '', str)
        query_en = form.get('query_dst', '', str)
        id = form.get('id', 0, int)
        top_doc_num = form.get('top_doc_num', 0, int)
        
        DETAILED_MONITOR.log_request(req_id, 'PARSED', f'query={query[:50]}, id={id}, top_doc_num={top_doc_num}')
        
        # 参数校验
        if query == '':
            DETAILED_MONITOR.log_request(req_id, 'REJECTED', 'query is empty')
            DETAILED_MONITOR.log_rejection('EMPTY_QUERY')
            return jsonify({'code': -1, 'msg': 'query must not be null.', 'data': None})
        
        if id == 0:
            DETAILED_MONITOR.log_request(req_id, 'REJECTED', 'id is 0')
            DETAILED_MONITOR.log_rejection('MISSING_ID')
            return jsonify({'code': -1, 'msg': 'id must not be null.', 'data': None})
        
        if top_doc_num == 0:
            DETAILED_MONITOR.log_request(req_id, 'REJECTED', 'top_doc_num is 0')
            DETAILED_MONITOR.log_rejection('MISSING_TOP_DOC_NUM')
            return jsonify({'code': -1, 'msg': 'top_doc_num must not be null.', 'data': None})
        
        DETAILED_MONITOR.log_request(req_id, 'VALIDATED', 'All parameters valid')
        
        # 模拟搜索流程
        DETAILED_MONITOR.log_request(req_id, 'ENCODING', 'Encoding query')
        time.sleep(0.1)  # 模拟编码耗时
        
        DETAILED_MONITOR.log_request(req_id, 'RECALL', 'Recalling documents')
        time.sleep(0.1)  # 模拟召回耗时
        
        DETAILED_MONITOR.log_request(req_id, 'RANKING', 'Ranking results')
        time.sleep(0.1)  # 模拟排序耗时
        
        DETAILED_MONITOR.log_request(req_id, 'COMPLETED', 'Request completed successfully')
        
        return jsonify({
            'code': 0,
            'msg': 'Success',
            'data': {
                'req_id': req_id,
                'doc_num': 5,
                'arr': []
            }
        })
        
    except Exception as e:
        DETAILED_MONITOR.log_request(req_id, 'ERROR', str(e)[:200])
        return jsonify({'code': -1, 'msg': str(e), 'data': None})

@app.route('/api-rqa-search/diagnose', methods=['GET'])
def get_diagnose():
    """获取诊断报告"""
    report = DETAILED_MONITOR.get_report()
    return jsonify({
        'code': 0,
        'msg': 'Diagnostic report',
        'data': report
    })

@app.route('/api-rqa-search/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({
        'code': 0,
        'msg': 'Service is running',
        'data': {'status': 'healthy'}
    })

if __name__ == '__main__':
    print('🔍 启动诊断服务...')
    print('访问 /api-rqa-search/diagnose 查看详细诊断信息')
    app.run(host='0.0.0.0', port=9599, threaded=True, debug=False)
