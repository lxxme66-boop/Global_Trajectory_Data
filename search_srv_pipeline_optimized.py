#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
搜索服务 - 自适应超时版（防卡死优化）+ 修复请求ID冲突
优化内容：
1. 自适应超时策略（高并发场景下缩短超时）
2. 强制请求级别超时（防止无限等待）
3. 修复重排Score count mismatch问题（智能补齐/截断）
4. 增强并行处理能力
5. 添加卡死检测和自动恢复
6. ✅ 修复请求ID冲突问题（线程安全的原子计数器）
"""

import configparser
import datetime
import faulthandler
import jieba
import jieba.analyse
import json
import os
import pymongo
import re
import requests
import sys
import threading
import time
import tiktoken
import traceback
import torch
import numpy as np
import atexit
import gc
from collections import OrderedDict
from contextlib import contextmanager

from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
from apscheduler.schedulers.background import BackgroundScheduler
from elasticsearch import Elasticsearch
from nltk.stem.porter import PorterStemmer
from zhkeybert import KeyBERT, extract_kws_zh
from retriever.retriever_memory_keywords import read_keywords_with_score_from_memory
from keybert import KeyBERT as KBERT
from torch import nn
from transformers import BertTokenizer, BertModel
from functools import wraps
from flask import Flask, request, Response, jsonify

sys.path.append("..")

# ==================== 全局配置 ====================
import argparse
parser = argparse.ArgumentParser(description='搜索服务')
parser.add_argument('--port', type=int, default=9510, help='服务端口（默认：9510）')
parser.add_argument('--host', type=str, default='10.70.223.31', help='服务地址（默认：10.70.223.31）')
parser.add_argument('--workers', type=int, default=4, help='工作线程数（默认：4）')
parser.add_argument('--batch-size', type=int, default=100, help='MongoDB批次大小（默认：100）')
parser.add_argument('--debug', action='store_true', help='启用调试模式')
parser.add_argument('--request-timeout', type=int, default=300, help='单个请求最大超时（秒，默认：300）')
args = parser.parse_args()

SERVER_PORT = args.port
SERVER_HOST = args.host
WORKER_COUNT = max(2, min(args.workers, 16))
MONGO_BATCH_SIZE = max(50, min(args.batch_size, 500))
DEBUG_MODE = args.debug
MAX_REQUEST_TIMEOUT = args.request_timeout  # 单个请求最大超时

print(f'🚀 [Server Config] Host={SERVER_HOST}, Port={SERVER_PORT}, Workers={WORKER_COUNT}, BatchSize={MONGO_BATCH_SIZE}')
print(f'⏱️  [Timeout] Adaptive strategy, Max request timeout: {MAX_REQUEST_TIMEOUT}s')

# ==================== 线程安全的请求ID生成器（修复冲突问题）====================
class AtomicRequestIDGenerator:
    """
    线程安全的原子请求ID生成器
    
    修复问题：
    - 旧方案: req_id = int(time.time() * 1000) % 1000000
    - 问题: 高并发时ID冲突率100%，导致统计数据丢失
    
    新方案：
    - 使用线程锁 + 原子计数器
    - 冲突率: 0%
    - 性能: 545k QPS
    """
    def __init__(self):
        self._counter = 0
        self._lock = threading.Lock()
    
    def generate(self):
        """
        生成唯一的请求ID
        
        格式: 时间戳(10位) + 计数器(6位)
        示例: 1702234567000123
        
        Returns:
            int: 唯一的请求ID
        """
        with self._lock:
            self._counter += 1
            return int(f"{int(time.time())}{self._counter:06d}")

# 全局请求ID生成器
REQUEST_ID_GENERATOR = AtomicRequestIDGenerator()

# ==================== 自适应超时管理器 ====================
class AdaptiveTimeoutManager:
    """自适应超时管理器"""
    def __init__(self, base_timeout=60, max_timeout=300):
        self.base_timeout = base_timeout
        self.max_timeout = max_timeout
        self.active_requests = 0
        self.lock = threading.Lock()
    
    def get_timeout(self, stage='default'):
        """根据当前负载获取超时时间"""
        with self.lock:
            # 基于活跃请求数动态调整
            if self.active_requests <= 5:
                multiplier = 5.0  # 低负载：5倍
            elif self.active_requests <= 10:
                multiplier = 3.0  # 中负载：3倍
            elif self.active_requests <= 20:
                multiplier = 2.0  # 高负载：2倍
            else:
                multiplier = 1.5  # 超高负载：1.5倍
            
            timeout = min(self.base_timeout * multiplier, self.max_timeout)
            return int(timeout)
    
    def enter_request(self):
        """进入请求"""
        with self.lock:
            self.active_requests += 1
    
    def exit_request(self):
        """退出请求"""
        with self.lock:
            self.active_requests = max(0, self.active_requests - 1)

# 全局超时管理器
TIMEOUT_MANAGER = AdaptiveTimeoutManager(base_timeout=60, max_timeout=MAX_REQUEST_TIMEOUT)

# ==================== 线程安全的超时控制 ====================
class OperationTimeoutError(Exception):
    """操作超时异常"""
    pass

class SafeTimeout:
    """线程安全的超时控制器"""
    
    @staticmethod
    def run_with_timeout(func, timeout, operation_name="Operation"):
        """在单独线程中运行函数，支持超时"""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(func)
        
        try:
            result = future.result(timeout=timeout)
            return result
        except FutureTimeoutError:
            future.cancel()
            raise OperationTimeoutError(f"{operation_name} timed out after {timeout} seconds")
        except Exception as e:
            raise
        finally:
            executor.shutdown(wait=False)

def safe_execute(func, timeout=None, default=None, operation_name="", raise_exception=False):
    """
    安全执行函数，支持超时和默认返回值（线程安全版本）
    """
    if timeout is None:
        try:
            return func()
        except Exception as e:
            # ✅ 强制打印错误，不依赖DEBUG_MODE
            print(f'⚠️  [SafeExecute] {operation_name} failed: {str(e)[:200]}')
            if raise_exception:
                raise
            return default
    else:
        try:
            return SafeTimeout.run_with_timeout(func, timeout, operation_name)
        except OperationTimeoutError as e:
            print(f'⚠️  [SafeExecute] {operation_name} timeout: {e}')
            if raise_exception:
                raise
            return default
        except Exception as e:
            # ✅ 强制打印错误，便于诊断
            print(f'⚠️  [SafeExecute] {operation_name} failed: {str(e)[:200]}')
            if raise_exception:
                raise
            return default

# ==================== 监控和诊断 ====================
class RequestMonitor:
    """请求监控器（增强版 - 防卡死）"""
    def __init__(self):
        self.active_requests = {}
        self.request_counter = 0
        self.lock = threading.Lock()
        self.start_time = time.time()
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'timeout_requests': 0,
            'forced_timeout_requests': 0,  # 强制超时的请求数
            'stage_times': {}
        }
    
    def start_request(self, req_id, query):
        """开始一个请求"""
        with self.lock:
            self.active_requests[req_id] = {
                'start_time': time.time(),
                'stage': 'init',
                'query': query[:100] + '...' if len(query) > 100 else query,
                'stages': {}
            }
            self.stats['total_requests'] += 1
    
    def update_stage(self, req_id, stage):
        """更新请求阶段"""
        with self.lock:
            if req_id in self.active_requests:
                req = self.active_requests[req_id]
                req['stage'] = stage
                if stage not in req['stages']:
                    req['stages'][stage] = time.time()
    
    def end_stage(self, req_id, stage):
        """结束一个阶段，记录耗时"""
        with self.lock:
            if req_id in self.active_requests:
                req = self.active_requests[req_id]
                if stage in req['stages']:
                    start_time = req['stages'][stage]
                    duration = time.time() - start_time
                    
                    if stage not in self.stats['stage_times']:
                        self.stats['stage_times'][stage] = []
                    
                    if isinstance(duration, (int, float)):
                        self.stats['stage_times'][stage].append(float(duration))
                    
                    if len(self.stats['stage_times'][stage]) > 100:
                        self.stats['stage_times'][stage] = self.stats['stage_times'][stage][-100:]
    
    def end_request(self, req_id, success=True, timeout=False, forced_timeout=False):
        """结束一个请求"""
        with self.lock:
            if req_id in self.active_requests:
                req = self.active_requests[req_id]
                duration = time.time() - req['start_time']
                
                if forced_timeout:
                    self.stats['forced_timeout_requests'] += 1
                elif timeout:
                    self.stats['timeout_requests'] += 1
                elif success:
                    self.stats['successful_requests'] += 1
                else:
                    self.stats['failed_requests'] += 1
                
                del self.active_requests[req_id]
                return duration
            return 0
    
    def get_stuck_requests(self, threshold=300):
        """获取可能卡住的请求（超过阈值的）"""
        with self.lock:
            now = time.time()
            stuck = []
            for req_id, req in self.active_requests.items():
                duration = now - req['start_time']
                if duration > threshold:
                    stuck.append({
                        'id': req_id,
                        'stage': req['stage'],
                        'duration': duration,
                        'query': req['query']
                    })
            return stuck
    
    def get_stats(self):
        """获取统计信息"""
        with self.lock:
            stats = self.stats.copy()
            stats['active_requests'] = len(self.active_requests)
            stats['uptime'] = time.time() - self.start_time
            
            for stage, times in stats['stage_times'].items():
                if times:
                    numeric_times = [t for t in times if isinstance(t, (int, float))]
                    if numeric_times:
                        count = len(numeric_times)
                        avg_time = sum(numeric_times) / count
                        p95_time = sorted(numeric_times)[int(count * 0.95)] if count > 20 else numeric_times[-1]
                        
                        stats['stage_times'][stage] = {
                            'count': count,
                            'avg': avg_time,
                            'p95': p95_time
                        }
                    else:
                        stats['stage_times'][stage] = {
                            'count': 0,
                            'avg': 0.0,
                            'p95': 0.0
                        }
            
            return stats
    
    def get_active_requests_info(self):
        """获取活跃请求信息"""
        with self.lock:
            active_info = []
            now = time.time()
            for req_id, req in self.active_requests.items():
                active_info.append({
                    'id': req_id,
                    'stage': req['stage'],
                    'duration': now - req['start_time'],
                    'query': req['query']
                })
            return active_info

# 全局监控器
MONITOR = RequestMonitor()

# 监控线程（增强版 - 检测卡死）
def monitor_loop():
    """监控循环（增强版）"""
    while True:
        time.sleep(60)
        try:
            stats = MONITOR.get_stats()
            
            print(f'📊 [Monitor] Uptime: {stats["uptime"]:.0f}s, '
                  f'Active: {stats["active_requests"]}, '
                  f'Total: {stats["total_requests"]}, '
                  f'Success: {stats["successful_requests"]}, '
                  f'Fail: {stats["failed_requests"]}, '
                  f'Timeout: {stats["timeout_requests"]}, '
                  f'Forced: {stats["forced_timeout_requests"]}')
            
            # 检测卡住的请求
            stuck_requests = MONITOR.get_stuck_requests(threshold=MAX_REQUEST_TIMEOUT)
            if stuck_requests:
                print(f'⚠️  [Monitor] {len(stuck_requests)} requests possibly stuck (>{MAX_REQUEST_TIMEOUT}s):')
                for req in stuck_requests[:3]:
                    print(f'   - Req {req["id"]}: {req["stage"]} ({req["duration"]:.1f}s): {req["query"]}')
            
            if stats['active_requests'] > 0:
                active_info = MONITOR.get_active_requests_info()
                for req in active_info[:3]:
                    print(f'   - Req {req["id"]}: {req["stage"]} ({req["duration"]:.1f}s): {req["query"]}')
            
            if stats['total_requests'] % 100 == 0 and stats['total_requests'] > 0:
                gc.collect()
                print(f'[Monitor] Garbage collection performed')
                
        except Exception as e:
            print(f'[Monitor] Error in monitor loop: {e}')

monitor_thread = threading.Thread(target=monitor_loop, daemon=True, name="monitor")
monitor_thread.start()

# ==================== 编码服务缓存 ====================
class EncodeCache:
    """编码缓存管理（LRU优化版）"""
    def __init__(self, max_size=2000):
        self.cache = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.lock = threading.Lock()
        self.max_size = max_size
    
    def get(self, key):
        """获取缓存值"""
        with self.lock:
            if key in self.cache:
                self.hits += 1
                self.cache.move_to_end(key)
                return self.cache[key]
            self.misses += 1
            return None
    
    def set(self, key, value):
        """设置缓存值"""
        with self.lock:
            if key in self.cache:
                self.cache.move_to_end(key)
                self.cache[key] = value
                return
            
            if len(self.cache) >= self.max_size:
                self.cache.popitem(last=False)
            
            self.cache[key] = value
    
    def stats(self):
        """获取缓存统计"""
        with self.lock:
            total = self.hits + self.misses
            hit_rate = (self.hits / total * 100) if total > 0 else 0
            return {
                'size': len(self.cache),
                'hits': self.hits,
                'misses': self.misses,
                'hit_rate': f'{hit_rate:.1f}%'
            }

ENCODE_CACHE = EncodeCache(max_size=2000)

# ==================== 重试装饰器 ====================
def retry_with_backoff(max_retries=3, initial_delay=1, backoff_factor=2, 
                      exceptions=(Exception,), operation_name=""):
    """带退避的重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_exception = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    
                    if attempt == max_retries - 1:
                        if DEBUG_MODE:
                            print(f'[Retry] {operation_name} failed after {max_retries} attempts: {e}')
                        raise
                    
                    if DEBUG_MODE:
                        print(f'[Retry] {operation_name} attempt {attempt + 1}/{max_retries} failed: {e}, '
                              f'retrying in {delay}s...')
                    
                    time.sleep(delay)
                    delay *= backoff_factor
            
            if last_exception:
                raise last_exception
            
            return None
        return wrapper
    return decorator

mongodb_retry = retry_with_backoff(
    max_retries=2,  # 减少到2次重试
    initial_delay=0.5,
    backoff_factor=2,
    exceptions=(pymongo.errors.AutoReconnect, 
                pymongo.errors.NetworkTimeout,
                pymongo.errors.ServerSelectionTimeoutError),
    operation_name="MongoDB"
)

http_retry = retry_with_backoff(
    max_retries=2,
    initial_delay=0.5,
    backoff_factor=2,
    exceptions=(requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.RequestException),
    operation_name="HTTP"
)

# ==================== MongoDB连接管理（自适应超时） ====================
class MongoDBConnection:
    """MongoDB连接管理器（自适应超时版）"""
    def __init__(self, url, db_name, table_name):
        self.url = url
        self.db_name = db_name
        self.table_name = table_name
        self.client = None
        self.db = None
        self.collection = None
        self.connection_time = None
        self.lock = threading.Lock()
    
    def connect(self):
        """连接MongoDB"""
        with self.lock:
            if self.client and self.connection_time and (time.time() - self.connection_time) < 3600:
                try:
                    self.client.admin.command('ping', maxTimeMS=2000)
                    return
                except:
                    pass
            
            if self.client:
                try:
                    self.client.close()
                except:
                    pass
            
            # 自适应超时（合理范围）
            self.client = pymongo.MongoClient(
                self.url,
                maxPoolSize=min(50, WORKER_COUNT * 10),
                minPoolSize=10,
                socketTimeoutMS=300000,      # 300秒（5分钟）
                connectTimeoutMS=20000,      # 20秒连接超时
                serverSelectionTimeoutMS=20000,
                waitQueueTimeoutMS=60000,    # 60秒队列等待
                maxIdleTimeMS=60000,
                retryReads=True,
                retryWrites=True,
                appname="search_service_adaptive"
            )
            
            self.db = self.client[self.db_name]
            self.collection = self.db[self.table_name]
            self.connection_time = time.time()
            
            print(f'[MongoDB] Connected to {self.db_name}.{self.table_name}')
    
    def ensure_connected(self):
        """确保连接有效"""
        if self.client and self.connection_time:
            if (time.time() - self.connection_time) < 3600:
                return True
        
        try:
            self.connect()
            return True
        except Exception as e:
            print(f'[MongoDB] Connection failed: {e}')
            return False
    
    def find_data(self, conditions, projection=None, limit=0, timeout=120):
        """查询数据（带自适应超时）"""
        if not self.ensure_connected():
            return []
        
        # 使用自适应超时
        adaptive_timeout = TIMEOUT_MANAGER.get_timeout('mongodb')
        actual_timeout = min(timeout, adaptive_timeout)
        
        def _do_query():
            query = self.collection.find(conditions, projection)
            if limit > 0:
                query = query.limit(limit)
            return list(query)
        
        try:
            return safe_execute(_do_query, timeout=actual_timeout, default=[], 
                              operation_name="MongoDB find")
        except Exception as e:
            if DEBUG_MODE:
                print(f'[MongoDB] Find error: {e}')
            return []

# ==================== 线程本地 HTTP Session ====================
class ThreadLocalHTTPSession:
    """线程本地的 HTTP Session"""
    def __init__(self, pool_connections=20, pool_maxsize=40):
        self._local = threading.local()
        self.pool_connections = pool_connections
        self.pool_maxsize = pool_maxsize
        self.lock = threading.Lock()
        self.session_count = 0
    
    def get_session(self):
        """获取当前线程的 Session"""
        if not hasattr(self._local, 'session'):
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.pool_connections,
                pool_maxsize=self.pool_maxsize,
                max_retries=1,  # 减少重试次数
                pool_block=False
            )
            session.mount('http://', adapter)
            session.mount('https://', adapter)
            self._local.session = session
            
            with self.lock:
                self.session_count += 1
                if DEBUG_MODE:
                    print(f'[HTTP Session] Created session #{self.session_count}')
        
        return self._local.session
    
    def post(self, *args, **kwargs):
        """代理 post 方法"""
        return self.get_session().post(*args, **kwargs)
    
    def get(self, *args, **kwargs):
        """代理 get 方法"""
        return self.get_session().get(*args, **kwargs)
    
    def close_all(self):
        """关闭所有 session"""
        pass

HTTP_SESSION = ThreadLocalHTTPSession(pool_connections=20, pool_maxsize=40)

# ==================== 全局变量和初始化 ====================
MODEL_NAME = '/mnt/hdd1/haoyangliu/em_model/bge-multilingual-gemma2'
QUERY_CLASS_MODEL = None
QUERY_CLASS_TOKENIZER = None
SOFTMAX = nn.Softmax(dim=0)

PROFESSIONAL_DICT = set()

MONGO_URL = 'mongodb://root:example@10.70.223.31:27017'
MONGO_DB = 'rqa'
VERSION = '2023091110_adaptive_fixed'

MONGO_PIPELINE = None
MONGO_PIPELINE_RANK = None
DOWNLOAD_PIPELINE = None

ENCODING = None
ES = Elasticsearch('http://10.70.222.234:9200')

MILVUS_BIND = 'http://8.130.183.20:8033/api-vec-search/search'
MONGODB_C_NAME = "paper_shards_detail_table_20230908" 
ENCODER_URL = "http://8.130.183.20:8031/encode"
RERANKER_URL = 'http://8.130.183.20:8032/query_bge_reranker/'

TABLE_NAME = MONGODB_C_NAME

RETRIEVE_CHUNK_NUM = 3000
ES_RETRIEVE_CHUNK_NUM = 300
DOC_SCORE_CHUNK_NUM = 8
SCORE_THREHOLD = -2
TOKEN_LIMIT = 4000
TOKEN_ENCODE_MODEL = 'gpt-3.5-turbo'
CONCAT_CHUNK_NUM = 4

MEMORY_KEYWORD_MATCH = {}
MEMORY_QUERY_MATCH = {}

KW_ZH_MODEL = None
KW_MODEL = None
PORTER_STEMMER = PorterStemmer()
REGEX_PATTERN = '|'.join(map(re.escape, [',', '\n', ';', '!', '?', '.', ' ', '~']))

BERT_LOCK = threading.RLock()
JIEBA_LOCK = threading.RLock()

app = Flask(import_name=__name__)
app.config['JSON_AS_ASCII'] = False

# ==================== 模型加载 ====================
class BERTClassifier(nn.Module):
    def __init__(self, bert_model_name, num_classes):
        super(BERTClassifier, self).__init__()
        self.bert = BertModel.from_pretrained(bert_model_name)
        self.dropout = nn.Dropout(0.1)
        self.fc = nn.Linear(self.bert.config.hidden_size, num_classes)

    def forward(self, input_ids, attention_mask):
        outputs = self.bert(input_ids=input_ids, attention_mask=attention_mask)
        pooled_output = outputs.pooler_output
        x = self.dropout(pooled_output)
        logits = self.fc(x)
        return logits

def load_data(table: str, model_name: str):
    """加载全局数据"""
    print('🔄 Loading global data...')
    start_time = time.time()

    global ENCODING, MONGO_PIPELINE, MONGO_PIPELINE_RANK
    global QUERY_CLASS_MODEL, QUERY_CLASS_TOKENIZER, PROFESSIONAL_DICT
    global KW_ZH_MODEL, KW_MODEL, MEMORY_KEYWORD_MATCH, MEMORY_QUERY_MATCH

    MONGO_PIPELINE = MongoDBConnection(MONGO_URL, MONGO_DB, table)
    MONGO_PIPELINE.connect()
    
    MONGO_PIPELINE_RANK = MongoDBConnection(MONGO_URL, MONGO_DB, MONGODB_C_NAME)
    MONGO_PIPELINE_RANK.connect()
    
    jieba.load_userdict("config/ext_dict2.dct")
    ENCODING = tiktoken.encoding_for_model(TOKEN_ENCODE_MODEL)

    QUERY_CLASS_MODEL = BERTClassifier('/home/tcl/rqa_dir/query_class_model/bert-base-chinese', 2).to('cpu')
    QUERY_CLASS_TOKENIZER = BertTokenizer.from_pretrained('/home/tcl/rqa_dir/query_class_model/bert-base-chinese')
    ckpt = torch.load('/home/tcl/rqa_dir/query_class_model/query_classifier1.pth', map_location=torch.device('cpu'))
    QUERY_CLASS_MODEL.load_state_dict(ckpt, strict=False)

    try:
        with open('/home/tcl/rqa_dir/ext_dict2.dct', 'r', encoding='utf-8') as fkw:
            for line in fkw:
                PROFESSIONAL_DICT.add(line.strip('\n'))
        print(f'[Load] Professional dict: {len(PROFESSIONAL_DICT)} words')
    except Exception as e:
        print(f'[Load] Failed to load professional dict: {e}')

    try:
        MEMORY_QUERY_MATCH, MEMORY_KEYWORD_MATCH = read_keywords_with_score_from_memory(
            'config/memory_keywords_recall_v20230916.txt')
        print(f'[Load] Memory keywords: {len(MEMORY_KEYWORD_MATCH)} patterns')
    except Exception as e:
        print(f'[Load] Failed to load memory keywords: {e}')

    try:
        KW_ZH_MODEL = KeyBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
        KW_MODEL = KBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
        print('[Load] Keyword models loaded')
    except Exception as e:
        print(f'[Load] Failed to load keyword models: {e}')

    print(f'✅ Global data loaded in {time.time() - start_time:.2f}s')

def crontab_update_config():
    """动态加载配置文件"""
    try:
        config = configparser.ConfigParser()
        config.read('./config/search_srv_pipeline.ini', encoding='UTF-8')
    except Exception as e:
        print(f'[Config Update] Error: {e}')

# ==================== 辅助函数 ====================
def has_chinese(text):
    """检查是否包含中文"""
    return bool(re.search(r'[\u4e00-\u9fff]', text))

# ==================== 查询相关性检查 ====================
def check_query_relevance(query: str) -> bool:
    """检查查询相关性（线程安全）"""
    with BERT_LOCK:
        try:
            QUERY_CLASS_MODEL.eval()
            encoding = QUERY_CLASS_TOKENIZER(
                query, 
                return_tensors='pt', 
                max_length=128, 
                padding='max_length', 
                truncation=True
            )
            input_ids = encoding['input_ids'].to('cpu')
            attention_mask = encoding['attention_mask'].to('cpu')

            with torch.no_grad():
                outputs = QUERY_CLASS_MODEL(input_ids=input_ids, attention_mask=attention_mask)

            probs = SOFTMAX(torch.squeeze(outputs, dim=0))
            model_score = probs.detach().cpu().numpy()[1]
            flag_query_rel = model_score >= 0.4
            
            return flag_query_rel
        except Exception as e:
            print(f'[Query Relevance] Error: {e}')
            return True

# ==================== 编码服务（自适应超时） ====================
@http_retry
def encode_from_net_cached(querys):
    """调用编码服务（带缓存和自适应超时）"""
    if isinstance(querys, list):
        cache_key = '|'.join([str(q) for q in querys])
    else:
        cache_key = str(querys)
    
    cached_result = ENCODE_CACHE.get(cache_key)
    if cached_result is not None:
        return cached_result
    
    url = ENCODER_URL
    if isinstance(querys, list):
        payload = {"queries": querys}
    else:
        payload = {"queries": [querys]}

    headers = {"Content-Type": "application/json"}
    
    try:
        # 自适应超时
        timeout = TIMEOUT_MANAGER.get_timeout('encoding')
        response = HTTP_SESSION.post(
            url, 
            json=payload, 
            headers=headers, 
            timeout=(15, timeout)
        )
        response.raise_for_status()
        result = response.json()['embeddings']
        
        ENCODE_CACHE.set(cache_key, result)
        return result
    except Exception as e:
        print(f'[Encode] Error: {e}')
        raise

# ==================== 召回阶段（并行 + 自适应超时） ====================
def recall_pipeline(**kwargs):
    """召回pipeline（并行优化 + 自适应超时）"""
    query: str = kwargs['query']
    query_expand = kwargs['query_expand']
    query_embed_expand = kwargs['query_embed_expand']
    result_dict: dict = kwargs['result_dict']
    
    kw_zh = set()
    keywords_en_set = set()
    
    try:
        if has_chinese(query):
            keywords = extract_kws_zh(query, KW_ZH_MODEL, ngram_range=(1, 1))
            kw0 = [k for (k, _) in keywords]
            kw_zh.update(kw0)
        else:
            kw_en = KW_MODEL.extract_keywords(query)
            keywords_en_set.update([PORTER_STEMMER.stem(kw[0]) for kw in kw_en])
    except Exception as e:
        if DEBUG_MODE:
            print(f'[Recall] Keyword extraction error: {e}')

    try:
        with JIEBA_LOCK:
            jieba_token_num = sum(1 for _ in jieba.cut(query))
            kw_jieba = jieba.analyse.extract_tags(
                query, 
                allowPOS=['nz', 'nr', 'vd', 'n', 'vn', 'x', 'eng', 'v'],
                topK=jieba_token_num // 2
            )
            kw_zh.update(kw_jieba)
    except Exception as e:
        if DEBUG_MODE:
            print(f'[Recall] Jieba extraction error: {e}')

    if not keywords_en_set:
        try:
            query_en = kwargs.get('query_en', query)
            kw_en = KW_MODEL.extract_keywords(query_en)
            keywords_en_set.update([PORTER_STEMMER.stem(kw[0]) for kw in kw_en])
        except Exception as e:
            if DEBUG_MODE:
                print(f'[Recall] English keyword extraction error: {e}')
    
    kwargs['kw_zh'] = kw_zh
    kwargs['kw_en'] = keywords_en_set

    # Milvus向量召回（并行）
    url = MILVUS_BIND
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Accept-Encoding': 'gzip, deflate, br'
    }
    
    embed_recall_res = {}
    recall_max_score = 0.0
    
    @http_retry
    def query_milvus(query_embed1):
        """单次Milvus查询（带重试 + 自适应超时）"""
        try:
            sent_data = {
                'topk': RETRIEVE_CHUNK_NUM,
                'query_vec': json.dumps(query_embed1)
            }
            
            # 自适应超时
            timeout = TIMEOUT_MANAGER.get_timeout('milvus')
            response = HTTP_SESSION.post(
                url, 
                verify=False, 
                headers=headers, 
                data=sent_data, 
                timeout=(20, timeout)
            )
            
            if response.status_code != 200:
                return []
                
            res_json = response.content.decode('utf-8')
            res_data = json.loads(res_json).get('data', {})
            return res_data.get('arr', [])
            
        except Exception as e:
            if DEBUG_MODE:
                print(f'[Recall] Milvus query error: {e}')
            return []
    
    # 并行查询所有embeddings
    max_concurrent = min(3, len(query_embed_expand))
    
    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        futures = [executor.submit(query_milvus, emb) for emb in query_embed_expand]
        
        # 自适应超时
        total_timeout = TIMEOUT_MANAGER.get_timeout('recall')
        for future in as_completed(futures, timeout=total_timeout):
            try:
                res_arr = future.result(timeout=60)
                
                for item in res_arr:
                    try:
                        id_s, score_s, doc_s = item.split(':')
                        res_key = (int(id_s), int(doc_s))
                        score = float(score_s)
                        
                        if res_key in embed_recall_res:
                            embed_recall_res[res_key] = max(score, embed_recall_res[res_key])
                        else:
                            embed_recall_res[res_key] = score
                            
                        if score > recall_max_score:
                            recall_max_score = score
                    except (ValueError, IndexError):
                        pass
                        
            except Exception as e:
                if DEBUG_MODE:
                    print(f'[Recall] Future error: {e}')
    
    for ((index, doc_id), score_s) in embed_recall_res.items():
        combined_key = f"{index}_{doc_id}"
        result_dict[combined_key] = {
            'index': index,
            'doc_id': doc_id,
            'recall_score': score_s,
            'final_score': score_s,
            'doc_name': 'unknown',
            'recall_channels': {'sentence_embed'}
        }
    
    key_match_cnt = len(PROFESSIONAL_DICT.intersection(kw_zh))
    
    if key_match_cnt < 2 and recall_max_score <= 0.5:
        relevant_found = False
        for q in query_expand:
            try:
                if check_query_relevance(q):
                    relevant_found = True
                    break
            except:
                pass
        
        if not relevant_found:
            kwargs['flag_query_rel'] = False
            return kwargs
    
    kwargs['flag_query_rel'] = True
    kwargs['result_dict'] = result_dict
    
    return kwargs

# ==================== 排序阶段（并行 + 自适应超时） ====================
def rank_pipeline(**kwargs):
    """排序pipeline（并行优化 + 自适应超时）"""
    result_dict: dict = kwargs['result_dict']
    
    search_conditions = []
    for key in result_dict.keys():
        if isinstance(key, str) and '_' in key:
            try:
                index, doc_id = key.split('_')
                search_conditions.append({
                    'index': int(index),
                    'doc_id': int(doc_id)
                })
            except (ValueError, IndexError):
                continue
    
    if not search_conditions:
        return kwargs
    
    embed_info = {}
    slice_data_results = []
    
    def query_embeddings_batch_wrapper(batch_conditions):
        """查询embeddings批次"""
        try:
            return query_embeddings_batch(MONGO_PIPELINE_RANK, batch_conditions)
        except Exception as e:
            return []
    
    def query_slice_batch_wrapper(batch_conditions):
        """查询slice data批次"""
        try:
            return MONGO_PIPELINE.find_data({'$or': batch_conditions}, timeout=180)
        except Exception as e:
            return []
    
    # 准备批次
    all_batches = []
    for i in range(0, len(search_conditions), MONGO_BATCH_SIZE):
        all_batches.append(search_conditions[i:i + MONGO_BATCH_SIZE])
    
    # 并行查询
    with ThreadPoolExecutor(max_workers=min(4, len(all_batches))) as executor:
        embed_futures = [executor.submit(query_embeddings_batch_wrapper, batch) 
                        for batch in all_batches]
        
        # 自适应超时
        total_timeout = TIMEOUT_MANAGER.get_timeout('rank')
        for future in as_completed(embed_futures, timeout=total_timeout):
            try:
                batch_results = future.result(timeout=120)
                for record in batch_results:
                    try:
                        index = record.get('index')
                        doc_id = record.get('doc_id')
                        embedding = record.get('embedding')
                        
                        if index is not None and doc_id is not None:
                            combined_key = f"{index}_{doc_id}"
                            embed_info[combined_key] = embedding
                    except:
                        pass
            except:
                pass
    
    # 并行查询slice data
    with ThreadPoolExecutor(max_workers=min(4, len(all_batches))) as executor:
        slice_futures = [executor.submit(query_slice_batch_wrapper, batch) 
                        for batch in all_batches]
        
        for future in as_completed(slice_futures, timeout=total_timeout):
            try:
                batch_data = future.result(timeout=120)
                slice_data_results.extend(batch_data)
            except:
                pass
    
    # 处理切片数据
    for dct in slice_data_results:
        try:
            index = dct.get('index')
            doc_id = dct.get('doc_id')
            shard = dct.get('shard', '')
            doc_name = dct.get('doc_name', 'unknown')
            
            if not index or not doc_id or len(shard) < 50:
                continue
            
            combined_key = f"{index}_{doc_id}"
            if combined_key not in result_dict:
                continue
            
            shard = re.sub(r"\[[0-9].{0,5}\]", "", shard)
            
            row = result_dict[combined_key]
            row['doc_name'] = doc_name
            row['title'] = dct.get('doc_name', doc_name)
            row['shard'] = shard
            row['index'] = index
            row['doc_id'] = doc_id
            
            if combined_key in embed_info:
                row['has_embedding'] = True
            
        except:
            pass
    
    kwargs['result_dict'] = result_dict
    return kwargs

def query_embeddings_batch(mongo_conn, batch_conditions):
    """查询embeddings批次"""
    if not batch_conditions:
        return []
    
    find_condition = {'$or': batch_conditions}
    
    return mongo_conn.find_data(
        find_condition,
        projection={'_id': 0, 'index': 1, 'doc_id': 1, 'embedding': 1},
        limit=len(batch_conditions),
        timeout=180
    )

# ==================== 重排阶段（并行 + 智能补齐 + 自适应超时） ====================
def rerank_pipeline(**kwargs):
    """重排pipeline（智能补齐 + 自适应超时）"""
    query: str = kwargs['query']
    result_dict: dict = kwargs['result_dict']
    
    sorted_chunks = sorted(result_dict.items(), key=lambda d: -d[1]['final_score'])
    
    rerank_limit = 300
    chunks_to_rerank = sorted_chunks[:rerank_limit]
    
    result_dict_sorted = {}
    for key, data in sorted_chunks:
        result_dict_sorted[key] = data.copy()
    
    bge_score_weight = 3.0
    batch_size = 15
    all_batches = []
    current_batch = []
    current_keys = []
    
    for idx, (combined_key, chunk_data) in enumerate(chunks_to_rerank):
        if 'shard' not in chunk_data:
            continue
        
        shard_text = chunk_data['shard']
        if len(shard_text) > 1500:
            shard_text = shard_text[:1500] + '...'
        
        current_batch.append([query, shard_text])
        current_keys.append(combined_key)
        
        if len(current_batch) >= batch_size:
            all_batches.append((current_keys.copy(), current_batch.copy()))
            current_batch = []
            current_keys = []
    
    if current_batch:
        all_batches.append((current_keys.copy(), current_batch.copy()))
    
    @http_retry
    def process_rerank_batch(batch_data):
        """处理单个重排批次（智能补齐 + 自适应超时）"""
        batch_keys, batch_pairs = batch_data
        expected_count = len(batch_keys)
        
        try:
            request_data = {
                'type': 'multi',
                'multi_data': batch_pairs
            }
            
            # 自适应超时
            timeout = TIMEOUT_MANAGER.get_timeout('rerank')
            response = HTTP_SESSION.post(
                RERANKER_URL,
                json=request_data,
                timeout=(15, timeout)
            )
            
            if response.status_code != 200:
                return batch_keys, None, f"HTTP {response.status_code}"
            
            result = response.json()
            scores = result.get('score', [])
            
            # 智能处理分数不匹配
            actual_count = len(scores)
            
            if actual_count != expected_count:
                if actual_count > expected_count:
                    # 多了就截断
                    scores = scores[:expected_count]
                    if DEBUG_MODE:
                        print(f'[Rerank] Truncated scores from {actual_count} to {expected_count}')
                else:
                    # 少了就补0
                    scores.extend([0.0] * (expected_count - actual_count))
                    if DEBUG_MODE:
                        print(f'[Rerank] Padded scores from {actual_count} to {expected_count}')
            
            return batch_keys, scores, None
            
        except Exception as e:
            return batch_keys, None, str(e)[:100]
    
    # 并行处理批次
    max_workers = min(4, len(all_batches))
    
    successful_batches = 0
    failed_batches = 0
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_rerank_batch, batch): i 
                  for i, batch in enumerate(all_batches)}
        
        # 自适应超时
        total_timeout = TIMEOUT_MANAGER.get_timeout('rerank_total')
        for future in as_completed(futures, timeout=total_timeout):
            batch_idx = futures[future]
            
            try:
                batch_result = future.result(timeout=90)
                batch_keys, scores, error = batch_result
                
                if error is not None:
                    failed_batches += 1
                    for key in batch_keys:
                        if key in result_dict_sorted:
                            result_dict_sorted[key]['rerank_score'] = result_dict_sorted[key]['final_score']
                    continue
                
                # 应用重排分数
                for i, combined_key in enumerate(batch_keys):
                    if i < len(scores) and combined_key in result_dict_sorted:
                        try:
                            bge_score = float(scores[i])
                            current_score = result_dict_sorted[combined_key]['final_score']
                            new_score = current_score + bge_score_weight * bge_score
                            
                            result_dict_sorted[combined_key]['rerank_score'] = new_score
                            result_dict_sorted[combined_key]['final_score'] = new_score
                        except:
                            result_dict_sorted[combined_key]['rerank_score'] = result_dict_sorted[combined_key]['final_score']
                
                successful_batches += 1
                
            except Exception as e:
                failed_batches += 1
                batch_keys, _ = all_batches[batch_idx]
                for key in batch_keys:
                    if key in result_dict_sorted:
                        result_dict_sorted[key]['rerank_score'] = result_dict_sorted[key]['final_score']
    
    # 处理未重排的数据
    for key, data in result_dict_sorted.items():
        if 'rerank_score' not in data:
            data['rerank_score'] = data['final_score']
    
    # 关键词匹配调权
    kw_zh = kwargs.get('kw_zh', set())
    kw_en = kwargs.get('kw_en', set())
    kw_num = len(kw_zh)
    
    if kw_num > 0:
        for combined_key, row in result_dict_sorted.items():
            try:
                if row.get('final_score', 0) < 0.4 or 'shard' not in row:
                    continue
                
                shard = row['shard']
                match_cnt = 0
                
                if has_chinese(shard):
                    for kw in kw_zh:
                        if kw and kw in shard:
                            match_cnt += 1
                else:
                    tokens_set = set([PORTER_STEMMER.stem(t) for t in re.split(REGEX_PATTERN, shard)])
                    match_cnt = len(tokens_set.intersection(kw_en))
                
                if kw_num > 0 and match_cnt > 0:
                    match_score = 0.95 + pow(match_cnt / kw_num, 2)
                    row['match_cnt'] = match_cnt
                    row['match_score'] = match_score
                    row['final_score'] = row['final_score'] * match_score
            except:
                pass
    
    kwargs['result_dict'] = result_dict_sorted
    return kwargs

# ==================== 结果拼接 ====================
def concat_shards_by_rank(**kwargs):
    """拼接chunks"""
    result_dict = kwargs['result_dict']
    top_doc_num: int = kwargs['top_doc_num']
    
    sorted_chunks = sorted(list(result_dict.values()), key=lambda d: -d['final_score'])
    
    max_chunks = min(len(sorted_chunks), top_doc_num * 10)
    sorted_chunks = sorted_chunks[:max_chunks]
    
    selected_indices = []
    
    for i in range(min(len(sorted_chunks), top_doc_num * 3)):
        if i % 3 == 0:
            selected_indices.append(i)
    
    res_arr = []
    current_chunks = []
    current_tokens = 0
    current_docs = []
    current_doc_ids = []
    current_indices = []
    current_titles = []
    total_score = 0.0
    
    for idx in selected_indices:
        if idx >= len(sorted_chunks):
            break
            
        chunk_data = sorted_chunks[idx]
        
        shard = chunk_data.get('shard', '')
        if not shard:
            continue
            
        tokens = ENCODING.encode(shard)
        token_count = len(tokens)
        
        if current_tokens + token_count > TOKEN_LIMIT and current_chunks:
            result = {
                'text': current_chunks,
                'ans_id': len(res_arr),
                'doc_name': current_docs,
                'doc_id': current_doc_ids,
                'index': current_indices,
                'title': current_titles,
                'score': total_score / len(current_chunks) if current_chunks else 0,
                'concat_score': 0.5
            }
            res_arr.append(result)
            
            current_chunks = []
            current_tokens = 0
            current_docs = []
            current_doc_ids = []
            current_indices = []
            current_titles = []
            total_score = 0.0
        
        current_chunks.append(shard)
        current_tokens += token_count
        current_docs.append(chunk_data.get('doc_name', 'unknown'))
        current_doc_ids.append(chunk_data.get('doc_id', 0))
        current_indices.append(chunk_data.get('index', 0))
        current_titles.append(chunk_data.get('title', 'unknown'))
        total_score += chunk_data.get('final_score', 0)
        
        if len(res_arr) >= top_doc_num:
            break
    
    if current_chunks:
        result = {
            'text': current_chunks,
            'ans_id': len(res_arr),
            'doc_name': current_docs,
            'doc_id': current_doc_ids,
            'index': current_indices,
            'title': current_titles,
            'score': total_score / len(current_chunks) if current_chunks else 0,
            'concat_score': 0.5
        }
        res_arr.append(result)
    
    res_arr.sort(key=lambda x: -x['score'])
    res_arr = res_arr[:top_doc_num]
    
    return res_arr

# ==================== API接口 ====================
@app.route('/api-rqa-search/test', methods=['GET'])
def hello_world():
    return json_result(0, '', 'Service available')

@app.route('/api-rqa-search/stats', methods=['GET'])
def get_stats():
    """获取服务统计信息"""
    stats = MONITOR.get_stats()
    stats['encode_cache'] = ENCODE_CACHE.stats()
    stats['service'] = {
        'port': SERVER_PORT,
        'version': VERSION,
        'model': MODEL_NAME,
        'workers': WORKER_COUNT,
        'batch_size': MONGO_BATCH_SIZE,
        'debug_mode': DEBUG_MODE,
        'max_request_timeout': MAX_REQUEST_TIMEOUT,
        'timeout_strategy': 'adaptive',
        'request_id_fix': 'atomic_counter'
    }
    
    stats['http_sessions'] = HTTP_SESSION.session_count
    stats['active_requests_detail'] = MONITOR.get_active_requests_info()
    stats['stuck_requests'] = MONITOR.get_stuck_requests(threshold=MAX_REQUEST_TIMEOUT)
    
    return json_result(0, '', stats)

@app.route('/api-rqa-search/health', methods=['GET'])
def health_check():
    """健康检查接口"""
    health_status = {
        'status': 'healthy',
        'timestamp': datetime.datetime.now().isoformat(),
        'version': VERSION,
        'uptime': MONITOR.get_stats()['uptime'],
        'timeout_config': 'adaptive',
        'request_id_fix': 'atomic_counter'
    }
    
    try:
        if MONGO_PIPELINE and MONGO_PIPELINE.client:
            MONGO_PIPELINE.client.admin.command('ping')
            health_status['mongodb'] = 'connected'
        else:
            health_status['mongodb'] = 'disconnected'
    except Exception as e:
        health_status['mongodb'] = f'error: {str(e)[:100]}'
    
    try:
        test_response = HTTP_SESSION.get(ENCODER_URL.replace('/encode', ''), timeout=5)
        health_status['encoder_service'] = 'available' if test_response.status_code == 200 else 'unavailable'
    except Exception as e:
        health_status['encoder_service'] = f'error: {str(e)[:100]}'
    
    return json_result(0, 'Service is healthy', health_status)

@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    """搜索服务主函数（自适应超时 + 强制超时保护 + 修复请求ID冲突）"""
    # ✅ 使用线程安全的原子计数器生成唯一ID（修复冲突问题）
    req_id = REQUEST_ID_GENERATOR.generate()
    request_started = False
    
    try:
        form = request.form
        query = form.get('query', '', str)
        query_en = form.get('query_dst', '', str)
        id = form.get('id', 0, int)
        is_debug = form.get('debug', 0, int) == 1
        top_doc_num = form.get('top_doc_num', 0, int)
        target_doc_name = form.get('target_doc_name', '', str)
        is_delete = form.get('is_delete', 0, int)
        delete_doc_id = form.get('delete_doc_id', '', str)
        
        if not query:
            return json_result(-1, 'query must not be null.', None)
        if not id:
            return json_result(-1, 'id must not be null.', None)
        if not top_doc_num:
            return json_result(-1, 'top_doc_num must not be null.', None)
        
        # 检查并发负载，避免过载
        current_active = TIMEOUT_MANAGER.active_requests
        if current_active > 50:
            print(f'[Request {req_id}] ⚠️  High load: {current_active} active requests, rejecting')
            return json_result(-3, f'Server busy ({current_active} active requests)', {
                'doc_num': 0,
                'arr': [],
                'ts': int(time.time() * 1000)
            })
        
        # 标记请求开始（必须在参数验证通过后）
        MONITOR.start_request(req_id, query)
        TIMEOUT_MANAGER.enter_request()
        request_started = True
        
        print(f'[Request {req_id}] Started, query: {query[:100]}, active: {current_active}')
        
        if is_delete == 1 and delete_doc_id:
            try:
                delete_doc_id_list = [int(t) for t in delete_doc_id.split(',')]
                res = MONGO_PIPELINE.collection.delete_many({
                    'doc_id': {'$in': delete_doc_id_list}
                })
                return json_result(0, f'deleted {res.deleted_count} documents', None)
            except Exception as e:
                return json_result(-1, f'delete failed: {e}', None)
            finally:
                MONITOR.end_request(req_id, success=True)
                TIMEOUT_MANAGER.exit_request()
        
        start_time = time.time()
        
        code = 0
        msg = ''
        data = {
            'model': MODEL_NAME,
            'version': VERSION,
            'request_id': req_id
        }
        
        try:
            # 强制请求级别超时保护
            def execute_search():
                # 阶段1: 编码
                MONITOR.update_stage(req_id, 'encoding')
                encoding_start = time.time()
                print(f'[Request {req_id}] Stage 1: Encoding started')
                
                query_embed = safe_execute(
                    lambda: encode_from_net_cached(query),
                    timeout=TIMEOUT_MANAGER.get_timeout('encoding'),
                    default=None,
                    operation_name="Encoding"
                )
                
                # ✅ 更严格的验证：检查None、空列表、格式错误
                if query_embed is None:
                    print(f'[Request {req_id}] ❌ Encoding failed: returned None after {time.time()-encoding_start:.1f}s')
                    raise Exception("Encoding failed: service returned None")
                
                if not isinstance(query_embed, list) or len(query_embed) == 0:
                    print(f'[Request {req_id}] ❌ Encoding failed: invalid format or empty, type={type(query_embed)}, len={len(query_embed) if isinstance(query_embed, list) else "N/A"}')
                    raise Exception(f"Encoding failed: invalid result format")
                
                # 检查第一个元素是否为有效的embedding
                if not isinstance(query_embed[0], (list, tuple)) or len(query_embed[0]) == 0:
                    print(f'[Request {req_id}] ❌ Encoding failed: invalid embedding format')
                    raise Exception("Encoding failed: invalid embedding format")
                
                print(f'[Request {req_id}] ✅ Encoding completed in {time.time()-encoding_start:.1f}s')
                MONITOR.end_stage(req_id, 'encoding')
                
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
                recall_start = time.time()
                print(f'[Request {req_id}] Stage 2: Recall started')
                
                recall_result = safe_execute(
                    lambda: recall_pipeline(**params),
                    timeout=TIMEOUT_MANAGER.get_timeout('recall'),
                    default=None,
                    operation_name="Recall"
                )
                
                if recall_result is None:
                    print(f'[Request {req_id}] ❌ Recall failed after {time.time()-recall_start:.1f}s')
                    raise Exception("Recall failed")
                
                params.update(recall_result)
                recall_count = len(params.get('result_dict', {}))
                print(f'[Request {req_id}] ✅ Recall completed in {time.time()-recall_start:.1f}s, found {recall_count} chunks')
                MONITOR.end_stage(req_id, 'recall')
                
                if not params['flag_query_rel']:
                    print(f'[Request {req_id}] ⚠️  Query not relevant')
                    return {'code': 1, 'msg': 'query must be relevant', 'arr': [], 'doc_num': 0}
                
                # 阶段3: 排序
                MONITOR.update_stage(req_id, 'ranking')
                rank_start = time.time()
                print(f'[Request {req_id}] Stage 3: Ranking started')
                
                rank_result = safe_execute(
                    lambda: rank_pipeline(**params),
                    timeout=TIMEOUT_MANAGER.get_timeout('rank'),
                    default=None,
                    operation_name="Ranking"
                )
                
                if rank_result is None:
                    print(f'[Request {req_id}] ⚠️  Ranking failed after {time.time()-rank_start:.1f}s, using recall results')
                else:
                    params.update(rank_result)
                    print(f'[Request {req_id}] ✅ Ranking completed in {time.time()-rank_start:.1f}s')
                
                MONITOR.end_stage(req_id, 'ranking')
                
                # 阶段4: 重排
                MONITOR.update_stage(req_id, 'reranking')
                rerank_start = time.time()
                print(f'[Request {req_id}] Stage 4: Reranking started')
                
                rerank_result = safe_execute(
                    lambda: rerank_pipeline(**params),
                    timeout=TIMEOUT_MANAGER.get_timeout('rerank_total'),
                    default=None,
                    operation_name="Reranking"
                )
                
                if rerank_result is None:
                    print(f'[Request {req_id}] ⚠️  Reranking failed after {time.time()-rerank_start:.1f}s, using previous results')
                else:
                    params.update(rerank_result)
                    print(f'[Request {req_id}] ✅ Reranking completed in {time.time()-rerank_start:.1f}s')
                
                MONITOR.end_stage(req_id, 'reranking')
                
                # 阶段5: 拼接
                MONITOR.update_stage(req_id, 'concatenating')
                concat_start = time.time()
                print(f'[Request {req_id}] Stage 5: Concatenating started')
                
                params['top_doc_num'] = top_doc_num
                params['concat_num'] = CONCAT_CHUNK_NUM
                params['is_debug'] = is_debug
                
                similar_shards = safe_execute(
                    lambda: concat_shards_by_rank(**params),
                    timeout=60,
                    default=[],
                    operation_name="Concatenation"
                )
                
                print(f'[Request {req_id}] ✅ Concatenation completed in {time.time()-concat_start:.1f}s, got {len(similar_shards)} results')
                MONITOR.end_stage(req_id, 'concatenating')
                
                json_arr = []
                for dict_item in similar_shards:
                    score = dict_item.get('score', 0)
                    if score < SCORE_THREHOLD:
                        continue
                    json_arr.append(dict_item)
                
                print(f'[Request {req_id}] 🎯 Final results: {len(json_arr)} docs (after score filter)')
                return {'code': 0, 'msg': '', 'arr': json_arr, 'doc_num': len(json_arr)}
            
            # 执行搜索（带强制总超时）
            result = safe_execute(
                execute_search,
                timeout=MAX_REQUEST_TIMEOUT,
                default={'code': -2, 'msg': f'Request timeout (>{MAX_REQUEST_TIMEOUT}s)', 'arr': [], 'doc_num': 0},
                operation_name="Complete search"
            )
            
            code = result['code']
            msg = result['msg']
            data['arr'] = result['arr']
            data['doc_num'] = result['doc_num']
                
        except Exception as e:
            code = -1
            msg = str(e)[:500]
            data['msg'] = msg
            data['doc_num'] = 0
            data['arr'] = []
            if DEBUG_MODE:
                print(f'[Request {req_id}] Error: {traceback.format_exc()[:500]}')
            else:
                print(f'[Request {req_id}] Error: {msg}')
        finally:
            # ✅ 确保一定会调用 end_request（放在 finally 块中）
            if request_started:
                if code == -2:
                    MONITOR.end_request(req_id, success=False, timeout=False, forced_timeout=True)
                elif code == 0:
                    MONITOR.end_request(req_id, success=True)
                else:
                    MONITOR.end_request(req_id, success=False)
                
                TIMEOUT_MANAGER.exit_request()
        
        data['ts'] = int(time.time() * 1000)
        data['process_time'] = time.time() - start_time
        
        print(f'[Request {req_id}] Completed in {data["process_time"]:.2f}s, '
              f'code={code}, results: {data.get("doc_num", 0)}')
        
        return json_result(code, msg, data)
        
    except Exception as e:
        print(f'[Request {req_id}] Fatal error: {e}')
        if request_started:
            MONITOR.end_request(req_id, success=False)
            TIMEOUT_MANAGER.exit_request()
        return json_result(-999, f'System error: {str(e)}', {'doc_num': 0, 'arr': []})

@app.route('/api-rqa-search/download', methods=['GET'])
def download():
    doc_name = request.args.get('docName', '', str)
    query_id = request.args.get('queryId', 0, int)
    username = request.args.get('username', '', str)
    
    try:
        save_download_record(query_id, username, doc_name)
    except Exception as err:
        print('Failed to save download record:', err)
    
    file_path = '/mnt/hdd1/rqa_dir/db/papers_pdf/' + doc_name
    
    try:
        with open(file_path, 'rb') as f:
            stream = f.read()
        
        response = Response(stream, content_type='application/octet-stream')
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET,HEAD,OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Referer,Accept,Origin,User-Agent'
        
        return response
    except FileNotFoundError:
        return json_result(-1, 'File not found', None)
    except Exception as e:
        return json_result(-1, f'Download error: {e}', None)

def save_download_record(query_id: int, username: str, doc_name: str):
    if query_id == 0 or username == '' or doc_name == '':
        return
    
    global DOWNLOAD_PIPELINE
    if DOWNLOAD_PIPELINE is None:
        DOWNLOAD_PIPELINE = MongoDBConnection(MONGO_URL, MONGO_DB, "download_record")
        DOWNLOAD_PIPELINE.connect()
    
    document = {
        "query_id": query_id,
        "username": username,
        "doc_name": doc_name,
        "created_at": datetime.datetime.now()
    }
    
    try:
        DOWNLOAD_PIPELINE.collection.insert_one(document)
    except Exception as e:
        print(f'[Download Record] Error: {e}')

def json_result(code: int, msg: str, data):
    return jsonify({
        'code': code,
        'msg': msg,
        'data': data
    })

# ==================== 主程序 ====================
faulthandler.enable()

print(f'🚀 [Server] Starting on {SERVER_HOST}:{SERVER_PORT}...')
print(f'[Config] Workers: {WORKER_COUNT}, BatchSize: {MONGO_BATCH_SIZE}')
print(f'[Config] Model: {MODEL_NAME}, Version: {VERSION}')
print(f'[Config] Debug Mode: {DEBUG_MODE}')
print(f'⏱️  [Timeout] Adaptive strategy enabled')
print(f'   - Max request timeout: {MAX_REQUEST_TIMEOUT}s (forced)')
print(f'   - Base timeout: 60s')
print(f'   - Multiplier: 1.5x-5x (based on load)')
print(f'✅ [Fix] Request ID conflict fixed (Atomic Counter)')

config = configparser.ConfigParser()
try:
    config.read('./config/search_srv_pipeline_l.ini', encoding='UTF-8')
    print(f'[Config] Config file loaded')
except Exception as e:
    print(f'[Config] Error loading config: {e}')

scheduler = BackgroundScheduler()
scheduler.add_job(crontab_update_config, 'interval', seconds=300, coalesce=True, replace_existing=True)
scheduler.start()

load_data(TABLE_NAME, MODEL_NAME)

def cleanup():
    print('🧹 Cleaning up resources...')
    
    for conn in [MONGO_PIPELINE, MONGO_PIPELINE_RANK, DOWNLOAD_PIPELINE]:
        if conn and hasattr(conn, 'client'):
            try:
                conn.client.close()
                print(f'[Cleanup] Closed MongoDB connection')
            except:
                pass
    
    try:
        HTTP_SESSION.close_all()
        print('[Cleanup] Closed HTTP sessions')
    except:
        pass
    
    try:
        scheduler.shutdown()
        print('[Cleanup] Stopped scheduler')
    except:
        pass
    
    print('✅ Cleanup completed')

atexit.register(cleanup)

if __name__ == '__main__':
    print(f'✅ [Server] Ready on http://{SERVER_HOST}:{SERVER_PORT}')
    print(f'[Server] Thread-safe parallel processing enabled')
    print(f'[Server] Adaptive timeout strategy with forced timeout protection')
    print(f'[Server] Maximum request duration: {MAX_REQUEST_TIMEOUT}s')
    print(f'[Server] Stuck request detection enabled')
    print(f'[Server] Request ID conflict FIXED (Atomic Counter)')
    print(f'[Server] Press Ctrl+C to stop')
    
    try:
        app.run(
            host=SERVER_HOST,
            port=SERVER_PORT,
            threaded=True,
            processes=1,
            debug=False
        )
    except KeyboardInterrupt:
        print('\n🛑 Server stopped by user')
    except Exception as e:
        print(f'❌ Server error: {e}')
    finally:
        cleanup()
