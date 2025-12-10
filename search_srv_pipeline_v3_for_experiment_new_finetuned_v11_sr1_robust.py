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

# ==================== 新增：重试装饰器 ====================
def mongodb_retry(max_retries=3, initial_delay=2):
    """MongoDB 查询重试装饰器，支持指数退避"""
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
                    delay = initial_delay * (2 ** attempt)  # 指数退避: 2s, 4s, 8s
                    print(f'[MongoDB Retry] Attempt {attempt + 1}/{max_retries} failed: {e}, retrying in {delay}s...')
                    time.sleep(delay)
            return None
        return wrapper
    return decorator

def http_retry(max_retries=3, initial_delay=1, timeout=(10, 60)):
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

def zhipu_translate(id: int, text: str, from_lang: str, to_lang: str):
    """
    调用质谱api中译英
    """
    # 质谱翻译API HTTP地址
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
        # 线程安全：使用全局Session，添加超时保护
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
        # ✅ 超级健壮配置：大幅增加所有超时时间和连接池
        self.client = pymongo.MongoClient(
            self.url, 
            maxPoolSize=100,              # 最大连接池
            minPoolSize=20,               # 最小连接池
            maxIdleTimeMS=120000,         # 空闲时间 120 秒
            connectTimeoutMS=60000,       # 连接超时 60 秒
            socketTimeoutMS=120000,       # ⭐ Socket 超时 120 秒
            serverSelectionTimeoutMS=60000,  # 服务器选择超时 60 秒
            retryReads=True,              # 启用读重试
            retryWrites=True,             # 启用写重试
            waitQueueTimeoutMS=30000,     # 等待连接超时 30 秒
        )
        self.db = self.client[self.db_name]
        print(f'db {self.db} collections={self.db.list_collection_names()}')
        self.collection = self.db[self.table_name]
        print(f'[MongoDB] Connected with pool size: max={self.client.max_pool_size}, min={self.client.min_pool_size}')
    
    def insert_one(self, data):
        result = self.collection.insert_one(data)
        return

    def find_data(self, conditions):
        return self.collection.find(conditions)

sys.path.append("..")

from flask import Flask, request, Response, jsonify

# embedding 模型
# MODEL_NAME = '/mnt/hdd1/haoyangliu/em_model/embedding_res'
MODEL_NAME = '/mnt/hdd1/haoyangliu/em_model/bge-multilingual-gemma2'
# query class 模型
QUERY_CLASS_MODEL = BERTClassifier('/home/tcl/rqa_dir/query_class_model/bert-base-chinese', 2).to('cpu')
QUERY_CLASS_TOKENIZER = BertTokenizer.from_pretrained('/home/tcl/rqa_dir/query_class_model/bert-base-chinese')
SOFTMAX = nn.Softmax(dim=0)

PROFESSIONAL_DICT = set()

# 连接mongo数据库相关的变量
MONGO_URL = 'mongodb://root:example@10.70.223.31:27017'
MONGO_DB = 'rqa'
# MONGO_URL = 'mongodb://root:example@10.70.223.112:27017'
# MONGO_DB = 'scrapy'
#VERSION = '20230908'
VERSION = '2023091110'


# TABLE_NAME = "test_table_lhy"

# 加载MongoDB的相关组件
MONGO_PIPELINE = None
MONGO_PIPELINE_RANK = None
DOWNLOAD_PIPELINE = None

# 加载向量库支持向量检索相关的变量
ENCODING = tiktoken.encoding_for_model("gpt-3.5-turbo")

# 加载ES相关组件
ES = Elasticsearch('http://10.70.222.234:9200')


#核心组件

# Milvus 向量检索服务
# jcf: 替换为 vector_search_srv_v4.py 中自己配置的 milvus 服务地址和端口
# MILVUS_BIND ='http://8.130.143.163:8021/api-vec-search/search' 
MILVUS_BIND = 'http://127.0.0.1:4100/api-vec-search/search'
# MILVUS_BIND = 'http://8.130.143.163:8022/api-vec-search/search'
MONGODB_C_NAME = "paper_shards_detail_table_20230908" 
# ENCODER_URL="http://8.130.143.163:8021/encode"
# ENCODER_URL="http://8.130.143.163:8022/encode"
ENCODER_URL="http://8.130.183.20:8031/encode"
# RERANKER_URL='http://8.130.158.117:8016/query_qwen_reranker/'
# RERANKER_URL='http://8.130.143.163:8023/query_bge_reranker/'
RERANKER_URL = 'http://8.130.183.20:8032/query_bge_reranker/'

TABLE_NAME = MONGODB_C_NAME

# recall
RETRIEVE_CHUNK_NUM = 4000
ES_RETRIEVE_CHUNK_NUM = 400

# rank
DOC_SCORE_CHUNK_NUM = 8
SCORE_THREHOLD = -2 # -2  # score是检索embed相似度阈值，低于阈值不展示
# SCORE_THREHOLD = 3.5 
TOKEN_LIMIT = 4000
TOKEN_ENCODE_MODEL = 'gpt-3.5-turbo'
CONCAT_CHUNK_NUM = 4

# ⭐ 新增：分批查询配置
MONGO_BATCH_SIZE = 500  # 每批查询 500 条

# memory keyword match
MEMORY_KEYWORD_MATCH = {}
MEMORY_QUERY_MATCH = {}

# keyword extract model
KW_ZH_MODEL = KeyBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')
KW_MODEL = KBERT(model='/mnt/hdd1/haoyangliu/em_model/kw/paraphrase-multilingual-MiniLM-L12-v2')

# nlp相关组件
PORTER_STEMMER = PorterStemmer() # 词干提取
REGEX_PATTERN = '|'.join(map(re.escape, [',', '\n', ';', '!', '?', '.', ' ', '~']))

# 线程安全：全局HTTP Session对象，支持连接池复用
HTTP_SESSION = requests.Session()
HTTP_ADAPTER = requests.adapters.HTTPAdapter(
    pool_connections=100,  # ⬆️ 增加连接池数量
    pool_maxsize=200,      # ⬆️ 增加连接池最大连接数
    max_retries=3,         # 失败重试次数
    pool_block=False       # 连接池满时不阻塞
)
HTTP_SESSION.mount('http://', HTTP_ADAPTER)
HTTP_SESSION.mount('https://', HTTP_ADAPTER)

# 线程安全：BERT模型推理锁
BERT_LOCK = threading.Lock()

# 线程安全：Jieba分词锁（jieba不是线程安全的）
JIEBA_LOCK = threading.Lock()

app = Flask(import_name=__name__)
app.config['JSON_AS_ASCII'] = False

def has_chinese(text):
    pattern = re.compile(r'[\u4e00-\u9fff]')  # 匹配中文字符的正则表达式
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
    """
    lambda_param: to balance diversity && relevance
    """
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
    # 加载需要在serve中持久化的item
    print('loading global data')
    start_time = time.time()

    global ENCODING
    global MONGO_PIPELINE
    global MONGO_PIPELINE_RANK
    global QUERY_CLASS_MODEL
    global QUERY_CLASS_TOKENIZER
    global PROFESSIONAL_DICT

    # ✅ 超级健壮配置：MongoDB连接
    MONGO_PIPELINE = mongodb(MONGO_URL, MONGO_DB, table)
    MONGO_PIPELINE.connect()
    
    # ✅ 创建 MONGO_PIPELINE_RANK 时使用超级健壮配置
    client = pymongo.MongoClient(
        "mongodb://root:example@10.70.223.31:27017", 
        maxPoolSize=100,              # 最大连接池
        minPoolSize=20,               # 最小连接池
        maxIdleTimeMS=120000,         # 空闲时间 120 秒
        connectTimeoutMS=60000,       # 连接超时 60 秒
        socketTimeoutMS=120000,       # ⭐ Socket 超时 120 秒
        serverSelectionTimeoutMS=60000,  # 服务器选择超时 60 秒
        retryReads=True,              # 启用读重试
        retryWrites=True,             # 启用写重试
        waitQueueTimeoutMS=30000,     # 等待连接超时 30 秒
    )
    db = client[MONGO_DB]
    MONGO_PIPELINE_RANK = db[MONGODB_C_NAME]
    
    print(f'[load_data] MongoDB connected: pool_size={client.max_pool_size}, socket_timeout={client.options.socket_timeout}ms')

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

    print(f'load global data from mongo=[{table}] spent_time={time.time() - start_time}')
    return

def crontab_update_config():
    """
    实现动态加载配置文件中的参数
    """
    config = configparser.ConfigParser()
    config.read('./config/search_srv_pipeline.ini', encoding='UTF-8')

    global RETRIEVE_CHUNK_NUM
    global SCORE_THREHOLD
    global DOC_SCORE_CHUNK_NUM
    global TOKEN_LIMIT
    global CONCAT_CHUNK_NUM

    DOC_SCORE_CHUNK_NUM = int(config['resort']['doc_score_chunk_num'])
    TOKEN_ENCODE_MODEL = config['concat']['token_encode_model']

    print(
        f'[update config] 执行定时任务(minute=*/3)@{datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}：{__name__}; Reading TOKEN_LIMIT={TOKEN_LIMIT}, SCORE_THREHOLD={SCORE_THREHOLD}, RETRIEVE_CHUNK_NUM={RETRIEVE_CHUNK_NUM}')
    return

def check_query_relevance(query: str) -> bool:
    """
    check query is relevant to semiconductor field
    """
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
    
    print(f'[check_query_relevance] Model score: {model_score}, text=[{query}]')
    return flag_query_rel

def recall_pipeline(**kwargs):
    """
    召回 query 相关的文本切片（chunk）
    """
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

    print(f'keyBert kw=[{kw0}],keywords=[{kw_zh}] get keywords in Eng=[{kw_en}]')

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
        
        # ⭐ 增加 Milvus 请求超时时间
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
        
    print(f'[Recall Pipeline] after embed query chunks num = {res_vec_num}')
    
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
    
    print(f'[Recall Pipeline] finally milvus search recall num = {len(result_dict)}')

    ############# check query relevance
    key_match_cnt = len(PROFESSIONAL_DICT.intersection(set(kw_zh)))
    print(f'[Recall Pipeline] find kw_set={kw_zh},key_match_cnt={key_match_cnt},recall_max_score={recall_max_score}')
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

    ############# recall pipeline 2: keyword embedding -> chunk match
    kw0 = None
    if kw0:
        url_kw = f'http://10.70.222.234:5200/api-kw-search/search'
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Content-Length': '<calculated when request is sent>',
            'Accept-Encoding': 'gzip, deflate, br'
        }
        sent_data_kw = {'topk': 128,
                        'kw': ','.join(kw0)}
        res_json_kw = HTTP_SESSION.post(url_kw, verify=False, headers=headers, data=sent_data_kw,
                                    timeout=(10, 30)).content.decode('utf-8')
        res_data_kw = json.loads(res_json_kw).get('data')
        res_vec_num_kw = res_data_kw.get('vec_num', 0)
        res_arr_kw = [t.split(':') for t in res_data_kw.get('arr')]
        kw_embed_cnt = 0
        for i in range(res_vec_num_kw):
            dct = {}
            [id_s, score_s] = res_arr_kw[i]
            dct['index'] = int(id_s)
            dct['recall_score'] = 0.0
            dct['final_score'] = -1.0
            dct['doc_name'] = 'null'
            if int(id_s) in result_dict:
                result_dict[int(id_s)].get('recall_channels', set(['sentence_embed'])).add('kw_embed')
            else:
                dct['recall_channels'] = set(['kw_embed'])
                kw_embed_cnt += 1
            result_dict[int(id_s)] = dct
        print(f'[Recall Pipeline] keyword embed recall num ={kw_embed_cnt}')

    es_cnt = 0
    print(f'[Recall Pipeline] es BM25 recall num = [{es_cnt}] (ES search disabled)')

    kwargs['result_dict'] = result_dict
    print(f"final recall chunk num = [{len(result_dict)}], es_cnt={es_cnt}")
    return kwargs

# ⭐⭐⭐ 核心修改：分批查询 MongoDB，带重试机制
@mongodb_retry(max_retries=3, initial_delay=2)
def query_embeddings_batch(mongo_collection, batch_conditions, batch_id, total_batches):
    """
    分批查询 embeddings，带自动重试
    Args:
        mongo_collection: MongoDB collection 对象
        batch_conditions: 本批次的查询条件列表
        batch_id: 当前批次ID（用于日志）
        total_batches: 总批次数（用于日志）
    Returns:
        list: 查询结果列表
    """
    if not batch_conditions:
        return []
    
    find_condition = {'$or': batch_conditions}
    
    print(f'[MongoDB Batch {batch_id}/{total_batches}] Querying {len(batch_conditions)} conditions...')
    batch_start = time.time()
    
    # ⭐ 设置服务器端超时 60 秒
    score_iter = mongo_collection.find(
        find_condition,
        {'_id': 0, 'index': 1, 'doc_id': 1, 'embedding': 1}
    ).max_time_ms(60000)
    
    # 立即转为列表
    results = list(score_iter)
    
    print(f'[MongoDB Batch {batch_id}/{total_batches}] ✅ Success: fetched {len(results)} records in {time.time() - batch_start:.2f}s')
    return results

def rank_pipeline(**kwargs):
    """
    对文本切片做精排打分 - 使用分批查询 + 重试机制
    """
    query: str = kwargs['query']
    query_embed: np.ndarray = kwargs['query_embed']
    query_rank_embed: np.ndarray = kwargs['query_rank_embed']
    result_dict: dict = kwargs['result_dict']
    
    search_index = [t['index'] for t in result_dict.values()]
    print(f'search_index len={len(search_index)} example={search_index[:100]}, search mongo version = {VERSION}')
    
    # 构建查询条件
    search_conditions = []
    for key in result_dict.keys():
        if isinstance(key, str) and '_' in key:
            index, doc_id = key.split('_')
            search_conditions.append({
                'index': int(index),
                'doc_id': int(doc_id)
            })
    
    print(f'search_conditions len={len(search_conditions)}, example={search_conditions[:5]}')
    
    search_start_time = time.time()
    
    # ⭐⭐⭐ 核心改进：分批查询 embeddings
    embed_info = {}
    total_conditions = len(search_conditions)
    total_batches = (total_conditions + MONGO_BATCH_SIZE - 1) // MONGO_BATCH_SIZE  # 向上取整
    
    print(f'[rank_pipeline] 🚀 Starting batch query: total={total_conditions}, batch_size={MONGO_BATCH_SIZE}, batches={total_batches}')
    
    successful_batches = 0
    failed_batches = 0
    
    for i in range(0, total_conditions, MONGO_BATCH_SIZE):
        batch_id = i // MONGO_BATCH_SIZE + 1
        batch_conditions = search_conditions[i:i + MONGO_BATCH_SIZE]
        
        try:
            # 调用带重试的批量查询
            batch_results = query_embeddings_batch(
                MONGO_PIPELINE_RANK, 
                batch_conditions, 
                batch_id, 
                total_batches
            )
            
            # 处理本批次结果
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
            print(f'[rank_pipeline] ❌ Batch {batch_id}/{total_batches} FAILED after retries: {e}')
            # 继续处理下一批，不中断整个流程
    
    print(f'[rank_pipeline] ✅ Batch query completed: success={successful_batches}/{total_batches}, failed={failed_batches}, embed_info_cnt={len(embed_info)}, time={time.time() - search_start_time:.2f}s')
    
    # search 请求切片、doc_name数据
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

    if cnt < RETRIEVE_CHUNK_NUM:
        print(f'mongodb err detect mongo num = {cnt} expect: {RETRIEVE_CHUNK_NUM}')

    ############# keyword -> doc_id match
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
        cnt2, cnt_final = 0, 0

        # 同时补充score的m3e embed - 也使用分批查询
        score_conditions2 = [{'doc_id': doc_id} for doc_id in keyword_docid.keys()]
        total_conditions2 = len(score_conditions2)
        total_batches2 = (total_conditions2 + MONGO_BATCH_SIZE - 1) // MONGO_BATCH_SIZE
        
        for i in range(0, total_conditions2, MONGO_BATCH_SIZE):
            batch_id = i // MONGO_BATCH_SIZE + 1
            batch_conditions2 = score_conditions2[i:i + MONGO_BATCH_SIZE]
            
            try:
                batch_results2 = query_embeddings_batch(
                    MONGO_PIPELINE_RANK,
                    batch_conditions2,
                    batch_id,
                    total_batches2
                )
                for r in batch_results2:
                    embed_info[int(r['index'])] = r['embedding']
            except Exception as e:
                print(f'[rank_pipeline] Keyword boost batch {batch_id} failed: {e}')
        
        print(f'after boosting by kw embed_info cnt={len(embed_info)}')

        docid_index_dict = {}
        weighted_doc_info = {}
        for dct in data_iter2:
            doc_id = int(dct['doc_id'])
            index = dct['index']
            if index in result_dict:
                result_dict[index]['final_score'] = result_dict[index]['final_score'] * keyword_docid[doc_id]
            shard = dct['shard']
            if len(shard) < 50:
                continue
            shard_id = dct['index']
            doc_name = dct['doc_name']
            if doc_id not in weighted_doc_info:
                weighted_doc_info[doc_id] = (doc_name, keyword_docid[doc_id])
            shard = re.sub(r"\[[0-9].{0,5}\]", "", shard)

            row = {}
            rank_score = 0
            if index in embed_info:
                query_rank_embed = query_rank_embed.to(torch.float32)
                rank_embed = np.array([float(x) for x in embed_info[index].split(',')])
                rank_embed = torch.tensor(rank_embed).to(torch.float32)
                rank_score = util.cos_sim(query_rank_embed, rank_embed).item()
                row['rank_embed'] = rank_embed
            row['index'] = index
            row['recall_score'] = 0.0
            row['rank_score'] = rank_score
            row['final_score'] = rank_score * keyword_docid[doc_id]
            row['doc_name'] = doc_name
            row['title']=title
            row['doc_id'] = doc_id
            row['shard'] = shard
            row['index'] = shard_id

            if doc_id not in docid_index_dict:
                docid_index_dict[doc_id] = [row]
            else:
                docid_index_dict[doc_id].append(row)
            cnt2 += 1

        print(f'weighted_doc_info = {weighted_doc_info}')
        for (doc_id, rows) in docid_index_dict.items():
            doc_top_rows = sorted(rows, key=lambda d: -d['final_score'])[:CONCAT_CHUNK_NUM * 2]
            for row in doc_top_rows:
                result_dict[row['index']] = row
                cnt_final += 1
                print(f"add doc id=[{doc_id}],name=[{row['doc_name']}],weight=[{keyword_docid.get(doc_id, 1.0)}],rank_score=[{row['rank_score']}], final_score=[{row['final_score']}]")

        print(f'Adding result: new cnt = {cnt2}, final adding cnt = {cnt_final}, adding files = {keyword_docid}')

    print(f'search on mongo spent time = {time.time() - search_start_time}')
    return kwargs

@http_retry(max_retries=3, initial_delay=1)
def encode_from_net(querys):
    """
    调用远程编码服务生成向量 - 带重试机制
    """
    url=ENCODER_URL
    if isinstance(querys,list):
        payload = {
        "queries": querys
        }
    else:
        payload = {
        "queries": [querys]
        }

    headers = {
        "Content-Type": "application/json"
    }

    # ⭐ 增加超时时间：连接 10s，读取 120s
    response = HTTP_SESSION.post(url, json=payload, headers=headers, timeout=(10, 120))
    return response.json()['embeddings']

def rerank_pipeline(**kwargs):
    """重排 pipeline（增强版 - 修复 score count mismatch 和 timeout 问题）"""
    print('################## process rerank pipeline ##################')
    bge_server_url = RERANKER_URL
    bge_score_weight = 3.0 
    batch_size = 12  # 减小批次大小：20 -> 12，降低服务端压力和超时风险

    query: str = kwargs['query']
    result_dict: dict = kwargs['result_dict']
    
    sorted_top_chunk = sorted(result_dict.items(), key=lambda d: -d[1]['final_score'])
    result_dict_sorted = {}
    bge_score_buff_dict = [[], []]
    
    batch_count = 0
    success_count = 0
    fail_count = 0
    
    def process_batch_with_retry(keys, pairs, retry_count=0, max_retries=2):
        """处理单个批次，带智能重试和降级策略"""
        try:
            bge_multi_data = {'type': 'multi', 'multi_data': pairs}
            
            # 动态超时：基础30秒 + 每对3秒
            read_timeout = 30 + len(pairs) * 3
            
            response = HTTP_SESSION.post(
                bge_server_url, 
                data=json.dumps(bge_multi_data), 
                timeout=(15, read_timeout)
            )
            
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}")
            
            bge_rerank_score_list = response.json()['score']
            
            # ⭐⭐⭐ 智能修复：处理 score 数量不匹配
            expected_count = len(keys)
            actual_count = len(bge_rerank_score_list)
            
            if actual_count != expected_count:
                error_msg = f"Score count mismatch: expected {expected_count}, got {actual_count}"
                print(f'[Rerank] Batch {batch_count} {error_msg}')
                
                # ⭐ 智能修复策略：直接调整 score 列表
                if actual_count > expected_count:
                    # 情况1：多了 → 取前 N 个
                    bge_rerank_score_list = bge_rerank_score_list[:expected_count]
                    print(f'[Rerank] 🔧 Auto-fix: trimmed {actual_count} → {expected_count}')
                    
                elif actual_count < expected_count:
                    # 情况2：少了 → 补充默认低分
                    missing_count = expected_count - actual_count
                    default_score = -10.0
                    bge_rerank_score_list.extend([default_score] * missing_count)
                    print(f'[Rerank] 🔧 Auto-fix: padded {missing_count} scores with {default_score}')
                
                print(f'[Rerank] ✅ Fixed: {len(bge_rerank_score_list)} == {expected_count}')
            
            # 验证修复（双重保险）
            if len(bge_rerank_score_list) != expected_count:
                print(f'[Rerank] ❌ Fix failed, trying split...')
                if retry_count < max_retries and len(keys) > 3:
                    mid = len(keys) // 2
                    success1 = process_batch_with_retry(keys[:mid], pairs[:mid], retry_count + 1, max_retries)
                    success2 = process_batch_with_retry(keys[mid:], pairs[mid:], retry_count + 1, max_retries)
                    return success1 or success2
                return False
            
            # 应用分数
            for score_index in range(len(bge_rerank_score_list)):
                try:
                    score_value = bge_rerank_score_list[score_index]
                    # 处理可能的嵌套列表
                    if isinstance(score_value, list):
                        score_value = score_value[0]
                    
                    rank_score = result_dict_sorted[keys[score_index]]['final_score'] + \
                                 bge_score_weight * float(score_value)
                    result_dict_sorted[keys[score_index]]['rerank_score'] = rank_score
                    result_dict_sorted[keys[score_index]]['final_score'] = rank_score
                except (IndexError, ValueError, TypeError) as e:
                    print(f'[Rerank] Error applying score at index {score_index}: {e}')
                    result_dict_sorted[keys[score_index]]['rerank_score'] = \
                        result_dict_sorted[keys[score_index]]['final_score']
            
            return True
            
        except requests.exceptions.Timeout as e:
            # 超时错误：尝试重试或分批
            if retry_count < max_retries:
                if len(keys) > 5:
                    print(f'[Rerank] Batch {batch_count} timeout, splitting batch (retry {retry_count + 1})...')
                    mid = len(keys) // 2
                    success1 = process_batch_with_retry(keys[:mid], pairs[:mid], retry_count + 1, max_retries)
                    success2 = process_batch_with_retry(keys[mid:], pairs[mid:], retry_count + 1, max_retries)
                    return success1 or success2
                else:
                    print(f'[Rerank] Batch {batch_count} timeout, retrying (attempt {retry_count + 1})...')
                    time.sleep(1 * (retry_count + 1))
                    return process_batch_with_retry(keys, pairs, retry_count + 1, max_retries)
            
            print(f'[Rerank] Batch {batch_count} error: {type(e).__name__}')
            return False
            
        except Exception as e:
            # 其他错误：重试
            if retry_count < max_retries:
                print(f'[Rerank] Batch {batch_count} error: {str(e)[:100]}, retrying...')
                time.sleep(0.5 * (retry_count + 1))
                return process_batch_with_retry(keys, pairs, retry_count + 1, max_retries)
            
            print(f'[Rerank] Batch {batch_count} error: {str(e)[:100]}')
            return False
        
    for index in range(len(sorted_top_chunk)):
        combined_key = sorted_top_chunk[index][0]
        chunk_data = sorted_top_chunk[index][1]
        
        if 'shard' not in chunk_data:
            continue
            
        result_dict_sorted[combined_key] = chunk_data
        
        if index < 300:
            # 文本预处理：清理特殊字符，智能截断
            shard_text = chunk_data['shard']
            if len(shard_text) > 1000:
                # 在句子边界截断
                truncated = shard_text[:1000]
                for punct in ['。', '！', '？', '.', '!', '?', '\n']:
                    last_idx = truncated.rfind(punct)
                    if last_idx > 700:
                        truncated = truncated[:last_idx + 1]
                        break
                shard_text = truncated
            
            # 移除空字符和多余空白
            shard_text = shard_text.replace('\x00', '').strip()
            if not shard_text:
                continue
            
            bge_score_buff_dict[0].append(combined_key)
            bge_score_buff_dict[1].append([query, shard_text])
            
            if len(bge_score_buff_dict[0]) >= batch_size:
                batch_count += 1
                print(f"[Rerank] Processing batch {batch_count}, size: {len(bge_score_buff_dict[0])}")
                
                if process_batch_with_retry(bge_score_buff_dict[0], bge_score_buff_dict[1]):
                    success_count += 1
                else:
                    fail_count += 1
                    # 失败降级：使用原始分数
                    for combined_key in bge_score_buff_dict[0]:
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
        print(f"[Rerank] Processing final batch {batch_count}, size: {len(bge_score_buff_dict[0])}")
        
        if process_batch_with_retry(bge_score_buff_dict[0], bge_score_buff_dict[1]):
            success_count += 1
        else:
            fail_count += 1
            for combined_key in bge_score_buff_dict[0]:
                result_dict_sorted[combined_key]['rerank_score'] = \
                    result_dict_sorted[combined_key]['final_score']
        
        bge_score_buff_dict = [[], []]
    
    print(f'[Rerank] Completed: {success_count} successful, {fail_count} failed out of {batch_count} batches')
             
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

    print(f'[Rerank] keyword weight num = {weight_num}')
    kwargs['result_dict'] = result_dict
    return kwargs

def concat_shards_by_rank(**kwargs):
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
    
    print(f'[concat_shards_by_rank], using time={time.time()-start_time}')

    selectedDocs = []
    
    sim_num = min(sim_num, len(sorted_top_chunk))
    for i in range(sim_num):
        mmrStep(0.9, selectedDocs, sorted_top_chunk, simMatrix)
    
    print(f'[concat_shards_by_rank] mmrStep, using time={time.time() - start_time}')

    print(f"[concat_shards_by_rank]compare: after mmr top10={selectedDocs[:30]}")

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
                print(f'[concat_shards_by_rank] chunks_id={chunks_id}, score={concat_score}')
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
    
    print(f'[concat_shards_by_rank] min concat_scores={min_concat_score}, chunks_id={min_concat_id}')

    if random.random() < 0.1 or is_debug:
        for i, d in enumerate(res_arr):
            print(f'[concat_shards_by_rank] rk={i}, res={d["text"][:200]}')

    return res_arr

def query_expand_srv(query: str, query_expand: list):
    query_contains = {
        'match': {
            'question': query
        }
    }
    es_res = ES.search(index="query_answer_history_v1023", query=query_contains, size=3)["hits"]["hits"]
    candidates = []
    for r in es_res:
        dct = {}
        score = r["_score"]
        dct['question'] = r["_source"]["question"]
        dct['answer'] = r["_source"]["answer"]
        dct['score'] = score
        candidates.append(dct)

    for dct in sorted(candidates, key=lambda x: -x['score']):
        editsim = levenshteinDistance(query, dct['question'])
        if editsim <= 2:
            query_expand.append(dct['answer'])
            return

    gpt4_df = pd.read_excel('/home/tcl/rqa_dir/models_lxl/test_0222_60.xlsx')
    if query in gpt4_df['问题'].values: 
        hanghao = gpt4_df[gpt4_df['问题'] == query].index.to_list()[0]
        query_expand.append(gpt4_df.iloc[hanghao, 1])
        return
    else :
        have_ans_querys = list(gpt4_df['问题'].values)
        for query_index in range(len(have_ans_querys)):
            querys_distance = levenshteinDistance(query, have_ans_querys[query_index])
            max_query_len = max(len(query), len(have_ans_querys[query_index]))
            if querys_distance/max_query_len < 0.2:
                query_expand.append(gpt4_df.iloc[query_index, 1])
                print(query, have_ans_querys[query_index], gpt4_df.iloc[query_index, 1])
                return
    
    url = 'https://tcl-ai-france.openai.azure.com/openai/deployments/gpt-4-0314/chat/completions?api-version=2023-03-15-preview'
    headers = {
        'Content-Type': 'application/json',
        'api-key': '98ff3b4afac846a7bede351bcec20ce8'
    }
    temperature = 0
    messages = [
        {
            "role": "system",
            "content": "You are an expert in the field of semiconductor displays technology. "
        },
        {
            "role": "assistant",
            "content": ""
        },
        {
            "role": "user",
            "content": f"""请用中文简短地回答问题: '{query}'。字数在30个字以内。"""
        }
    ]
    data = json.dumps({"messages": messages, "temperature": temperature})
    try:
        response = HTTP_SESSION.post(url, data=data, headers=headers, timeout=(10, 30))
        res = response.json().get('choices')[0].get('message').get('content')
        query_expand.append(res.strip(' '))
    except Exception as e:
        print(f'cannot find query expand by gpt-4 engine: {e}')
    return


@app.route('/api-rqa-search/test', methods=['GET'])
def hello_world():
    return json_result(0, '', 'Service available')

@app.route('/api-rqa-search/search', methods=['POST'])
def get_data():
    """
    搜索服务主函数
    """
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
        
        # ⭐ 使用带重试的编码服务
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
        print(f'[get_data] expand queries = {query_expand}, emb_len={len(query_embed_expand)}')

        params['query_embed'] = query_embed
        params['query_embed_expand'] = query_embed_expand
        params['query_rank_embed'] = query_rank_embed
        params['query_rank_embed_expand'] = query_rank_embed_expand
        params['target_doc_name'] = target_doc_name
        params['flag_query_rel'] = True
        
        if len(query_en.strip()) > 0:
            params['query_en'] = query_en
        params['result_dict'] = result_dict
        
        # 步骤3: 多路召回
        params = recall_pipeline(**params)
        print(f'recall spent time = {time.time() - start_time}')
        
        if not params['flag_query_rel']:
            code = 1
            data['msg'] = 'query must be relevant'
            data['doc_num'] = 0
            data['arr'] = []
            return json_result(code, msg, data)

        # 步骤4: 排序与重排
        params = rank_pipeline(**params)
        print(f'rank spent time = {time.time() - start_time}')
        params = rerank_pipeline(**params)
        print(f'rerank spent time = {time.time() - start_time}')
        print(f'In request: query={query}, top_doc_num={top_doc_num}')
        params['top_doc_num'] = top_doc_num
        params['concat_num'] = CONCAT_CHUNK_NUM
        params['is_debug'] = is_debug

        # 步骤5: 结果拼接与返回
        similar_shards = concat_shards_by_rank(**params)
        high_scores = []
        for dict in similar_shards:
            score = dict['score']
            print(score)
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
        print(f'[ERROR] get_data exception: {msg}')

    now = datetime.datetime.now()
    data['ts'] = int(datetime.datetime.timestamp(now) * 1000)
    print(f'total spent time = {time.time() - start_time}')
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
print(f'start server at {time.time()}')

# 服务启动参数填充
config = configparser.ConfigParser()
config.read('./config/search_srv_pipeline_l.ini', encoding='UTF-8')
print(f'load embed model name = {MODEL_NAME}')
print(f'load mongo tbl={TABLE_NAME} , {type(TABLE_NAME)}, version={VERSION}')

scheduler = BackgroundScheduler()
scheduler.add_job(crontab_update_config, 'interval', seconds=180, coalesce=True, replace_existing=True)
scheduler.start()

load_data(TABLE_NAME, MODEL_NAME)

if __name__ == '__main__':
    # 启用多线程支持并发请求处理
    print(f'🚀 [Server Start] MongoDB batch_size={MONGO_BATCH_SIZE}, HTTP pool_size={HTTP_ADAPTER.pool_maxsize}')
    app.run('10.70.223.31', port=9510, threaded=True, processes=1)
