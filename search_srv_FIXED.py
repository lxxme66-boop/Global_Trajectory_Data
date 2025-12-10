#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
搜索服务 - 修复版本
主要修复：
1. 修复监控统计不准确的问题（参数校验失败时也要更新监控）
2. 使用try-finally确保监控总是被调用
3. 添加详细的请求日志
4. 修复参数解析问题（同时支持json和form data）
"""

import sys
import time
import traceback
from flask import Flask, request, jsonify

# 这里只展示关键修复部分，完整代码需要保留原有的导入和函数定义

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# 假设这些是全局变量（从原代码复制）
# MONITOR = RequestMonitor()
# TIMEOUT_MANAGER = AdaptiveTimeoutManager()
# ...其他全局变量...

@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    """
    搜索服务主函数 - 修复版本
    
    修复内容：
    1. 使用try-finally确保监控总是被更新
    2. 同时支持json和form参数
    3. 添加详细日志
    """
    req_id = int(time.time() * 1000) % 1000000
    success = False
    start_time = time.time()
    
    # 初始化返回数据
    code = 0
    msg = ''
    data = {
        'model': MODEL_NAME,
        'version': VERSION,
        'request_id': req_id
    }
    
    # ========== 修复1: 先启动监控 ==========
    MONITOR.start_request(req_id, '')  # query暂时为空，后面更新
    TIMEOUT_MANAGER.enter_request()
    
    try:
        # ========== 修复2: 支持多种参数格式 ==========
        # 优先使用form data，如果没有则尝试json
        if request.form:
            # Form data (原有方式)
            query = request.form.get('query', '', str)
            query_en = request.form.get('query_dst', '', str)
            id = request.form.get('id', 0, int)
            top_doc_num = request.form.get('top_doc_num', 0, int)
            target_doc_name = request.form.get('target_doc_name', '', str)
            is_delete = request.form.get('is_delete', 0, int)
            delete_doc_id = request.form.get('delete_doc_id', '', str)
            is_debug = request.form.get('debug', 0, int) == 1
        elif request.is_json:
            # JSON data (备选方式)
            json_data = request.get_json()
            query = json_data.get('query', '')
            query_en = json_data.get('query_dst', '')
            id = json_data.get('id', 0)
            top_doc_num = json_data.get('top_doc_num', 0)
            target_doc_name = json_data.get('target_doc_name', '')
            is_delete = json_data.get('is_delete', 0)
            delete_doc_id = json_data.get('delete_doc_id', '')
            is_debug = json_data.get('debug', 0) == 1
        else:
            # 没有参数
            query = ''
            query_en = ''
            id = 0
            top_doc_num = 0
            target_doc_name = ''
            is_delete = 0
            delete_doc_id = ''
            is_debug = False
        
        # ========== 修复3: 更新监控中的query ==========
        if query:
            with MONITOR.lock:
                if req_id in MONITOR.active_requests:
                    MONITOR.active_requests[req_id]['query'] = query[:100]
        
        # ========== 修复4: 参数校验失败时记录原因 ==========
        if not query:
            code = -1
            msg = 'query must not be null.'
            data['doc_num'] = 0
            data['arr'] = []
            data['validation_error'] = 'EMPTY_QUERY'
            print(f'[Request {req_id}] ⚠️  Rejected: empty query')
            return jsonify({'code': code, 'msg': msg, 'data': data})
        
        if not id:
            code = -1
            msg = 'id must not be null.'
            data['doc_num'] = 0
            data['arr'] = []
            data['validation_error'] = 'MISSING_ID'
            print(f'[Request {req_id}] ⚠️  Rejected: missing id')
            return jsonify({'code': code, 'msg': msg, 'data': data})
        
        if not top_doc_num:
            code = -1
            msg = 'top_doc_num must not be null.'
            data['doc_num'] = 0
            data['arr'] = []
            data['validation_error'] = 'MISSING_TOP_DOC_NUM'
            print(f'[Request {req_id}] ⚠️  Rejected: missing top_doc_num')
            return jsonify({'code': code, 'msg': msg, 'data': data})
        
        # 处理删除请求
        if is_delete == 1 and delete_doc_id:
            try:
                delete_doc_id_list = [int(t) for t in delete_doc_id.split(',')]
                res = MONGO_PIPELINE.collection.delete_many({
                    'doc_id': {'$in': delete_doc_id_list}
                })
                success = True
                return jsonify({
                    'code': 0,
                    'msg': f'deleted {res.deleted_count} documents',
                    'data': None
                })
            except Exception as e:
                code = -1
                msg = f'delete failed: {e}'
                return jsonify({'code': code, 'msg': msg, 'data': None})
        
        print(f'[Request {req_id}] ✅ Validated: query="{query[:50]}...", id={id}, top_doc_num={top_doc_num}')
        
        # ========== 执行搜索逻辑（原有代码） ==========
        
        # 阶段1: 编码
        MONITOR.update_stage(req_id, 'encoding')
        
        query_embed = safe_execute(
            lambda: encode_from_net_cached(query),
            timeout=TIMEOUT_MANAGER.get_timeout('encoding'),
            default=None,
            operation_name="Encoding"
        )
        
        if query_embed is None:
            raise Exception("Encoding failed")
        
        MONITOR.end_stage(req_id, 'encoding')
        
        # 准备参数
        result_dict = {}
        params = {
            'id': id,
            'query': query,
            'query_zh': query if has_chinese(query) else (query_en if query_en else query),
            'query_en': query_en if query_en and not has_chinese(query) else query,
            'query_expand': [query],
            'query_embed': query_embed,
            'query_embed_expand': [query_embed],
            'query_rank_embed': query_embed,
            'query_rank_embed_expand': [query_embed],
            'target_doc_name': target_doc_name,
            'flag_query_rel': True,
            'result_dict': result_dict
        }
        
        # 阶段2: 召回
        MONITOR.update_stage(req_id, 'recall')
        recall_result = safe_execute(
            lambda: recall_pipeline(**params),
            timeout=TIMEOUT_MANAGER.get_timeout('recall'),
            default=None,
            operation_name="Recall"
        )
        
        if recall_result is None:
            raise Exception("Recall failed")
        
        params.update(recall_result)
        MONITOR.end_stage(req_id, 'recall')
        
        # 检查查询相关性
        if not params['flag_query_rel']:
            code = 1
            msg = 'query must be relevant'
            data['doc_num'] = 0
            data['arr'] = []
            return jsonify({'code': code, 'msg': msg, 'data': data})
        
        # 阶段3: 排序
        MONITOR.update_stage(req_id, 'ranking')
        rank_result = safe_execute(
            lambda: rank_pipeline(**params),
            timeout=TIMEOUT_MANAGER.get_timeout('rank'),
            default=None,
            operation_name="Ranking"
        )
        
        if rank_result is None:
            print(f'[Request {req_id}] Ranking failed, using recall results')
        else:
            params.update(rank_result)
        
        MONITOR.end_stage(req_id, 'ranking')
        
        # 阶段4: 重排
        MONITOR.update_stage(req_id, 'reranking')
        rerank_result = safe_execute(
            lambda: rerank_pipeline(**params),
            timeout=TIMEOUT_MANAGER.get_timeout('rerank_total'),
            default=None,
            operation_name="Reranking"
        )
        
        if rerank_result is None:
            print(f'[Request {req_id}] Reranking failed, using previous results')
        else:
            params.update(rerank_result)
        
        MONITOR.end_stage(req_id, 'reranking')
        
        # 阶段5: 拼接
        MONITOR.update_stage(req_id, 'concatenating')
        params['top_doc_num'] = top_doc_num
        params['concat_num'] = CONCAT_CHUNK_NUM
        params['is_debug'] = is_debug
        
        similar_shards = safe_execute(
            lambda: concat_shards_by_rank(**params),
            timeout=60,
            default=[],
            operation_name="Concatenation"
        )
        
        MONITOR.end_stage(req_id, 'concatenating')
        
        # 构造结果
        json_arr = []
        for dict_item in similar_shards:
            score = dict_item.get('score', 0)
            if score < SCORE_THREHOLD:
                continue
            json_arr.append(dict_item)
        
        code = 0
        msg = ''
        data['arr'] = json_arr
        data['doc_num'] = len(json_arr)
        
        # 标记成功
        success = True
        
        print(f'[Request {req_id}] ✅ Completed: {len(json_arr)} results in {time.time() - start_time:.2f}s')
        
    except Exception as e:
        code = -1
        msg = str(e)[:500]
        data['msg'] = msg
        data['doc_num'] = 0
        data['arr'] = []
        print(f'[Request {req_id}] ❌ Error: {msg}')
        if DEBUG_MODE:
            print(traceback.format_exc()[:500])
    
    finally:
        # ========== 修复5: 无论如何都更新监控 ==========
        MONITOR.end_request(req_id, success=success)
        TIMEOUT_MANAGER.exit_request()
    
    # 添加时间戳和处理时间
    data['ts'] = int(time.time() * 1000)
    data['process_time'] = time.time() - start_time
    
    return jsonify({'code': code, 'msg': msg, 'data': data})


# ========== 修复6: 统计接口增强 ==========
@app.route('/api-rqa-search/stats', methods=['GET'])
def get_stats():
    """获取服务统计信息 - 增强版"""
    stats = MONITOR.get_stats()
    stats['encode_cache'] = ENCODE_CACHE.stats()
    stats['service'] = {
        'port': SERVER_PORT,
        'version': VERSION + '_FIXED',  # 标记为修复版本
        'model': MODEL_NAME,
        'workers': WORKER_COUNT,
        'batch_size': MONGO_BATCH_SIZE,
        'debug_mode': DEBUG_MODE,
        'max_request_timeout': MAX_REQUEST_TIMEOUT,
        'timeout_strategy': 'adaptive'
    }
    
    stats['http_sessions'] = HTTP_SESSION.session_count
    stats['active_requests_detail'] = MONITOR.get_active_requests_info()
    stats['stuck_requests'] = MONITOR.get_stuck_requests(threshold=MAX_REQUEST_TIMEOUT)
    
    # ========== 新增：监控健康检查 ==========
    total = stats['total_requests']
    success = stats['successful_requests']
    failed = stats['failed_requests']
    timeout = stats['timeout_requests']
    accounted = success + failed + timeout
    unaccounted = total - accounted
    
    stats['monitoring_health'] = {
        'total_requests': total,
        'accounted_requests': accounted,
        'unaccounted_requests': unaccounted,
        'accounting_rate': f'{accounted/total*100:.1f}%' if total > 0 else '0%',
        'is_healthy': unaccounted == 0
    }
    
    if unaccounted > 0:
        stats['monitoring_warning'] = (
            f'{unaccounted} requests are unaccounted for. '
            f'This may indicate monitoring bugs or requests still in progress.'
        )
    
    return jsonify({'code': 0, 'msg': '', 'data': stats})


# ========== 修复7: 健康检查增强 ==========
@app.route('/api-rqa-search/health', methods=['GET'])
def health_check():
    """健康检查接口 - 增强版"""
    health_status = {
        'status': 'healthy',
        'timestamp': datetime.datetime.now().isoformat(),
        'version': VERSION + '_FIXED',
        'uptime': MONITOR.get_stats()['uptime'],
        'timeout_config': 'adaptive',
        'fixes_applied': [
            'monitoring_coverage',
            'try_finally_pattern',
            'json_support',
            'detailed_logging',
            'validation_tracking'
        ]
    }
    
    # 检查MongoDB连接
    try:
        if MONGO_PIPELINE and MONGO_PIPELINE.client:
            MONGO_PIPELINE.client.admin.command('ping', maxTimeMS=2000)
            health_status['mongodb'] = 'connected'
        else:
            health_status['mongodb'] = 'disconnected'
    except Exception as e:
        health_status['mongodb'] = f'error: {str(e)[:100]}'
    
    # 检查编码服务
    try:
        test_response = HTTP_SESSION.get(ENCODER_URL.replace('/encode', ''), timeout=5)
        health_status['encoder_service'] = 'available' if test_response.status_code == 200 else 'unavailable'
    except Exception as e:
        health_status['encoder_service'] = f'error: {str(e)[:100]}'
    
    # 检查监控健康度
    stats = MONITOR.get_stats()
    total = stats['total_requests']
    if total > 0:
        success = stats['successful_requests']
        failed = stats['failed_requests']
        timeout = stats['timeout_requests']
        accounted = success + failed + timeout
        
        health_status['monitoring'] = {
            'coverage': f'{accounted/total*100:.1f}%',
            'is_tracking_all_requests': accounted == total
        }
    
    return jsonify({'code': 0, 'msg': 'Service is healthy', 'data': health_status})


# 保留原有的其他路由和函数定义...
# @app.route('/api-rqa-search/download', methods=['GET'])
# def download():
#     ...
# 
# def save_download_record(...):
#     ...
# 
# ...其他函数...

if __name__ == '__main__':
    print(f'🚀 [Server FIXED] Starting on {SERVER_HOST}:{SERVER_PORT}...')
    print(f'✨ [Fixes] Applied monitoring coverage and try-finally pattern')
    print(f'✨ [Fixes] Added JSON parameter support')
    print(f'✨ [Fixes] Enhanced logging and validation tracking')
    
    # 加载数据和启动服务（原有代码）
    # load_data(TABLE_NAME, MODEL_NAME)
    # app.run(host=SERVER_HOST, port=SERVER_PORT, threaded=True, processes=1, debug=False)
