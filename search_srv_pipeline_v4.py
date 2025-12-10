#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
搜索服务 V4 - 超长超时版本 + 严格错误处理
主要改进：
1. 所有超时时间 × 10 倍
2. 更严格的 Score count mismatch 处理
3. 更小的批次大小（12 → 8）
4. 更多的重试次数（2 → 3）
5. 更详细的错误日志
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

# ==================== 版本信息 ====================
VERSION = '2023091110_V4_10X_TIMEOUT'
print(f'🚀 [Version] {VERSION} - 10倍超时 + 严格错误处理')

# ==================== 新增：重试装饰器（超时时间 × 10） ====================
def mongodb_retry(max_retries=3, initial_delay=20):  # 2s → 20s
    """MongoDB 查询重试装饰器，支持指数退避（超时 × 10）"""
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
                    delay = initial_delay * (2 ** attempt)  # 20s, 40s, 80s
                    print(f'[MongoDB Retry] Attempt {attempt + 1}/{max_retries} failed: {e}, retrying in {delay}s...')
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

def http_retry(max_retries=3, initial_delay=10, timeout=(100, 600)):  # 1s → 10s, (10,60) → (100,600)
    """HTTP 请求重试装饰器（超时 × 10）"""
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
        # ⭐ 超时 × 10: (10, 30) → (100, 300)
        res_json = HTTP_SESSION.post(url, verify=False, headers=headers, data=data,
                                 timeout=(100, 300)).content.decode('utf-8')
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
        # ⭐ 超级超时配置：所有超时 × 10
        self.client = pymongo.MongoClient(
            self.url, 
            maxPoolSize=100,                    # 最大连接池
            minPoolSize=20,                     # 最小连接池
            maxIdleTimeMS=1200000,              # 空闲时间 1200 秒（120s × 10）
            connectTimeoutMS=600000,            # 连接超时 600 秒（60s × 10）
            socketTimeoutMS=1200000,            # ⭐ Socket 超时 1200 秒（120s × 10）
            serverSelectionTimeoutMS=600000,    # 服务器选择超时 600 秒（60s × 10）
            retryReads=True,                    # 启用读重试
            retryWrites=True,                   # 启用写重试
            waitQueueTimeoutMS=300000,          # 等待连接超时 300 秒（30s × 10）
        )
        self.db = self.client[self.db_name]
        print(f'[MongoDB] Connected to db={self.db_name}.{self.table_name}')
        print(f'[MongoDB] Config: poolSize=100, socketTimeout=1200s, connectTimeout=600s')
        self.collection = self.db[self.table_name]
    
    def insert_one(self, data):
        result = self.collection.insert_one(data)
        return

    def find_data(self, conditions):
        return self.collection.find(conditions)

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
VERSION_NUM = '2023091110'

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

# ⭐ 分批查询配置
MONGO_BATCH_SIZE = 500

# memory keyword match
MEMORY_KEYWORD_MATCH = {}
MEMORY_QUERY_MATCH = {}

# keyword extract model
KW_ZH_MODEL = None
KW_MODEL = None

# nlp相关组件
PORTER_STEMMER = PorterStemmer()
REGEX_PATTERN = '|'.join(map(re.escape, [',', '\n', ';', '!', '?', '.', ' ', '~']))

# ⭐ HTTP Session 配置（连接池 × 2）
HTTP_SESSION = requests.Session()
HTTP_ADAPTER = requests.adapters.HTTPAdapter(
    pool_connections=200,  # 100 → 200
    pool_maxsize=400,      # 200 → 400
    max_retries=3,
    pool_block=False
)
HTTP_SESSION.mount('http://', HTTP_ADAPTER)
HTTP_SESSION.mount('https://', HTTP_ADAPTER)

# 线程安全：BERT模型推理锁
BERT_LOCK = threading.Lock()

# 线程安全：Jieba分词锁
JIEBA_LOCK = threading.Lock()

app = Flask(import_name=__name__)
app.config['JSON_AS_ASCII'] = False

def has_chinese(text):
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
    """MMR 算法步骤"""
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
    print('🔄 [Loading] Starting to load global data...')
    start_time = time.time()

    global ENCODING
    global MONGO_PIPELINE
    global MONGO_PIPELINE_RANK
    global QUERY_CLASS_MODEL
    global QUERY_CLASS_TOKENIZER
    global PROFESSIONAL_DICT

    # ⭐ MongoDB 超时配置 × 10
    MONGO_PIPELINE = mongodb(MONGO_URL, MONGO_DB, table)
    MONGO_PIPELINE.connect()
    
    # ⭐ RANK MongoDB 配置 × 10
    client = pymongo.MongoClient(
        "mongodb://root:example@10.70.223.31:27017", 
        maxPoolSize=100,
        minPoolSize=20,
        maxIdleTimeMS=1200000,              # 1200 秒
        connectTimeoutMS=600000,            # 600 秒
        socketTimeoutMS=1200000,            # 1200 秒
        serverSelectionTimeoutMS=600000,    # 600 秒
        retryReads=True,
        retryWrites=True,
        waitQueueTimeoutMS=300000,          # 300 秒
    )
    db = client[MONGO_DB]
    MONGO_PIPELINE_RANK = db[MONGODB_C_NAME]
    
    print(f'[Loading] MongoDB connected with 10× timeout config')

    # 加载token计算模块
    jieba.load_userdict("config/ext_dict2.dct")
    ENCODING = tiktoken.encoding_for_model(TOKEN_ENCODE_MODEL)

    # 加载query分类模型
    ckpt = torch.load('/home/tcl/rqa_dir/query_class_model/query_classifier1.pth',map_location=torch.device('cpu'))
    QUERY_CLASS_MODEL.load_state_dict(ckpt,strict=False)

    fkw = open('/home/tcl/rqa_dir/ext_dict2.dct')
    for line in fkw:
        PROFESSIONAL_DICT.add(line.strip('\n'))
    fkw.close()

    # 加载MEMORY
    global MEMORY_KEYWORD_MATCH
    global MEMORY_QUERY_MATCH

    MEMORY_QUERY_MATCH, MEMORY_KEYWORD_MATCH = read_keywords_with_score_from_memory(
        'config/memory_keywords_recall_v20230916.txt')

    # keyword提取
    global KW_ZH_MODEL
    global KW_MODEL
    global PORTER_STEMMER
    KW_ZH_MODEL = KeyBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
    KW_MODEL = KBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
    PORTER_STEMMER = PorterStemmer()

    print(f'✅ [Loading] Completed in {time.time() - start_time:.2f}s')
    return

def crontab_update_config():
    """实现动态加载配置文件中的参数"""
    config = configparser.ConfigParser()
    config.read('./config/search_srv_pipeline.ini', encoding='UTF-8')

    global RETRIEVE_CHUNK_NUM
    global SCORE_THREHOLD
    global DOC_SCORE_CHUNK_NUM
    global TOKEN_LIMIT
    global CONCAT_CHUNK_NUM

    DOC_SCORE_CHUNK_NUM = int(config['resort']['doc_score_chunk_num'])
    TOKEN_ENCODE_MODEL = config['concat']['token_encode_model']

    print(f'[Config Update] @{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    return

def check_query_relevance(query: str) -> bool:
    """检查query是否与半导体领域相关"""
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
    """召回 query 相关的文本切片（chunk）"""
    id: int = kwargs['id']
    query: str = kwargs['query']
    query_expand = kwargs['query_expand']
    query_embed_expand = kwargs['query_embed_expand']
    query_en: str = kwargs['query_en']
    query_embed: np.ndarray = kwargs['query_embed']
    result_dict: dict = kwargs['result_dict']
    query_embed_expand = [query_embed]
    
    ############# kw extraction
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

    print(f'[Recall] Keywords: zh={kw_zh}, en={kw_en}')

    ############# recall pipeline 1: sentence embedding -> chunk embedding
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
        
        # ⭐ Milvus 超时 × 10: (10, 120) → (100, 1200)
        res_json = HTTP_SESSION.post(url, verify=False, headers=headers, data=sent_data, 
                                     timeout=(100, 1200)).content.decode('utf-8')
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
    
    print(f'[Recall] Total recall: {len(result_dict)} chunks, max_score={recall_max_score:.3f}')

    ############# check query relevance
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

# ⭐ MongoDB 分批查询（超时 × 10）
@mongodb_retry(max_retries=3, initial_delay=20)
def query_embeddings_batch(mongo_collection, batch_conditions, batch_id, total_batches):
    """分批查询 embeddings，带自动重试（超时 × 10）"""
    if not batch_conditions:
        return []
    
    find_condition = {'$or': batch_conditions}
    
    print(f'[MongoDB Batch {batch_id}/{total_batches}] Querying {len(batch_conditions)} conditions...')
    batch_start = time.time()
    
    # ⭐ 服务器端超时 × 10: 60s → 600s
    score_iter = mongo_collection.find(
        find_condition,
        {'_id': 0, 'index': 1, 'doc_id': 1, 'embedding': 1}
    ).max_time_ms(600000)  # 600 秒
    
    results = list(score_iter)
    
    print(f'[MongoDB Batch {batch_id}/{total_batches}] ✅ Success: {len(results)} records in {time.time() - batch_start:.2f}s')
    return results

def rank_pipeline(**kwargs):
    """对文本切片做精排打分"""
    query: str = kwargs['query']
    query_embed: np.ndarray = kwargs['query_embed']
    query_rank_embed: np.ndarray = kwargs['query_rank_embed']
    result_dict: dict = kwargs['result_dict']
    
    search_index = [t['index'] for t in result_dict.values()]
    print(f'[Rank] Processing {len(search_index)} chunks')
    
    # 构建查询条件
    search_conditions = []
    for key in result_dict.keys():
        if isinstance(key, str) and '_' in key:
            index, doc_id = key.split('_')
            search_conditions.append({
                'index': int(index),
                'doc_id': int(doc_id)
            })
    
    print(f'[Rank] Total conditions: {len(search_conditions)}')
    
    search_start_time = time.time()
    
    # ⭐ 分批查询 embeddings
    embed_info = {}
    total_conditions = len(search_conditions)
    total_batches = (total_conditions + MONGO_BATCH_SIZE - 1) // MONGO_BATCH_SIZE
    
    print(f'[Rank] Batch query: total={total_conditions}, batch_size={MONGO_BATCH_SIZE}, batches={total_batches}')
    
    successful_batches = 0
    failed_batches = 0
    
    for i in range(0, total_conditions, MONGO_BATCH_SIZE):
        batch_id = i // MONGO_BATCH_SIZE + 1
        batch_conditions = search_conditions[i:i + MONGO_BATCH_SIZE]
        
        try:
            batch_results = query_embeddings_batch(
                MONGO_PIPELINE_RANK, 
                batch_conditions, 
                batch_id, 
                total_batches
            )
            
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
            print(f'[Rank] ❌ Batch {batch_id}/{total_batches} FAILED: {e}')
    
    print(f'[Rank] ✅ Batches: {successful_batches} success, {failed_batches} failed, {len(embed_info)} embeddings, {time.time() - search_start_time:.2f}s')
    
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

    print(f'[Rank] Completed in {time.time() - search_start_time:.2f}s')
    return kwargs

@http_retry(max_retries=3, initial_delay=10)
def encode_from_net(querys):
    """调用远程编码服务生成向量（超时 × 10）"""
    url=ENCODER_URL
    if isinstance(querys,list):
        payload = {"queries": querys}
    else:
        payload = {"queries": [querys]}

    headers = {"Content-Type": "application/json"}

    # ⭐ 编码超时 × 10: (10, 120) → (100, 1200)
    response = HTTP_SESSION.post(url, json=payload, headers=headers, timeout=(100, 1200))
    return response.json()['embeddings']

def rerank_pipeline(**kwargs):
    """
    重排 pipeline V4 - 超严格版本
    主要改进：
    1. 批次大小：12 → 8（更小，更稳定）
    2. 重试次数：2 → 3（更多重试）
    3. 超时时间：30+3×N → 300+30×N（× 10）
    4. 更详细的错误日志
    5. 更激进的分批策略
    """
    print('################## [Rerank V4] Starting rerank pipeline ##################')
    bge_server_url = RERANKER_URL
    bge_score_weight = 3.0 
    batch_size = 8  # ⭐ 减小批次：12 → 8

    query: str = kwargs['query']
    result_dict: dict = kwargs['result_dict']
    
    sorted_top_chunk = sorted(result_dict.items(), key=lambda d: -d[1]['final_score'])
    result_dict_sorted = {}
    bge_score_buff_dict = [[], []]
    
    batch_count = 0
    success_count = 0
    fail_count = 0
    partial_success_count = 0
    total_chunks_processed = 0
    
    def process_batch_with_retry(keys, pairs, retry_count=0, max_retries=3):  # ⭐ 重试：2 → 3
        """处理单个批次，带超严格重试和降级策略"""
        nonlocal total_chunks_processed
        
        try:
            bge_multi_data = {'type': 'multi', 'multi_data': pairs}
            
            # ⭐ 动态超时 × 10: 30+3×N → 300+30×N
            read_timeout = 300 + len(pairs) * 30  # 基础300秒 + 每对30秒
            
            print(f'[Rerank V4] Batch {batch_count}: size={len(pairs)}, timeout={read_timeout}s, retry={retry_count}/{max_retries}')
            
            response = HTTP_SESSION.post(
                bge_server_url, 
                data=json.dumps(bge_multi_data), 
                timeout=(150, read_timeout)  # 连接 × 10: 15s → 150s
            )
            
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}: {response.text[:100]}")
            
            bge_rerank_score_list = response.json()['score']
            
            # ⭐ 核心：严格检查 score 数量是否匹配
            expected_count = len(keys)
            actual_count = len(bge_rerank_score_list)
            
            if actual_count != expected_count:
                error_msg = f"Score count mismatch: expected {expected_count}, got {actual_count}"
                print(f'[Rerank V4] ❌ Batch {batch_count} {error_msg}')
                
                # ⭐ 策略1：如果还能重试，立即分批
                if retry_count < max_retries and len(keys) > 2:  # 最小分批为2
                    print(f'[Rerank V4] 📊 Splitting batch into smaller chunks (attempt {retry_count + 1})...')
                    
                    # 分成3份（更激进）
                    third = len(keys) // 3
                    if third < 2:
                        third = len(keys) // 2  # 至少分成2份
                    
                    parts = []
                    for i in range(0, len(keys), third):
                        end = min(i + third, len(keys))
                        if end > i:
                            parts.append((keys[i:end], pairs[i:end]))
                    
                    print(f'[Rerank V4] Split into {len(parts)} parts: sizes={[len(p[0]) for p in parts]}')
                    
                    # 递归处理每个部分
                    success_parts = 0
                    for part_keys, part_pairs in parts:
                        if process_batch_with_retry(part_keys, part_pairs, retry_count + 1, max_retries):
                            success_parts += 1
                    
                    return success_parts > 0  # 至少一个成功就算成功
                
                # ⭐ 策略2：如果是最后一次重试，尝试单个处理
                if retry_count == max_retries and len(keys) > 1:
                    print(f'[Rerank V4] 🔍 Last attempt: processing items one by one...')
                    success_items = 0
                    for i, (key, pair) in enumerate(zip(keys, pairs)):
                        try:
                            single_result = HTTP_SESSION.post(
                                bge_server_url,
                                data=json.dumps({'type': 'multi', 'multi_data': [pair]}),
                                timeout=(150, 600)  # 单个item超时600秒
                            )
                            if single_result.status_code == 200:
                                single_score = single_result.json()['score']
                                if len(single_score) == 1:
                                    score_value = single_score[0]
                                    if isinstance(score_value, list):
                                        score_value = score_value[0]
                                    rank_score = result_dict_sorted[key]['final_score'] + bge_score_weight * float(score_value)
                                    result_dict_sorted[key]['rerank_score'] = rank_score
                                    result_dict_sorted[key]['final_score'] = rank_score
                                    success_items += 1
                                    total_chunks_processed += 1
                        except Exception as e:
                            print(f'[Rerank V4] Item {i+1}/{len(keys)} failed: {str(e)[:50]}')
                    
                    print(f'[Rerank V4] One-by-one: {success_items}/{len(keys)} succeeded')
                    return success_items > 0
                
                print(f'[Rerank V4] ❌ Batch {batch_count} failed completely')
                return False
            
            # ⭐ 数量匹配，应用分数
            score_applied = 0
            for score_index in range(len(bge_rerank_score_list)):
                try:
                    score_value = bge_rerank_score_list[score_index]
                    # 处理可能的嵌套列表
                    if isinstance(score_value, list):
                        if len(score_value) > 0:
                            score_value = score_value[0]
                        else:
                            print(f'[Rerank V4] ⚠️ Empty score list at index {score_index}')
                            continue
                    
                    rank_score = result_dict_sorted[keys[score_index]]['final_score'] + \
                                 bge_score_weight * float(score_value)
                    result_dict_sorted[keys[score_index]]['rerank_score'] = rank_score
                    result_dict_sorted[keys[score_index]]['final_score'] = rank_score
                    score_applied += 1
                    total_chunks_processed += 1
                except (IndexError, ValueError, TypeError) as e:
                    print(f'[Rerank V4] ⚠️ Error at score_index={score_index}: {e}')
                    result_dict_sorted[keys[score_index]]['rerank_score'] = \
                        result_dict_sorted[keys[score_index]]['final_score']
            
            print(f'[Rerank V4] ✅ Batch {batch_count}: applied {score_applied}/{len(keys)} scores')
            return True
            
        except requests.exceptions.Timeout as e:
            # 超时错误：尝试重试或分批
            print(f'[Rerank V4] ⏰ Timeout: {type(e).__name__}')
            if retry_count < max_retries:
                if len(keys) > 3:
                    print(f'[Rerank V4] Splitting on timeout (retry {retry_count + 1})...')
                    mid = len(keys) // 2
                    success1 = process_batch_with_retry(keys[:mid], pairs[:mid], retry_count + 1, max_retries)
                    success2 = process_batch_with_retry(keys[mid:], pairs[mid:], retry_count + 1, max_retries)
                    return success1 or success2
                else:
                    wait_time = 10 * (retry_count + 1)  # 等待时间 × 10
                    print(f'[Rerank V4] Retrying after {wait_time}s (attempt {retry_count + 1})...')
                    time.sleep(wait_time)
                    return process_batch_with_retry(keys, pairs, retry_count + 1, max_retries)
            
            print(f'[Rerank V4] ❌ Timeout after all retries')
            return False
            
        except Exception as e:
            # 其他错误：重试
            error_msg = f"{type(e).__name__}: {str(e)[:100]}"
            print(f'[Rerank V4] ❌ Error: {error_msg}')
            
            if retry_count < max_retries:
                wait_time = 5 * (retry_count + 1)
                print(f'[Rerank V4] Retrying after {wait_time}s...')
                time.sleep(wait_time)
                return process_batch_with_retry(keys, pairs, retry_count + 1, max_retries)
            
            return False
        
    # 主处理循环
    for index in range(len(sorted_top_chunk)):
        combined_key = sorted_top_chunk[index][0]
        chunk_data = sorted_top_chunk[index][1]
        
        if 'shard' not in chunk_data:
            continue
            
        result_dict_sorted[combined_key] = chunk_data
        
        if index < 300:
            # ⭐ 文本预处理：更严格的清理
            shard_text = chunk_data['shard']
            
            # 1. 清理特殊字符
            shard_text = shard_text.replace('\x00', '').replace('\r', '').replace('\t', ' ')
            shard_text = re.sub(r'\s+', ' ', shard_text)  # 多个空白替换为单个空格
            
            # 2. 智能截断（在句子边界）
            if len(shard_text) > 800:  # ⭐ 更保守：1000 → 800
                truncated = shard_text[:800]
                for punct in ['。', '！', '？', '.', '!', '?', '\n', '；', ';']:
                    last_idx = truncated.rfind(punct)
                    if last_idx > 600:  # 确保有足够内容
                        truncated = truncated[:last_idx + 1]
                        break
                shard_text = truncated
            
            # 3. 最终清理
            shard_text = shard_text.strip()
            if not shard_text or len(shard_text) < 10:  # 至少10个字符
                continue
            
            bge_score_buff_dict[0].append(combined_key)
            bge_score_buff_dict[1].append([query, shard_text])
            
            if len(bge_score_buff_dict[0]) >= batch_size:
                batch_count += 1
                print(f"\n[Rerank V4] ═══ Processing batch {batch_count}, size: {len(bge_score_buff_dict[0])} ═══")
                
                if process_batch_with_retry(bge_score_buff_dict[0], bge_score_buff_dict[1]):
                    success_count += 1
                else:
                    fail_count += 1
                    # 失败降级：使用原始分数
                    for combined_key in bge_score_buff_dict[0]:
                        if combined_key in result_dict_sorted:
                            result_dict_sorted[combined_key]['rerank_score'] = \
                                result_dict_sorted[combined_key]['final_score']
                
                bge_score_buff_dict = [[], []]
        else:
            # 对于排名300之后的，使用默认惩罚分数
            bge_rerank_score = -8.0
            rank_score = result_dict_sorted[combined_key]['final_score'] + bge_score_weight + bge_rerank_score
            result_dict_sorted[combined_key]['rerank_score'] = rank_score
            result_dict_sorted[combined_key]['final_score'] = rank_score

    # 处理最后一个不满批次
    if len(bge_score_buff_dict[0]) > 0:
        batch_count += 1
        print(f"\n[Rerank V4] ═══ Processing FINAL batch {batch_count}, size: {len(bge_score_buff_dict[0])} ═══")
        
        if process_batch_with_retry(bge_score_buff_dict[0], bge_score_buff_dict[1]):
            success_count += 1
        else:
            fail_count += 1
            for combined_key in bge_score_buff_dict[0]:
                if combined_key in result_dict_sorted:
                    result_dict_sorted[combined_key]['rerank_score'] = \
                        result_dict_sorted[combined_key]['final_score']
        
        bge_score_buff_dict = [[], []]
    
    # 最终统计
    success_rate = (success_count / batch_count * 100) if batch_count > 0 else 0
    print(f'\n[Rerank V4] ═══════════════════════════════════════════════════')
    print(f'[Rerank V4] 📊 FINAL STATS:')
    print(f'[Rerank V4]    Total batches: {batch_count}')
    print(f'[Rerank V4]    ✅ Success: {success_count} ({success_rate:.1f}%)')
    print(f'[Rerank V4]    ❌ Failed: {fail_count}')
    print(f'[Rerank V4]    📝 Chunks processed: {total_chunks_processed}')
    print(f'[Rerank V4] ═══════════════════════════════════════════════════\n')
             
    result_dict = result_dict_sorted

    # query kw 匹配调权
    kw_zh = kwargs['kw_zh']
    kw_en = kwargs['kw_en']

    kw_num, weight_num = len(kw_zh), 0
    
    for (combined_key, row) in result_dict.items():
        match_cnt = 0
        if row.get('final_score', 0) < 0.4 or 'shard' not in row:
            continue
        weight_num += 1
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

    print(f'[Rerank V4] Keyword weight applied to {weight_num} chunks')
    kwargs['result_dict'] = result_dict
    return kwargs

def concat_shards_by_rank(**kwargs):
    """拼接 chunks"""
    query = kwargs['query']
    query_expand = kwargs['query_expand']
    result_dict = kwargs['result_dict']
    top_doc_num: int = kwargs['top_doc_num']
    is_debug: bool = kwargs['is_debug']
    target_doc_name = kwargs['target_doc_name']

    sorted_top_chunk = sorted(list(result_dict.values()), key=lambda d: -d['final_score'])
    
    start_time = time.time()

    simMatrix = {}
    if top_doc_num <= 5:
        sim_num = 5 * 5 + 30
    else:
        sim_num = top_doc_num * 5 + 30
    
    for i in range(min(sim_num, len(sorted_top_chunk))):
        info_i = sorted_top_chunk[i]
        index_i = info_i['index']
        
        for j in range(i + 1, min(sim_num, len(sorted_top_chunk))):
            info_j = sorted_top_chunk[j]
            index_j = info_j['index']
            bigger, smaller = max(index_i, index_j), min(index_i, index_j)
    
    print(f'[Concat] Similarity matrix time={time.time()-start_time:.2f}s')

    selectedDocs = []
    
    sim_num = min(sim_num, len(sorted_top_chunk))
    for i in range(sim_num):
        mmrStep(0.9, selectedDocs, sorted_top_chunk, simMatrix)
    
    print(f'[Concat] MMR time={time.time() - start_time:.2f}s')

    res_arr_dict = {}
    res_arr = []
    res_num = 0

    min_concat_score = 99.9
    min_concat_id = -1

    i = 0
    this_chunk_num = 0
    this_index = []
    this_res = {}
    this_docs = []
    this_doc_ids = []
    this_shard = []
    this_title = []
    this_num_token = 0
    this_scores = 0.0
    chunks_id = 0

    for selected_index in selectedDocs:
        i += 1
        
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
            concat_score = 0.5
            this_res['text'] = this_shard
            this_res['concat_score'] = concat_score
            if is_debug:
                print(f'[Concat] chunks_id={chunks_id}, score={concat_score}')
            if concat_score < min_concat_score:
                min_concat_score = concat_score
                min_concat_id = chunks_id
            
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
            chunks_id += 1
            res_num += 1
        
        if res_num >= top_doc_num:
            break
    
    sorted_res_arr_dict = sorted(res_arr_dict.items(), key=lambda d: -d[0][0]) 
    for item_index, sorted_item in enumerate(sorted_res_arr_dict):
        sorted_item[1]['ans_id'] = item_index
        res_arr.append(sorted_item[1])
    
    print(f'[Concat] Generated {len(res_arr)} result groups')

    return res_arr

@app.route('/api-rqa-search/test', methods=['GET'])
def hello_world():
    return json_result(0, '', f'V4 Service Available - 10× Timeout Config')

@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    """搜索服务主函数 V4"""
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
        
        # 使用带重试的编码服务
        query_embed = encode_from_net(query)
        query_rank_embed = encode_from_net(query)

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
        query_embed = encode_from_net(query)
        query_embed_expand = encode_from_net(query_expand)
        query_rank_embed_expand = encode_from_net(query_expand)

        params['query_embed'] = query_embed
        params['query_embed_expand'] = query_embed_expand
        params['query_rank_embed'] = query_rank_embed
        params['query_rank_embed_expand'] = query_rank_embed_expand
        params['target_doc_name'] = target_doc_name
        params['flag_query_rel'] = True
        
        if len(query_en.strip()) > 0:
            params['query_en'] = query_en
        params['result_dict'] = result_dict
        
        # 多路召回
        params = recall_pipeline(**params)
        print(f'[Main] Recall: {time.time() - start_time:.2f}s')
        
        if not params['flag_query_rel']:
            code = 1
            data['msg'] = 'query must be relevant'
            data['doc_num'] = 0
            data['arr'] = []
            return json_result(code, msg, data)

        # 排序与重排
        params = rank_pipeline(**params)
        print(f'[Main] Rank: {time.time() - start_time:.2f}s')
        params = rerank_pipeline(**params)
        print(f'[Main] Rerank: {time.time() - start_time:.2f}s')
        
        params['top_doc_num'] = top_doc_num
        params['concat_num'] = CONCAT_CHUNK_NUM
        params['is_debug'] = is_debug

        # 结果拼接与返回
        similar_shards = concat_shards_by_rank(**params)
        high_scores = []
        for dict in similar_shards:
            score = dict['score']
            if score < SCORE_THREHOLD:
                break
            high_scores.append(score)
            json_arr.append(dict)
        
        data['arr'] = json_arr
        data['doc_num'] = len(json_arr)

    except Exception as e:
        code = -1
        msg = traceback.format_exc()
        data['msg'] = msg
        data['doc_num'] = 0
        data['arr'] = []
        print(f'[ERROR] Exception:\n{msg}')

    now = datetime.datetime.now()
    data['ts'] = int(datetime.datetime.timestamp(now) * 1000)
    total_time = time.time() - start_time
    print(f'[Main] ⏱️ Total time: {total_time:.2f}s')
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

faulthandler.enable()

print(f'═══════════════════════════════════════════════════════════')
print(f'🚀 [Server V4] Starting with 10× TIMEOUT configuration')
print(f'═══════════════════════════════════════════════════════════')
print(f'   Version: {VERSION}')
print(f'   Batch Size: 8 (rerank)')
print(f'   Max Retries: 3')
print(f'   Timeouts:')
print(f'     - MongoDB: 600-1200s (× 10)')
print(f'     - HTTP: 100-1200s (× 10)')
print(f'     - Rerank: 300+30×N s (× 10)')
print(f'     - Encoder: (100, 1200)s (× 10)')
print(f'     - Milvus: (100, 1200)s (× 10)')
print(f'═══════════════════════════════════════════════════════════\n')

# 服务启动参数填充
config = configparser.ConfigParser()
config.read('./config/search_srv_pipeline_l.ini', encoding='UTF-8')

scheduler = BackgroundScheduler()
scheduler.add_job(crontab_update_config, 'interval', seconds=180, coalesce=True, replace_existing=True)
scheduler.start()

load_data(TABLE_NAME, MODEL_NAME)

if __name__ == '__main__':
    print(f'\n✅ [Server V4] Ready to serve!')
    print(f'   Listen: 10.70.223.31:9510')
    print(f'   HTTP Pool: {HTTP_ADAPTER.pool_maxsize} connections')
    print(f'   MongoDB Batch: {MONGO_BATCH_SIZE}\n')
    app.run('10.70.223.31', port=9510, threaded=True, processes=1)
