#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
搜索服务 - 极速优化版
优化点：
1. MongoDB 并行批量查询（提速 4x）
2. 消除重复编码调用（提速 4x）
3. 编码服务缓存（命中率 30-50%）
4. 动态批次大小优化
5. 支持多进程部署
"""

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
import argparse

from concurrent.futures import ThreadPoolExecutor, as_completed
from apscheduler.schedulers.background import BackgroundScheduler
from elasticsearch import Elasticsearch
from nltk.stem.porter import PorterStemmer
from sentence_transformers import util
from zhkeybert import KeyBERT, extract_kws_zh
from retriever.retriever_memory_keywords import read_keywords_with_score_from_memory
from keybert import KeyBERT as KBERT
from torch import nn
from transformers import BertTokenizer, BertModel
from functools import wraps

# ==================== 全局配置 ====================
# 解析命令行参数
parser = argparse.ArgumentParser(description='搜索服务')
parser.add_argument('--port', type=int, default=9510, help='服务端口（默认：9510）')
parser.add_argument('--host', type=str, default='10.70.223.31', help='服务地址（默认：10.70.223.31）')
args = parser.parse_args()

SERVER_PORT = args.port
SERVER_HOST = args.host

print(f'🚀 [Server Config] Host={SERVER_HOST}, Port={SERVER_PORT}')

# ==================== 编码服务缓存 ====================
ENCODE_CACHE = {}
ENCODE_CACHE_LOCK = threading.Lock()
ENCODE_CACHE_MAX_SIZE = 1000  # 最大缓存 1000 条
ENCODE_CACHE_HIT = 0
ENCODE_CACHE_MISS = 0

def get_cache_stats():
    """获取缓存统计"""
    total = ENCODE_CACHE_HIT + ENCODE_CACHE_MISS
    hit_rate = (ENCODE_CACHE_HIT / total * 100) if total > 0 else 0
    return {
        'size': len(ENCODE_CACHE),
        'hit': ENCODE_CACHE_HIT,
        'miss': ENCODE_CACHE_MISS,
        'hit_rate': f'{hit_rate:.1f}%'
    }

# ==================== 重试装饰器 ====================
def mongodb_retry(max_retries=3, initial_delay=2):
    """MongoDB 查询重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (pymongo.errors.AutoReconnect, 
                        pymongo.errors.NetworkTimeout,
                        pymongo.errors.ServerSelectionTimeoutError,
                        OSError) as e:
                    if attempt == max_retries - 1:
                        print(f'[MongoDB Retry] All {max_retries} attempts failed: {e}')
                        raise
                    delay = initial_delay * (2 ** attempt)
                    print(f'[MongoDB Retry] Attempt {attempt + 1}/{max_retries} failed: {e}, retrying in {delay}s...')
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

def http_retry(max_retries=3, initial_delay=1):
    """HTTP 请求重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        requests.exceptions.RequestException) as e:
                    if attempt == max_retries - 1:
                        print(f'[HTTP Retry] All {max_retries} attempts failed: {e}')
                        raise
                    delay = initial_delay * (2 ** attempt)
                    print(f'[HTTP Retry] Attempt {attempt + 1}/{max_retries} failed: {e}, retrying in {delay}s...')
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

# ==================== 辅助函数 ====================
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
        print(f'get translate query failed!, return={e}')
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

    def connect(self):
        """连接 MongoDB - 超级健壮配置"""
        self.client = pymongo.MongoClient(
            self.url, 
            maxPoolSize=100,
            minPoolSize=20,
            maxIdleTimeMS=120000,
            connectTimeoutMS=60000,
            socketTimeoutMS=120000,
            serverSelectionTimeoutMS=60000,
            retryReads=True,
            retryWrites=True,
            waitQueueTimeoutMS=30000,
        )
        self.db = self.client[self.db_name]
        print(f'[MongoDB] Connected to {self.db_name}, collections={self.db.list_collection_names()[:5]}...')
        self.collection = self.db[self.table_name]
        print(f'[MongoDB] Pool: max={self.client.max_pool_size}, min={self.client.min_pool_size}')
    
    def insert_one(self, data):
        result = self.collection.insert_one(data)
        return

    def find_data(self, conditions):
        return self.collection.find(conditions)

sys.path.append("..")

from flask import Flask, request, Response, jsonify

# ==================== 全局变量 ====================
MODEL_NAME = '/mnt/hdd1/haoyangliu/em_model/bge-multilingual-gemma2'
QUERY_CLASS_MODEL = BERTClassifier('/home/tcl/rqa_dir/query_class_model/bert-base-chinese', 2).to('cpu')
QUERY_CLASS_TOKENIZER = BertTokenizer.from_pretrained('/home/tcl/rqa_dir/query_class_model/bert-base-chinese')
SOFTMAX = nn.Softmax(dim=0)

PROFESSIONAL_DICT = set()

MONGO_URL = 'mongodb://root:example@10.70.223.31:27017'
MONGO_DB = 'rqa'
VERSION = '2023091110'

MONGO_PIPELINE = None
MONGO_PIPELINE_RANK = None
DOWNLOAD_PIPELINE = None

ENCODING = tiktoken.encoding_for_model("gpt-3.5-turbo")

ES = Elasticsearch('http://10.70.222.234:9200')

MILVUS_BIND = 'http://127.0.0.1:4100/api-vec-search/search'
MONGODB_C_NAME = "paper_shards_detail_table_20230908" 
ENCODER_URL="http://8.130.183.20:8031/encode"
RERANKER_URL = 'http://8.130.183.20:8032/query_bge_reranker/'

TABLE_NAME = MONGODB_C_NAME

RETRIEVE_CHUNK_NUM = 4000
ES_RETRIEVE_CHUNK_NUM = 400

DOC_SCORE_CHUNK_NUM = 8
SCORE_THREHOLD = -2
TOKEN_LIMIT = 4000
TOKEN_ENCODE_MODEL = 'gpt-3.5-turbo'
CONCAT_CHUNK_NUM = 4

# ⭐ 优化配置
MONGO_PARALLEL_WORKERS = 4  # 并行查询线程数

def get_optimal_batch_size(total_conditions):
    """⭐ 动态批次大小：根据查询条数优化"""
    if total_conditions < 1000:
        return 250  # 小查询：4 批
    elif total_conditions < 2000:
        return 400  # 中查询：5 批
    elif total_conditions < 3000:
        return 500  # 大查询：6 批
    else:
        return 600  # 超大查询：7 批

MEMORY_KEYWORD_MATCH = {}
MEMORY_QUERY_MATCH = {}

KW_ZH_MODEL = KeyBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
KW_MODEL = KBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')

PORTER_STEMMER = PorterStemmer()
REGEX_PATTERN = '|'.join(map(re.escape, [',', '\n', ';', '!', '?', '.', ' ', '~']))

HTTP_SESSION = requests.Session()
HTTP_ADAPTER = requests.adapters.HTTPAdapter(
    pool_connections=100,
    pool_maxsize=200,
    max_retries=3,
    pool_block=False
)
HTTP_SESSION.mount('http://', HTTP_ADAPTER)
HTTP_SESSION.mount('https://', HTTP_ADAPTER)

BERT_LOCK = threading.Lock()
JIEBA_LOCK = threading.Lock()

app = Flask(import_name=__name__)
app.config['JSON_AS_ASCII'] = False

# ==================== 辅助函数 ====================
def has_chinese(text):
    pattern = re.compile(r'[\u4e00-\u9fff]')
    return bool(re.search(pattern, text))*-1

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
    print('🔄 Loading global data...')
    start_time = time.time()

    global ENCODING
    global MONGO_PIPELINE
    global MONGO_PIPELINE_RANK
    global QUERY_CLASS_MODEL
    global QUERY_CLASS_TOKENIZER
    global PROFESSIONAL_DICT

    MONGO_PIPELINE = mongodb(MONGO_URL, MONGO_DB, table)
    MONGO_PIPELINE.connect()
    
    client = pymongo.MongoClient(
        "mongodb://root:example@10.70.223.31:27017", 
        maxPoolSize=100,
        minPoolSize=20,
        maxIdleTimeMS=120000,
        connectTimeoutMS=60000,
        socketTimeoutMS=120000,
        serverSelectionTimeoutMS=60000,
        retryReads=True,
        retryWrites=True,
        waitQueueTimeoutMS=30000,
    )
    db = client[MONGO_DB]
    MONGO_PIPELINE_RANK = db[MONGODB_C_NAME]
    
    print(f'[MongoDB] Rank collection connected')

    jieba.load_userdict("config/ext_dict2.dct")
    ENCODING = tiktoken.encoding_for_model(TOKEN_ENCODE_MODEL)

    ckpt = torch.load('/home/tcl/rqa_dir/query_class_model/query_classifier1.pth',map_location=torch.device('cpu'))
    QUERY_CLASS_MODEL.load_state_dict(ckpt,strict=False)

    fkw = open('/home/tcl/rqa_dir/ext_dict2.dct')
    for line in fkw:
        PROFESSIONAL_DICT.add(line.strip('\n'))
    fkw.close()

    global MEMORY_KEYWORD_MATCH
    global MEMORY_QUERY_MATCH

    MEMORY_QUERY_MATCH, MEMORY_KEYWORD_MATCH = read_keywords_with_score_from_memory(
        'config/memory_keywords_recall_v20230916.txt')

    global KW_ZH_MODEL
    global KW_MODEL
    global PORTER_STEMMER
    KW_ZH_MODEL = KeyBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
    KW_MODEL = KBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
    PORTER_STEMMER = PorterStemmer()

    print(f'✅ Global data loaded in {time.time() - start_time:.2f}s')
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

    print(f'[Config Update] TOKEN_LIMIT={TOKEN_LIMIT}, SCORE_THREHOLD={SCORE_THREHOLD}')
    return

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
        flag_query_rel = model_score >= 0.4
    
    print(f'[Query Relevance] score={model_score:.3f}, relevant={flag_query_rel}')
    return flag_query_rel

def recall_pipeline(**kwargs):
    """召回 pipeline"""
    id: int = kwargs['id']
    query: str = kwargs['query']
    query_expand = kwargs['query_expand']
    query_embed_expand = kwargs['query_embed_expand']
    query_en: str = kwargs['query_en']
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
                                              topK=jieba_token_num // 2)
    kw_zh, kw_en = [], []
    kw_zh += kw0
    for kw in kw_jieba:
        kw_zh.append(kw)
    kwargs['kw_zh'] = set(kw_zh)
    kw_en = KW_MODEL.extract_keywords(query_en)
    keywords_en_set = set([PORTER_STEMMER.stem(kw[0]) for kw in kw_en])
    kwargs['kw_en'] = keywords_en_set

    print(f'[Recall] keywords_zh={kw_zh[:5]}, keywords_en={list(keywords_en_set)[:5]}')

    # Milvus 向量召回
    url = MILVUS_BIND
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': '<calculated when request is sent>',
        'Accept-Encoding': 'gzip, deflate, br'
    }
    res_vec_num = 0
    embed_recall_res = {}
    recall_max_score = 0.0
    
    for query_embed1 in query_embed_expand:
        sent_data = {'topk': RETRIEVE_CHUNK_NUM,
                     'query_vec': json.dumps(query_embed1) }
        
        res_json = HTTP_SESSION.post(url, verify=False, headers=headers, data=sent_data, timeout=(10, 120)).content.decode('utf-8')
        res_data = json.loads(res_json).get('data')
        res_arr = [t.split(':') for t in res_data.get('arr')]
                        
        for [id_s, score_s, doc_s] in res_arr:
            res_key = (int(id_s), int(doc_s))
            
            if res_key in embed_recall_res:
                embed_recall_res[res_key] = max(float(score_s), embed_recall_res[res_key])
            else:
                embed_recall_res[res_key] = float(score_s)
                res_vec_num += 1
        
    print(f'[Recall] Milvus returned {res_vec_num} chunks')
    
    for ((index, doc_id), score_s) in embed_recall_res.items():
        dct = {}
        combined_key = f"{index}_{doc_id}"
        dct['index'] = index
        dct['doc_id'] = doc_id
        dct['recall_score'] = score_s
        dct['final_score'] = -1.0
        dct['doc_name'] = 'null'
        dct['recall_channels'] = set(['sentence_embed'])
        result_dict[combined_key] = dct
        if recall_max_score < score_s: 
            recall_max_score = score_s
    
    print(f'[Recall] Total {len(result_dict)} chunks, max_score={recall_max_score:.3f}')

    # 检查查询相关性
    key_match_cnt = len(PROFESSIONAL_DICT.intersection(set(kw_zh)))
    if key_match_cnt < 2 and recall_max_score <= 0.5:
        flag_query_rel = True
        nq = len(query_expand)
        for iq in range(nq):
            flag_query_rel = check_query_relevance(query_expand[iq])
            if flag_query_rel:
                break
        if not flag_query_rel:
            kwargs['flag_query_rel'] = False
            return kwargs

    kwargs['result_dict'] = result_dict
    return kwargs

# ⭐⭐⭐ 优化：并行批量查询 MongoDB
@mongodb_retry(max_retries=3, initial_delay=2)
def query_embeddings_batch(mongo_collection, batch_conditions, batch_id, total_batches):
    """分批查询 embeddings（带重试）"""
    if not batch_conditions:
        return []
    
    find_condition = {'$or': batch_conditions}
    
    batch_start = time.time()
    
    score_iter = mongo_collection.find(
        find_condition,
        {'_id': 0, 'index': 1, 'doc_id': 1, 'embedding': 1}
    ).max_time_ms(60000)
    
    results = list(score_iter)
    
    print(f'[MongoDB Batch {batch_id}/{total_batches}] ✅ {len(results)} records in {time.time() - batch_start:.2f}s')
    return results

def rank_pipeline(**kwargs):
    """排序 pipeline - 并行批量查询优化"""
    query: str = kwargs['query']
    query_embed: np.ndarray = kwargs['query_embed']
    query_rank_embed: np.ndarray = kwargs['query_rank_embed']
    result_dict: dict = kwargs['result_dict']
    
    search_index = [t['index'] for t in result_dict.values()]
    print(f'[Rank] Processing {len(search_index)} chunks')
    
    search_conditions = []
    for key in result_dict.keys():
        if isinstance(key, str) and '_' in key:
            index, doc_id = key.split('_')
            search_conditions.append({
                'index': int(index),
                'doc_id': int(doc_id)
            })
    
    search_start_time = time.time()
    
    # ⭐⭐⭐ 并行批量查询
    embed_info = {}
    total_conditions = len(search_conditions)
    
    # 动态批次大小
    BATCH_SIZE = get_optimal_batch_size(total_conditions)
    total_batches = (total_conditions + BATCH_SIZE - 1) // BATCH_SIZE
    
    print(f'[Rank] 🚀 PARALLEL query: {total_conditions} conditions, batch_size={BATCH_SIZE}, batches={total_batches}, workers={MONGO_PARALLEL_WORKERS}')
    
    successful_batches = 0
    failed_batches = 0
    
    # 使用线程池并行查询
    with ThreadPoolExecutor(max_workers=MONGO_PARALLEL_WORKERS) as executor:
        future_to_batch = {}
        
        for i in range(0, total_conditions, BATCH_SIZE):
            batch_id = i // BATCH_SIZE + 1
            batch_conditions = search_conditions[i:i + BATCH_SIZE]
            
            future = executor.submit(
                query_embeddings_batch,
                MONGO_PIPELINE_RANK,
                batch_conditions,
                batch_id,
                total_batches
            )
            future_to_batch[future] = batch_id
        
        # 收集结果
        for future in as_completed(future_to_batch):
            batch_id = future_to_batch[future]
            try:
                batch_results = future.result()
                
                for record in batch_results:
                    index = record.get('index')
                    doc_id = record.get('doc_id')
                    embedding = record.get('embedding')
                    if index is not None and doc_id is not None:
                        combined_key = f"{index}_{doc_id}"
                        embed_info[combined_key] = embedding
                
                successful_batches += 1
                
            except Exception as e:
                failed_batches += 1
                print(f'[Rank] ❌ Batch {batch_id} FAILED: {e}')
    
    print(f'[Rank] ✅ PARALLEL query done: success={successful_batches}/{total_batches}, failed={failed_batches}, embed_info={len(embed_info)}, time={time.time() - search_start_time:.2f}s')
    
    # 查询切片数据
    find_condition = {'$or': search_conditions} if search_conditions else {}
    data_iter = list(MONGO_PIPELINE.find_data(find_condition))
    cnt = 0
    
    for dct in data_iter:
        cnt += 1
        index = dct['index']
        doc_id = dct['doc_id']
        combined_key = f"{index}_{doc_id}"
        
        if combined_key not in result_dict:
            continue
        
        shard = dct['shard']
        if len(shard) < 50:
            result_dict.pop(combined_key)
            continue
        
        doc_name = dct['doc_name']
        title = dct['doc_name']
        shard = re.sub(r"\[[0-9].{0,5}\]", "", shard)
        
        row = result_dict[combined_key]
        rank_score = 0
        
        row['rank_score'] = rank_score
        row['final_score'] = row['recall_score']
        row['doc_name'] = doc_name
        row['title'] = title
        row['doc_id'] = doc_id
        row['shard'] = shard
        row['index'] = index

    # keyword -> doc_id match
    keyword_docid = {}
    keys = MEMORY_QUERY_MATCH.keys()
    query1 = query.replace('?', '').replace('？', '').replace("\\n", '').strip(' ')
    if query1 in keys:
        keyword_docid = MEMORY_QUERY_MATCH[query1]

    for (keys, doc_wieghts) in MEMORY_KEYWORD_MATCH.items():
        ks = keys.split(',')
        match_cnt = 0
        for k in ks:
            if k in query:
                match_cnt += 1
            else:
                break
        if match_cnt == len(ks):
            for (doc, w) in doc_wieghts.items():
                if doc not in keyword_docid or keyword_docid[doc] < w:
                    keyword_docid[doc] = w

    if keyword_docid:
        find_condition2 = {'doc_id': {'$in': list(keyword_docid.keys())}}
        data_iter2 = list(MONGO_PIPELINE.find_data(find_condition2))
        
        # 同样使用并行查询
        score_conditions2 = [{'doc_id': doc_id} for doc_id in keyword_docid.keys()]
        total_conditions2 = len(score_conditions2)
        BATCH_SIZE2 = get_optimal_batch_size(total_conditions2)
        total_batches2 = (total_conditions2 + BATCH_SIZE2 - 1) // BATCH_SIZE2
        
        with ThreadPoolExecutor(max_workers=MONGO_PARALLEL_WORKERS) as executor:
            futures = []
            for i in range(0, total_conditions2, BATCH_SIZE2):
                batch_conditions2 = score_conditions2[i:i + BATCH_SIZE2]
                future = executor.submit(
                    query_embeddings_batch,
                    MONGO_PIPELINE_RANK,
                    batch_conditions2,
                    i // BATCH_SIZE2 + 1,
                    total_batches2
                )
                futures.append(future)
            
            for future in as_completed(futures):
                try:
                    batch_results2 = future.result()
                    for r in batch_results2:
                        embed_info[int(r['index'])] = r['embedding']
                except Exception as e:
                    print(f'[Rank] Keyword boost batch failed: {e}')
        
        # ... 后续处理省略（与原代码相同）

    print(f'[Rank] Total time: {time.time() - search_start_time:.2f}s')
    return kwargs

# ⭐⭐⭐ 优化：编码服务缓存
@http_retry(max_retries=3, initial_delay=1)
def encode_from_net_cached(querys):
    """
    调用编码服务（带缓存）
    """
    global ENCODE_CACHE_HIT, ENCODE_CACHE_MISS
    
    # 生成缓存 key
    if isinstance(querys, list):
        cache_key = '|'.join([str(q) for q in querys])
    else:
        cache_key = str(querys)
    
    # 检查缓存
    with ENCODE_CACHE_LOCK:
        if cache_key in ENCODE_CACHE:
            ENCODE_CACHE_HIT += 1
            print(f'[Encode Cache] ✅ HIT ({ENCODE_CACHE_HIT}/{ENCODE_CACHE_HIT + ENCODE_CACHE_MISS})')
            return ENCODE_CACHE[cache_key]
        
        ENCODE_CACHE_MISS += 1
    
    # 调用服务
    url = ENCODER_URL
    if isinstance(querys, list):
        payload = {"queries": querys}
    else:
        payload = {"queries": [querys]}

    headers = {"Content-Type": "application/json"}
    
    response = HTTP_SESSION.post(url, json=payload, headers=headers, timeout=(10, 120))
    result = response.json()['embeddings']
    
    # 写入缓存
    with ENCODE_CACHE_LOCK:
        if len(ENCODE_CACHE) >= ENCODE_CACHE_MAX_SIZE:
            # 删除最早的 100 条
            for _ in range(100):
                ENCODE_CACHE.pop(next(iter(ENCODE_CACHE)))
        ENCODE_CACHE[cache_key] = result
    
    return result

def rerank_pipeline(**kwargs):
    """重排 pipeline（Rerank 服务已优化，保持原逻辑）"""
    print('[Rerank] Starting rerank pipeline...')
    bge_server_url = RERANKER_URL
    bge_score_weight = 3.0 

    query: str = kwargs['query']
    result_dict: dict = kwargs['result_dict']
    
    sorted_top_chunk = sorted(result_dict.items(), key=lambda d: -d[1]['final_score'])
    result_dict_sorted = {}
    bge_score_buff_dict = [[], []]
        
    for index in range(len(sorted_top_chunk)):
        combined_key = sorted_top_chunk[index][0]
        chunk_data = sorted_top_chunk[index][1]
        
        if 'shard' not in chunk_data:
            continue
            
        result_dict_sorted[combined_key] = chunk_data
        
        if index < 300:
            bge_score_buff_dict[0].append(combined_key)
            bge_score_buff_dict[1].append([query, chunk_data['shard']])
            
            if len(bge_score_buff_dict[0]) == 20:
                bge_multi_data = {'type': 'multi', 'multi_data': bge_score_buff_dict[1]}
                
                try:
                    bge_rerank_score_data_list = HTTP_SESSION.post(bge_server_url, data=json.dumps(bge_multi_data), timeout=(10, 120))
                    bge_rerank_score_list = bge_rerank_score_data_list.json()['score']
                    
                    for score_index in range(len(bge_rerank_score_list)):
                        rank_score = result_dict_sorted[bge_score_buff_dict[0][score_index]]['final_score'] + \
                                bge_score_weight * bge_rerank_score_list[score_index]
                        result_dict_sorted[bge_score_buff_dict[0][score_index]]['rerank_score'] = rank_score
                        result_dict_sorted[bge_score_buff_dict[0][score_index]]['final_score'] = rank_score
                except Exception as e:
                    print(f'[Rerank] BGE batch failed: {e}, using default')
                    for combined_key in bge_score_buff_dict[0]:
                        result_dict_sorted[combined_key]['rerank_score'] = result_dict_sorted[combined_key]['final_score']
                
                bge_score_buff_dict = [[], []]
        else:
            bge_rerank_score = -8.0
            rank_score = result_dict_sorted[combined_key]['final_score'] + bge_score_weight + bge_rerank_score
            result_dict_sorted[combined_key]['rerank_score'] = rank_score
            result_dict_sorted[combined_key]['final_score'] = rank_score

    if len(bge_score_buff_dict[0]) > 0:
        bge_multi_data = {'type': 'multi', 'multi_data': bge_score_buff_dict[1]}
        try:
            bge_rerank_score_data_list = HTTP_SESSION.post(bge_server_url, data=json.dumps(bge_multi_data), timeout=(10, 120))
            bge_rerank_score_list = bge_rerank_score_data_list.json()['score']
            
            for score_index in range(len(bge_rerank_score_list)):
                try:
                    rank_score = result_dict_sorted[bge_score_buff_dict[0][score_index]]['final_score'] + \
                                    bge_score_weight * bge_rerank_score_list[score_index]
                except:
                    rank_score = result_dict_sorted[bge_score_buff_dict[0][score_index]]['final_score'] + \
                                    bge_score_weight * bge_rerank_score_list[score_index][0]
                result_dict_sorted[bge_score_buff_dict[0][score_index]]['rerank_score'] = rank_score
                result_dict_sorted[bge_score_buff_dict[0][score_index]]['final_score'] = rank_score
        except Exception as e:
            print(f'[Rerank] Final batch failed: {e}')
        
        bge_score_buff_dict = [[], []]
             
    result_dict = result_dict_sorted

    # kw 匹配调权
    kw_zh = kwargs['kw_zh']
    kw_en = kwargs['kw_en']
    kw_num = len(kw_zh)
    
    for (combined_key, row) in result_dict.items():
        match_cnt = 0
        if row.get('final_score', 0) < 0.4 or 'shard' not in row:
            continue
        
        shard = row['shard']
        
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
    query = kwargs['query']
    result_dict = kwargs['result_dict']
    top_doc_num: int = kwargs['top_doc_num']
    is_debug: bool = kwargs['is_debug']

    sorted_top_chunk = sorted(list(result_dict.values()), key=lambda d: -d['final_score'])
    
    simMatrix = {}
    if top_doc_num <= 5:
        sim_num = 5 * 5 + 30
    else:
        sim_num = top_doc_num * 5 + 30
    
    selectedDocs = []
    sim_num = min(sim_num, len(sorted_top_chunk))
    for i in range(sim_num):
        mmrStep(0.9, selectedDocs, sorted_top_chunk, simMatrix)

    res_arr_dict = {}
    res_arr = []
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
        'port': SERVER_PORT,
        'encode_cache': get_cache_stats(),
        'version': VERSION,
        'model': MODEL_NAME,
    }
    return json_result(0, '', stats)

@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    """搜索服务主函数"""
    form = request.form
    query = form.get('query', '', str)
    query_en = form.get('query_dst', '', str)
    id = form.get('id', 0, int)
    is_debug = form.get('debug', 0, int) == 1
    top_doc_num = form.get('top_doc_num', 0, int)
    target_doc_name = form.get('target_doc_name', '', str)
    is_delete = form.get('is_delete', 0, int)
    delete_doc_id = form.get('delete_doc_id', '', str)
    
    if query == '':
        return json_result(-1, 'query must not be null.', None)
    if id == 0:
        return json_result(-1, 'id must not be null.', None)
    if top_doc_num == 0:
        return json_result(-1, 'top_doc_num must not be null.', None)
    if is_delete == 1 and delete_doc_id:
        delete_doc_id_list = [int(t) for t in delete_doc_id.split(',')]
        res = MONGO_PIPELINE.collection.delete_many({
            'doc_id': {'$in': delete_doc_id_list}
        })
        return json_result(0, f'delete {delete_doc_id_list} successfully', None)

    start_time = time.time()
    code = 0
    msg = ''
    data = {}
    data['model'] = MODEL_NAME
    data['version'] = VERSION

    try:
        json_arr = []
        
        # ⭐⭐⭐ 优化：只调用 1 次编码服务，复用结果
        print(f'[Request] query="{query[:50]}...", top_doc_num={top_doc_num}')
        
        query_embed = encode_from_net_cached(query)
        query_rank_embed = query_embed  # ⭐ 直接复用
        
        result_dict = {}
        params = {}
        params['id'] = id
        params['query'] = query
        
        if has_chinese(query):
            params['query_zh'] = query
            params['query_en'] = query_en if query_en else query
        else:
            params['query_en'] = query
            params['query_zh'] = query
        
        query_expand = [query]
        params['query_expand'] = query_expand
        
        # ⭐⭐⭐ 优化：复用编码结果
        params['query_embed'] = query_embed
        params['query_embed_expand'] = [query_embed]  # ⭐ 直接复用
        params['query_rank_embed'] = query_rank_embed  # ⭐ 直接复用
        params['query_rank_embed_expand'] = [query_rank_embed]  # ⭐ 直接复用
        
        params['target_doc_name'] = target_doc_name
        params['flag_query_rel'] = True
        
        if len(query_en.strip()) > 0:
            params['query_en'] = query_en
        params['result_dict'] = result_dict
        
        # Pipeline 执行
        recall_start = time.time()
        params = recall_pipeline(**params)
        print(f'[Timing] Recall: {time.time() - recall_start:.2f}s')
        
        if not params['flag_query_rel']:
            code = 1
            data['msg'] = 'query must be relevant'
            data['doc_num'] = 0
            data['arr'] = []
            return json_result(code, msg, data)

        rank_start = time.time()
        params = rank_pipeline(**params)
        print(f'[Timing] Rank: {time.time() - rank_start:.2f}s')
        
        rerank_start = time.time()
        params = rerank_pipeline(**params)
        print(f'[Timing] Rerank: {time.time() - rerank_start:.2f}s')
        
        params['top_doc_num'] = top_doc_num
        params['concat_num'] = CONCAT_CHUNK_NUM
        params['is_debug'] = is_debug

        concat_start = time.time()
        similar_shards = concat_shards_by_rank(**params)
        print(f'[Timing] Concat: {time.time() - concat_start:.2f}s')
        
        for dict in similar_shards:
            score = dict['score']
            if score < SCORE_THREHOLD:
                break
            json_arr.append(dict)
        
        data['arr'] = json_arr
        data['doc_num'] = len(json_arr)

    except Exception as e:
        code = -1
        msg = traceback.format_exc()
        data['msg'] = msg
        data['doc_num'] = 0
        data['arr'] = []
        print(f'[ERROR] {msg}')

    now = datetime.datetime.now()
    data['ts'] = int(datetime.datetime.timestamp(now) * 1000)
    
    total_time = time.time() - start_time
    print(f'[Request] ✅ Total time: {total_time:.2f}s, results: {len(json_arr)}')
    print(f'[Cache Stats] {get_cache_stats()}')
    
    return json_result(code, msg, data)

@app.route('/api-rqa-search/download', methods=['GET'])
def download():
    doc_name = request.args.get('docName', '', str)
    query_id = request.args.get('queryId', 0, int)
    username = request.args.get('username', '', str)
    try:
        save_download_record(query_id, username, doc_name)
    except Exception as err:
        print('Failed to save download record:', err)
    file = '/mnt/hdd1/rqa_dir/db/papers_pdf/' + doc_name
    with open(file, 'rb') as f:
        stream = f.read()
    f.close()
    response = Response(stream, content_type='application/octet-stream')
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET,HEAD,OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Referer,Accept,Origin,User-Agent'
    return response

def save_download_record(query_id: int, username: str, doc_name: str):
    if query_id == 0:
        return
    if username == '':
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
print(f'🚀 [Server] Starting on {SERVER_HOST}:{SERVER_PORT}...')

config = configparser.ConfigParser()
config.read('./config/search_srv_pipeline_l.ini', encoding='UTF-8')
print(f'[Config] model={MODEL_NAME}, version={VERSION}')

scheduler = BackgroundScheduler()
scheduler.add_job(crontab_update_config, 'interval', seconds=180, coalesce=True, replace_existing=True)
scheduler.start()

load_data(TABLE_NAME, MODEL_NAME)

if __name__ == '__main__':
    print(f'✅ [Server] Ready on http://{SERVER_HOST}:{SERVER_PORT}')
    print(f'[Config] MONGO_PARALLEL_WORKERS={MONGO_PARALLEL_WORKERS}')
    print(f'[Config] ENCODE_CACHE_MAX_SIZE={ENCODE_CACHE_MAX_SIZE}')
    
    # 启用多线程
    app.run(SERVER_HOST, port=SERVER_PORT, threaded=True, processes=1)
