import configparser
import datetime
import faulthandler
import jieba
import jieba.analyse
import json
import os
import pymongo
import random
import re
import requests
import sys
import threading
import time
import tiktoken
import traceback
import torch
import numpy as np
import pandas as pd

from apscheduler.schedulers.background import BackgroundScheduler
from concurrent.futures import ThreadPoolExecutor, as_completed
from elasticsearch import Elasticsearch
from functools import wraps, lru_cache
from nltk.stem.porter import PorterStemmer
from sentence_transformers import util
from zhkeybert import KeyBERT, extract_kws_zh
from retriever.retriever_memory_keywords import read_keywords_with_score_from_memory
from keybert import KeyBERT as KBERT
from torch import nn
from transformers import BertTokenizer, BertModel

# ==================== 并发控制与连接池管理 ====================
# ⭐ 关键优化：限制总并发数，避免连接池耗尽
MAX_CONCURRENT_REQUESTS = 50  # 最大并发请求数（32并发测试 + 余量）
REQUEST_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_REQUESTS)

# MongoDB 连接池统一配置
MONGO_POOL_CONFIG = {
    'maxPoolSize': 300,  # 增大连接池：50并发 × 2并行 × 3倍余量
    'minPoolSize': 80,   # 预热80个连接
    'maxIdleTimeMS': 15000,  # ⭐ 15秒空闲超时（远小于服务器超时）
    'connectTimeoutMS': 10000,
    'socketTimeoutMS': 60000,  # ⭐ 60秒socket超时
    'serverSelectionTimeoutMS': 10000,
    'waitQueueTimeoutMS': 20000,  # ⭐ 20秒等待超时（快速失败）
    'retryReads': True,
    'retryWrites': True,
    'connect': True,
    'heartbeatFrequencyMS': 3000,  # ⭐ 3秒心跳
}

# 连接池监控
POOL_STATS = {
    'total_requests': 0,
    'active_requests': 0,
    'failed_requests': 0,
    'connection_errors': 0,
    'last_reset_time': time.time()
}
POOL_STATS_LOCK = threading.Lock()

def update_pool_stats(key, delta=1):
    """更新连接池统计"""
    with POOL_STATS_LOCK:
        POOL_STATS[key] += delta

def get_pool_stats():
    """获取连接池统计"""
    with POOL_STATS_LOCK:
        stats = POOL_STATS.copy()
        uptime = time.time() - stats['last_reset_time']
        stats['uptime_seconds'] = int(uptime)
        stats['requests_per_second'] = stats['total_requests'] / uptime if uptime > 0 else 0
        return stats

# 重试装饰器优化
def mongodb_retry(max_retries=2, initial_delay=0.5):
    """MongoDB 查询重试装饰器 - 快速重试"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (pymongo.errors.AutoReconnect, 
                        pymongo.errors.NetworkTimeout,
                        pymongo.errors.ServerSelectionTimeoutError,
                        ConnectionResetError,
                        OSError) as e:
                    update_pool_stats('connection_errors')
                    if attempt == max_retries - 1:
                        raise
                    # 快速重试：0.5s, 1s
                    delay = initial_delay * (2 ** attempt)
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

def zhipu_translate(id: int, text: str, from_lang: str, to_lang: str):
    """调用质谱api中译英"""
    query_target = ''
    url = 'https://rqa-test.t-knows.com/api-rqa-web/v1/tcl_translate'
    headers = {
        'Content-Length': '<calculated when request is sent>',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {
        'id': id,
        'query': text,
        'src_lang': from_lang,
        'dst_lang': to_lang
    }
    try:
        res_json = HTTP_SESSION.post(url, verify=False, headers=headers, data=data,
                                 timeout=(10, 30)).content.decode('utf-8')
        json_data = json.loads(res_json)
        query_target = json_data['target']
    except Exception as e:
        pass
    return query_target

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

class mongodb:
    def __init__(self, url, db_name, table_name):
        self.url = url 
        self.db_name = db_name 
        self.table_name = table_name
        self.client = None
        self.db = None
        self.collection = None
        self.last_ping_time = 0
        self.ping_lock = threading.Lock()
        self.query_count = 0

    def connect(self):
        """连接 MongoDB - 统一配置，高并发优化"""
        self.client = pymongo.MongoClient(self.url, **MONGO_POOL_CONFIG)
        self.db = self.client[self.db_name]
        self.collection = self.db[self.table_name]
        
        # 预热连接池
        try:
            self.client.admin.command('ping')
            self.last_ping_time = time.time()
            print(f'[MongoDB] ✅ Connected: {self.db_name}')
            print(f'[MongoDB] Pool: max={self.client.max_pool_size}, min={self.client.min_pool_size}')
        except Exception as e:
            print(f'[MongoDB] ❌ Connection failed: {e}')
            raise
    
    def ping(self):
        """轻量级心跳检查"""
        current_time = time.time()
        if current_time - self.last_ping_time < 10:
            return True
        
        with self.ping_lock:
            if current_time - self.last_ping_time < 10:
                return True
            
            try:
                self.client.admin.command('ping')
                self.last_ping_time = current_time
                return True
            except Exception as e:
                update_pool_stats('connection_errors')
                return False
    
    def get_pool_info(self):
        """获取连接池信息"""
        try:
            return {
                'max_pool_size': self.client.max_pool_size,
                'min_pool_size': self.client.min_pool_size,
                'query_count': self.query_count
            }
        except:
            return {}
    
    def insert_one(self, data):
        result = self.collection.insert_one(data)
        return

    @mongodb_retry(max_retries=2, initial_delay=0.5)
    def find_data(self, conditions, **kwargs):
        """查询数据 - 带重试和健康检查"""
        self.ping()
        self.query_count += 1
        return self.collection.find(conditions, **kwargs)

sys.path.append("..")

from flask import Flask, request, Response, jsonify

# embedding 模型
MODEL_NAME = '/mnt/hdd1/haoyangliu/em_model/bge-multilingual-gemma2'
# query class 模型
QUERY_CLASS_MODEL = BERTClassifier('/home/tcl/rqa_dir/query_class_model/bert-base-chinese', 2).to('cpu')
QUERY_CLASS_TOKENIZER = BertTokenizer.from_pretrained('/home/tcl/rqa_dir/query_class_model/bert-base-chinese')
SOFTMAX = nn.Softmax(dim=0)

PROFESSIONAL_DICT = set()

# 连接mongo数据库相关的变量
MONGO_URL = 'mongodb://root:example@10.70.223.31:27017'
MONGO_DB = 'rqa'
VERSION = '2023091110'

# 加载MongoDB的相关组件
MONGO_PIPELINE = None
MONGO_PIPELINE_RANK = None
DOWNLOAD_PIPELINE = None

# 加载向量库支持向量检索相关的变量
ENCODING = tiktoken.encoding_for_model("gpt-3.5-turbo")

# 加载ES相关组件
ES = Elasticsearch('http://10.70.222.234:9200')

#核心组件
MILVUS_BIND = 'http://127.0.0.1:4100/api-vec-search/search'
MONGODB_C_NAME = "paper_shards_detail_table_20230908" 
ENCODER_URL="http://8.130.183.20:8031/encode"
RERANKER_URL = 'http://8.130.183.20:8032/query_bge_reranker/'

TABLE_NAME = MONGODB_C_NAME

# recall
RETRIEVE_CHUNK_NUM = 4000
ES_RETRIEVE_CHUNK_NUM = 400

# rank
DOC_SCORE_CHUNK_NUM = 8
SCORE_THREHOLD = -2
TOKEN_LIMIT = 4000
TOKEN_ENCODE_MODEL = 'gpt-3.5-turbo'
CONCAT_CHUNK_NUM = 4

# memory keyword match
MEMORY_KEYWORD_MATCH = {}
MEMORY_QUERY_MATCH = {}

# KeyBERT模型
KW_ZH_MODEL = None
KW_MODEL = None

PORTER_STEMMER = PorterStemmer()
REGEX_PATTERN = '|'.join(map(re.escape, [',', '\n', ';', '!', '?', '.', ' ', '~']))

# ⭐ 优化：使用连接池
HTTP_SESSION = requests.Session()
HTTP_ADAPTER = requests.adapters.HTTPAdapter(
    pool_connections=100,
    pool_maxsize=200,
    max_retries=3,
    pool_block=False
)
HTTP_SESSION.mount('http://', HTTP_ADAPTER)
HTTP_SESSION.mount('https://', HTTP_ADAPTER)

# 线程安全锁
BERT_LOCK = threading.Lock()
JIEBA_LOCK = threading.Lock()

app = Flask(import_name=__name__)
app.config['JSON_AS_ASCII'] = False

# ==================== 辅助函数 ====================
@lru_cache(maxsize=1000)
def has_chinese(text):
    """检查是否包含中文（带缓存）"""
    pattern = re.compile(r'[\u4e00-\u9fff]')
    return bool(re.search(pattern, text))

def levenshteinDistance(s1, s2):
    if len(s1) > len(s2):
        s1, s2 = s2, s1
    distances = range(len(s1) + 1)
    for i2, c2 in enumerate(s2):
        distances_ = [i2 + 1]
        for i1, c1 in enumerate(s1):
            if c1 == c2:
                distances_.append(distances[i1])
            else:
                distances_.append(1 + min((distances[i1], distances[i1 + 1], distances_[-1])))
        distances = distances_
    return distances[-1]

def mmrStep(lambda_param: float, selectedDocs: list, sorteddDocs: list, simMatrix: dict):
    """MMR 算法"""
    mmr = -9999.999
    res_index = -1
    
    if not selectedDocs:
        selectedDocs.append(0)
        return

    for list_idx, infoDict in enumerate(sorteddDocs):
        if list_idx in selectedDocs:
            continue
        
        mmr_score = lambda_param * infoDict.get('final_score', 0.0)
        current_index = infoDict['index']
        
        diversity_score = max(
            [simMatrix.get(
                (min(current_index, sorteddDocs[selected_list_idx]['index']), 
                 max(current_index, sorteddDocs[selected_list_idx]['index'])), 
                0.0
            ) for selected_list_idx in selectedDocs]
        )
        
        mmr_score -= (1 - lambda_param) * diversity_score * 30
        
        if mmr_score > mmr:
            mmr = mmr_score
            res_index = list_idx

    if res_index != -1:
        selectedDocs.append(res_index)
    return

def load_data(table: str, model_name: str):
    """加载全局数据"""
    print('[Load] Starting...')
    start_time = time.time()

    global ENCODING
    global MONGO_PIPELINE
    global MONGO_PIPELINE_RANK
    global QUERY_CLASS_MODEL
    global QUERY_CLASS_TOKENIZER
    global PROFESSIONAL_DICT

    # ⭐ 统一配置：两个MongoDB客户端使用相同配置
    MONGO_PIPELINE = mongodb(MONGO_URL, MONGO_DB, table)
    MONGO_PIPELINE.connect()
    
    # ⭐ 统一配置：MONGO_PIPELINE_RANK 使用相同的连接池配置
    client = pymongo.MongoClient(MONGO_URL, **MONGO_POOL_CONFIG)
    db = client[MONGO_DB]
    MONGO_PIPELINE_RANK = db[MONGODB_C_NAME]
    
    # 预热连接
    try:
        client.admin.command('ping')
        print(f'[MongoDB] ✅ MONGO_PIPELINE_RANK connected')
    except Exception as e:
        print(f'[MongoDB] ❌ MONGO_PIPELINE_RANK connection failed: {e}')

    # 加载token计算模块
    jieba.load_userdict("config/ext_dict2.dct")
    ENCODING = tiktoken.encoding_for_model(TOKEN_ENCODE_MODEL)

    # 加载query class 模型
    ckpt = torch.load('/home/tcl/rqa_dir/query_class_model/query_classifier1.pth',map_location=torch.device('cpu'))
    QUERY_CLASS_MODEL.load_state_dict(ckpt,strict=False)

    # 加载专业词典
    fkw = open('/home/tcl/rqa_dir/ext_dict2.dct')
    for line in fkw:
        PROFESSIONAL_DICT.add(line.strip('\n'))
    fkw.close()

    # 加载memory keyword match
    global MEMORY_QUERY_MATCH
    global MEMORY_KEYWORD_MATCH
    MEMORY_QUERY_MATCH, MEMORY_KEYWORD_MATCH = read_keywords_with_score_from_memory(
        'config/memory_keywords_recall_v20230916.txt')

    # 加载KeyBERT模型
    global KW_ZH_MODEL
    global KW_MODEL
    global PORTER_STEMMER
    KW_ZH_MODEL = KeyBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
    KW_MODEL = KBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
    PORTER_STEMMER = PorterStemmer()

    print(f'[Load] ✅ Completed in {time.time() - start_time:.2f}s')
    return

def crontab_update_config():
    """动态加载配置文件"""
    config = configparser.ConfigParser()
    config.read('./config/search_srv_pipeline.ini', encoding='UTF-8')

    global RETRIEVE_CHUNK_NUM
    global SCORE_THREHOLD
    global DOC_SCORE_CHUNK_NUM
    global TOKEN_LIMIT
    global CONCAT_CHUNK_NUM

    DOC_SCORE_CHUNK_NUM = int(config['resort']['doc_score_chunk_num'])
    TOKEN_ENCODE_MODEL = config['concat']['token_encode_model']
    return


def crontab_connection_health_check():
    """定期连接池健康检查和统计"""
    try:
        if MONGO_PIPELINE and MONGO_PIPELINE.client:
            MONGO_PIPELINE.ping()
        
        stats = get_pool_stats()
        if stats['total_requests'] > 0:
            print(f'[Pool] Active:{stats["active_requests"]} '
                  f'Total:{stats["total_requests"]} '
                  f'Failed:{stats["failed_requests"]} '
                  f'Errors:{stats["connection_errors"]} '
                  f'RPS:{stats["requests_per_second"]:.1f}')
    except:
        pass


def check_query_relevance(query: str) -> bool:
    """检查查询相关性"""
    with BERT_LOCK:
        QUERY_CLASS_MODEL.eval()
        encoding = QUERY_CLASS_TOKENIZER(query, return_tensors='pt', max_length=128, padding='max_length', truncation=True)
        input_ids = encoding['input_ids'].to('cpu')
        attention_mask = encoding['attention_mask'].to('cpu')

        with torch.no_grad():
            outputs = QUERY_CLASS_MODEL(input_ids=input_ids, attention_mask=attention_mask)

        probs = SOFTMAX(torch.squeeze(outputs, dim=0))
        model_score = probs.detach().cpu().numpy()[1]
        return model_score >= 0.4

def encode_from_net(querys):
    """调用编码服务"""
    url = ENCODER_URL
    if isinstance(querys, list):
        payload = {"queries": querys}
    else:
        payload = {"queries": [querys]}

    headers = {"Content-Type": "application/json"}
    
    try:
        response = HTTP_SESSION.post(url, json=payload, headers=headers, timeout=(10, 60))
        return response.json()['embeddings']
    except Exception as e:
        print(f'[Encode] ❌ Failed: {e}')
        raise

def recall_pipeline(**kwargs):
    """召回 pipeline"""
    query: str = kwargs['query']
    query_embed: np.ndarray = kwargs['query_embed']
    result_dict: dict = kwargs['result_dict']
    query_embed_expand = [query_embed]
    
    # 关键词提取
    if has_chinese(query):
        keywords = extract_kws_zh(query, KW_ZH_MODEL, ngram_range=(1, 1))
    else:
        keywords = KW_MODEL.extract_keywords(query)
    kw0 = [k for (k, _) in keywords]

    with JIEBA_LOCK:
        jieba_token_num = sum(1 for _ in jieba.cut(query))
        kw_jieba = jieba.analyse.extract_tags(query, allowPOS=['nz', 'nr', 'vd', 'n', 'vn', 'x', 'eng', 'v'],
                                              topK=max(1, jieba_token_num // 2))
    
    kw_zh = kw0 + kw_jieba
    kwargs['kw_zh'] = set(kw_zh)
    
    kw_en = KW_MODEL.extract_keywords(kwargs.get('query_en', query))
    keywords_en_set = set([PORTER_STEMMER.stem(kw[0]) for kw in kw_en])
    kwargs['kw_en'] = keywords_en_set

    # Milvus 向量召回
    url = MILVUS_BIND
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    
    embed_recall_res = {}
    recall_max_score = 0.0
    
    for query_embed1 in query_embed_expand:
        sent_data = {
            'topk': RETRIEVE_CHUNK_NUM,
            'query_vec': json.dumps(query_embed1)
        }
        
        try:
            res_json = HTTP_SESSION.post(url, headers=headers, data=sent_data, timeout=(10, 60)).content.decode('utf-8')
            res_data = json.loads(res_json).get('data')
            res_arr = [t.split(':') for t in res_data.get('arr')]
            
            for [id_s, score_s, doc_s] in res_arr:
                res_key = (int(id_s), int(doc_s))
                score = float(score_s)
                
                if res_key in embed_recall_res:
                    embed_recall_res[res_key] = max(score, embed_recall_res[res_key])
                else:
                    embed_recall_res[res_key] = score
                
                recall_max_score = max(recall_max_score, score)
        except Exception as e:
            print(f'[Recall] ❌ Milvus failed: {e}')
    
    # 构建结果
    for ((index, doc_id), score_s) in embed_recall_res.items():
        combined_key = f"{index}_{doc_id}"
        result_dict[combined_key] = {
            'index': index,
            'doc_id': doc_id,
            'recall_score': score_s,
            'final_score': -1.0,
            'doc_name': 'null',
            'recall_channels': set(['sentence_embed'])
        }

    # 检查查询相关性
    key_match_cnt = len(PROFESSIONAL_DICT.intersection(set(kw_zh)))
    if key_match_cnt < 2 and recall_max_score <= 0.5:
        if not check_query_relevance(query):
            kwargs['flag_query_rel'] = False
            return kwargs

    kwargs['result_dict'] = result_dict
    return kwargs


def rank_pipeline(**kwargs):
    """排序 pipeline - ⭐ 并行查询优化"""
    query: str = kwargs['query']
    result_dict: dict = kwargs['result_dict']
    
    search_conditions = []
    for key in result_dict.keys():
        if isinstance(key, str) and '_' in key:
            index, doc_id = key.split('_')
            search_conditions.append({
                'index': int(index),
                'doc_id': int(doc_id)
            })
    
    if not search_conditions:
        return kwargs
    
    find_condition = {'$or': search_conditions}
    search_start_time = time.time()
    
    # ⭐⭐⭐ 并行优化：同时查询 embedding 和 data
    embed_info = {}
    data_list = []
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        # 任务1：查询 embedding
        def query_embeddings():
            try:
                score_iter = MONGO_PIPELINE_RANK.find(
                    find_condition,
                    {'_id': 0, 'index': 1, 'doc_id': 1, 'embedding': 1}
                ).max_time_ms(60000)
                
                results = {}
                for record in list(score_iter):
                    index = record.get('index')
                    doc_id = record.get('doc_id')
                    embedding = record.get('embedding')
                    if index is not None and doc_id is not None:
                        combined_key = f"{index}_{doc_id}"
                        results[combined_key] = embedding
                return results
            except Exception as e:
                print(f'[Rank] ⚠️ Embedding query failed: {e}')
                return {}
        
        # 任务2：查询 data
        def query_data():
            try:
                return list(MONGO_PIPELINE.find_data(find_condition))
            except Exception as e:
                print(f'[Rank] ⚠️ Data query failed: {e}')
                return []
        
        # 并行执行
        future_embed = executor.submit(query_embeddings)
        future_data = executor.submit(query_data)
        
        # 等待结果
        embed_info = future_embed.result()
        data_list = future_data.result()
    
    print(f'[Rank] ✅ Parallel query: embed={len(embed_info)}, data={len(data_list)}, time={time.time()-search_start_time:.2f}s')
    
    # 处理数据
    for dct in data_list:
        index = dct['index']
        doc_id = dct['doc_id']
        combined_key = f"{index}_{doc_id}"
        
        if combined_key not in result_dict:
            continue
        
        shard = dct.get('shard', '')
        if len(shard) < 50:
            result_dict.pop(combined_key, None)
            continue
        
        doc_name = dct['doc_name']
        shard = re.sub(r"\[[0-9].{0,5}\]", "", shard)
        
        row = result_dict[combined_key]
        row['rank_score'] = 0
        row['final_score'] = row['recall_score']
        row['doc_name'] = doc_name
        row['title'] = doc_name
        row['doc_id'] = doc_id
        row['shard'] = shard
        row['index'] = index

    # keyword boost
    keyword_docid = {}
    query1 = query.replace('?', '').replace('？', '').replace("\\n", '').strip(' ')
    if query1 in MEMORY_QUERY_MATCH:
        keyword_docid = MEMORY_QUERY_MATCH[query1]

    for (keys, doc_weights) in MEMORY_KEYWORD_MATCH.items():
        ks = keys.split(',')
        if all(k in query for k in ks):
            for (doc, w) in doc_weights.items():
                if doc not in keyword_docid or keyword_docid[doc] < w:
                    keyword_docid[doc] = w

    if keyword_docid:
        find_condition2 = {'doc_id': {'$in': list(keyword_docid.keys())}}
        try:
            data_iter2 = list(MONGO_PIPELINE.find_data(find_condition2))
            
            docid_index_dict = {}
            weighted_doc_info = {}
            
            for dct in data_iter2:
                doc_id = int(dct['doc_id'])
                index = dct['index']
                shard = dct.get('shard', '')
                
                if index in result_dict:
                    result_dict[index]['final_score'] *= keyword_docid[doc_id]
                
                if len(shard) < 50:
                    continue
                
                doc_name = dct['doc_name']
                if doc_id not in weighted_doc_info:
                    weighted_doc_info[doc_id] = (doc_name, keyword_docid[doc_id])
                
                shard = re.sub(r"\[[0-9].{0,5}\]", "", shard)
                
                if doc_id not in docid_index_dict:
                    docid_index_dict[doc_id] = []
                
                docid_index_dict[doc_id].append({
                    'index': index,
                    'doc_id': doc_id,
                    'recall_score': 0.0,
                    'rank_score': 0,
                    'final_score': keyword_docid[doc_id],
                    'doc_name': doc_name,
                    'title': doc_name,
                    'shard': shard,
                    'recall_channels': set(['keyword'])
                })
            
            # 添加到结果
            for doc_id, rows in docid_index_dict.items():
                rows_sorted = sorted(rows, key=lambda x: -x['final_score'])
                for row in rows_sorted[:5]:
                    combined_key = f"{row['index']}_{row['doc_id']}"
                    if combined_key not in result_dict:
                        result_dict[combined_key] = row
        except Exception as e:
            print(f'[Rank] ⚠️ Keyword boost failed: {e}')

    return kwargs


def rerank_pipeline(**kwargs):
    """重排 pipeline - ⭐ 增大批次提速"""
    query: str = kwargs['query']
    result_dict: dict = kwargs['result_dict']
    
    bge_server_url = RERANKER_URL
    bge_score_weight = 3.0
    BATCH_SIZE = 50  # ⭐ 优化：批次从20增加到50
    
    sorted_top_chunk = sorted(result_dict.items(), key=lambda d: -d[1]['final_score'])
    result_dict_sorted = {}
    bge_score_buff_dict = [[], []]
    
    def process_batch(keys, pairs):
        """处理一个批次"""
        if not keys:
            return
        
        bge_multi_data = {'type': 'multi', 'multi_data': pairs}
        try:
            response = HTTP_SESSION.post(bge_server_url, data=json.dumps(bge_multi_data), timeout=(10, 60))
            bge_rerank_score_list = response.json()['score']
            
            for idx, combined_key in enumerate(keys):
                score = bge_rerank_score_list[idx] if isinstance(bge_rerank_score_list[idx], float) else bge_rerank_score_list[idx][0]
                rank_score = result_dict_sorted[combined_key]['final_score'] + bge_score_weight * score
                result_dict_sorted[combined_key]['rerank_score'] = rank_score
                result_dict_sorted[combined_key]['final_score'] = rank_score
        except Exception as e:
            # 失败时使用默认分数
            for combined_key in keys:
                result_dict_sorted[combined_key]['rerank_score'] = result_dict_sorted[combined_key]['final_score']
    
    for index, (combined_key, chunk_data) in enumerate(sorted_top_chunk):
        if 'shard' not in chunk_data:
            continue
        
        result_dict_sorted[combined_key] = chunk_data
        
        if index < 300:
            bge_score_buff_dict[0].append(combined_key)
            bge_score_buff_dict[1].append([query, chunk_data['shard']])
            
            if len(bge_score_buff_dict[0]) == BATCH_SIZE:
                process_batch(bge_score_buff_dict[0], bge_score_buff_dict[1])
                bge_score_buff_dict = [[], []]
        else:
            rank_score = result_dict_sorted[combined_key]['final_score'] + bge_score_weight * (-8.0)
            result_dict_sorted[combined_key]['rerank_score'] = rank_score
            result_dict_sorted[combined_key]['final_score'] = rank_score
    
    # 处理剩余批次
    if bge_score_buff_dict[0]:
        process_batch(bge_score_buff_dict[0], bge_score_buff_dict[1])
    
    result_dict = result_dict_sorted

    # kw 匹配调权
    kw_zh = kwargs['kw_zh']
    kw_en = kwargs['kw_en']
    kw_num = len(kw_zh)
    
    for (combined_key, row) in result_dict.items():
        if row.get('final_score', 0) < 0.4 or 'shard' not in row:
            continue
        
        shard = row['shard']
        match_cnt = 0
        
        if has_chinese(shard):
            for kw in kw_zh:
                if kw in shard:
                    match_cnt += 1
        else:
            tokens_set = set([PORTER_STEMMER.stem(t) for t in re.split(REGEX_PATTERN, shard)])
            match_cnt = len(tokens_set.intersection(kw_en))
        
        match_score = (0.95 + pow(match_cnt / kw_num, 2)) if kw_num > 0 else 0.001
        row['match_cnt'] = match_cnt
        row['match_score'] = match_score
        row['final_score'] = row['final_score'] * match_score

    kwargs['result_dict'] = result_dict
    return kwargs


def concat_shards_by_rank(**kwargs):
    """拼接 chunks"""
    result_dict = kwargs['result_dict']
    top_doc_num: int = kwargs['top_doc_num']

    sorted_top_chunk = sorted(list(result_dict.values()), key=lambda d: -d['final_score'])
    
    simMatrix = {}
    sim_num = min(top_doc_num * 5 + 30, len(sorted_top_chunk))
    
    selectedDocs = []
    for i in range(sim_num):
        mmrStep(0.9, selectedDocs, sorted_top_chunk, simMatrix)

    res_arr_dict = {}
    res_num = 0

    this_chunk_num = 0
    this_index = []
    this_res = {}
    this_docs = []
    this_doc_ids = []
    this_shard = []
    this_title = []
    this_num_token = 0
    this_scores = 0.0

    for selected_index in selectedDocs:
        chunk_data = sorted_top_chunk[selected_index]
        
        doc_name = chunk_data['doc_name']
        shard = chunk_data['shard']
        index = chunk_data['index']
        title = chunk_data['title']
        doc_id = chunk_data['doc_id']

        tokens = ENCODING.encode(shard)
        this_num_token += len(tokens)
        this_shard.append(shard)
        this_index.append(index)
        this_title.append(title)
        this_chunk_num += 1
        this_scores += chunk_data['final_score']

        this_docs.append(doc_name)
        this_doc_ids.append(doc_id)

        if this_num_token >= TOKEN_LIMIT:
            this_res['text'] = this_shard
            this_res['ans_id'] = res_num
            this_res['doc_name'] = this_docs
            this_res['doc_id'] = this_doc_ids
            this_res['index'] = this_index
            this_res['title'] = this_title
            this_res['score'] = this_scores / this_chunk_num
            this_res['concat_score'] = 0.5
            
            res_arr_dict[(this_res['score'], tuple(this_res['text']))] = this_res

            this_chunk_num = 0
            this_res = {}
            this_docs = []
            this_doc_ids = []
            this_shard = []
            this_index = []
            this_title = []
            this_num_token = 0
            this_scores = 0.0
            res_num += 1
        
        if res_num >= top_doc_num:
            break
    
    sorted_res_arr_dict = sorted(res_arr_dict.items(), key=lambda d: -d[0][0])
    res_arr = []
    for item_index, sorted_item in enumerate(sorted_res_arr_dict):
        sorted_item[1]['ans_id'] = item_index
        res_arr.append(sorted_item[1])

    return res_arr


@app.route('/api-rqa-search/test', methods=['GET'])
def hello_world():
    return json_result(0, '', 'Service available')


@app.route('/api-rqa-search/stats', methods=['GET'])
def get_stats():
    """获取服务统计信息"""
    stats = {
        'pool_stats': get_pool_stats(),
        'mongo_pool_config': {
            'maxPoolSize': MONGO_POOL_CONFIG['maxPoolSize'],
            'minPoolSize': MONGO_POOL_CONFIG['minPoolSize'],
            'maxIdleTimeMS': MONGO_POOL_CONFIG['maxIdleTimeMS'],
        },
        'max_concurrent_requests': MAX_CONCURRENT_REQUESTS,
        'version': VERSION,
        'model': MODEL_NAME
    }
    try:
        if MONGO_PIPELINE and MONGO_PIPELINE.client:
            stats['mongo_pipeline_info'] = MONGO_PIPELINE.get_pool_info()
    except:
        pass
    return json_result(0, '', stats)


@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    """搜索服务主函数 - 带并发控制"""
    # ⭐ 并发控制
    acquired = REQUEST_SEMAPHORE.acquire(blocking=False)
    if not acquired:
        update_pool_stats('failed_requests')
        return json_result(-1, 'Service busy, please retry later', {
            'error': 'Too many concurrent requests',
            'max_concurrent': MAX_CONCURRENT_REQUESTS
        }), 503
    
    try:
        update_pool_stats('total_requests')
        update_pool_stats('active_requests')
        
        form = request.form
        query = form.get('query', '', str)
        query_en = form.get('query_dst', '', str)
        id = form.get('id', 0, int)
        is_debug = form.get('debug', 0, int) == 1
        top_doc_num = form.get('top_doc_num', 0, int)
        target_doc_name = form.get('target_doc_name', '', str)
        is_delete = form.get('is_delete', 0, int)
        delete_doc_id = form.get('delete_doc_id', '', str)
        
        # 参数校验
        if not query:
            return json_result(-1, 'query must not be null.', None)
        if id == 0:
            return json_result(-1, 'id must not be null.', None)
        if top_doc_num == 0:
            return json_result(-1, 'top_doc_num must not be null.', None)
        
        # 删除操作
        if is_delete == 1 and delete_doc_id:
            delete_doc_id_list = [int(t) for t in delete_doc_id.split(',')]
            MONGO_PIPELINE.collection.delete_many({'doc_id': {'$in': delete_doc_id_list}})
            return json_result(0, f'delete {delete_doc_id_list} successfully', None)

        start_time = time.time()
        code = 0
        msg = ''
        data = {
            'model': MODEL_NAME,
            'version': VERSION
        }

        try:
            json_arr = []
            
            # ⭐ 优化：只调用一次编码服务
            query_embed = encode_from_net(query)
            
            # ⭐ 复用编码结果
            query_rank_embed = query_embed
            query_expand = [query]
            query_embed_expand = [query_embed]
            query_rank_embed_expand = [query_embed]

            result_dict = {}
            params = {
                'id': id,
                'query': query,
                'query_expand': query_expand,
                'query_embed': query_embed,
                'query_embed_expand': query_embed_expand,
                'query_rank_embed': query_rank_embed,
                'query_rank_embed_expand': query_rank_embed_expand,
                'target_doc_name': target_doc_name,
                'flag_query_rel': True,
                'result_dict': result_dict
            }
            
            if has_chinese(query):
                params['query_zh'] = query
                params['query_en'] = query_en if query_en else query
            else:
                params['query_en'] = query
                params['query_zh'] = query
            
            if query_en.strip():
                params['query_en'] = query_en
            
            # Pipeline 执行
            params = recall_pipeline(**params)
            
            if not params['flag_query_rel']:
                code = 1
                data['msg'] = 'query must be relevant'
                data['doc_num'] = 0
                data['arr'] = []
                return json_result(code, msg, data)

            params = rank_pipeline(**params)
            params = rerank_pipeline(**params)
            
            params['top_doc_num'] = top_doc_num
            params['concat_num'] = CONCAT_CHUNK_NUM
            params['is_debug'] = is_debug

            similar_shards = concat_shards_by_rank(**params)
            
            for dict_item in similar_shards:
                score = dict_item['score']
                if score < SCORE_THREHOLD:
                    break
                json_arr.append(dict_item)

            data['arr'] = json_arr
            data['doc_num'] = len(json_arr)

        except Exception as e:
            code = -1
            msg = str(e)
            print(f'[Error] {traceback.format_exc()}')
            data['msg'] = msg
            data['doc_num'] = 0
            data['arr'] = []
            update_pool_stats('failed_requests')

        data['ts'] = int(datetime.datetime.timestamp(datetime.datetime.now()) * 1000)
        total_time = time.time() - start_time
        print(f'[Request] Total:{total_time:.2f}s Results:{len(json_arr) if "json_arr" in locals() else 0}')
        
        return json_result(code, msg, data)
        
    finally:
        REQUEST_SEMAPHORE.release()
        update_pool_stats('active_requests', -1)


@app.route('/api-rqa-search/download', methods=['GET'])
def download():
    doc_name = request.args.get('docName', '', str)
    query_id = request.args.get('queryId', 0, int)
    username = request.args.get('username', '', str)
    try:
        save_download_record(query_id, username, doc_name)
    except:
        pass
    
    file = '/mnt/hdd1/rqa_dir/db/papers_pdf/' + doc_name
    with open(file, 'rb') as f:
        stream = f.read()
    
    response = Response(stream, content_type='application/octet-stream')
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET,HEAD,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Referer,Accept,Origin,User-Agent'
    return response


def save_download_record(query_id: int, username: str, doc_name: str):
    if query_id == 0 or username == '':
        return
    
    global DOWNLOAD_PIPELINE
    if DOWNLOAD_PIPELINE is None:
        DOWNLOAD_PIPELINE = mongodb(MONGO_URL, MONGO_DB, "download_record")
        DOWNLOAD_PIPELINE.connect()
    
    document = {
        "query_id": query_id,
        "username": username,
        "doc_name": doc_name,
        "created_at": datetime.datetime.now()
    }
    DOWNLOAD_PIPELINE.insert_one(document)


def json_result(code: int, msg: str, data):
    return jsonify({'code': code, 'msg': msg, 'data': data})


# ==================== 主程序 ====================
faulthandler.enable()

config = configparser.ConfigParser()
config.read('./config/search_srv_pipeline_l.ini', encoding='UTF-8')

scheduler = BackgroundScheduler()
scheduler.add_job(crontab_update_config, 'interval', seconds=180, coalesce=True, replace_existing=True)
scheduler.add_job(crontab_connection_health_check, 'interval', seconds=30, coalesce=True, replace_existing=True)
scheduler.start()

print('\n' + '='*80)
print('🚀 Starting RQA Search Service')
print('='*80)

load_data(TABLE_NAME, MODEL_NAME)

print('\n' + '='*80)
print('📊 Service Configuration:')
print(f'  Model: {MODEL_NAME}')
print(f'  Version: {VERSION}')
print(f'  MongoDB Pool:')
print(f'    - maxPoolSize: {MONGO_POOL_CONFIG["maxPoolSize"]}')
print(f'    - minPoolSize: {MONGO_POOL_CONFIG["minPoolSize"]}')
print(f'    - maxIdleTimeMS: {MONGO_POOL_CONFIG["maxIdleTimeMS"]}ms')
print(f'    - socketTimeoutMS: {MONGO_POOL_CONFIG["socketTimeoutMS"]}ms')
print(f'  Concurrency:')
print(f'    - Max concurrent requests: {MAX_CONCURRENT_REQUESTS}')
print(f'    - Rerank batch size: 50 (optimized)')
print(f'  Optimizations:')
print(f'    - ✅ Parallel MongoDB queries')
print(f'    - ✅ Connection health check')
print(f'    - ✅ Request-level concurrency control')
print(f'    - ✅ Encode service result reuse')
print('='*80 + '\n')

if __name__ == '__main__':
    print(f'✅ Service ready at http://10.70.223.31:9510')
    print(f'   - Health:  GET  /api-rqa-search/test')
    print(f'   - Stats:   GET  /api-rqa-search/stats')
    print(f'   - Search:  POST /api-rqa-search/search')
    print('='*80 + '\n')
    
    app.run('10.70.223.31', port=9510, threaded=True, processes=1)
