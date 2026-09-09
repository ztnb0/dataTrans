# -*- coding: utf-8 -*-
"""数据迁移工具后端：后台 MySQL <-> 简道云"""
import os
import sys
import re
import json
import time
import datetime
import requests
import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import List, Optional

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
ENV_PATH = os.path.join(BASE_DIR, '.env')


def load_env():
    """从 .env 文件读取 KEY=VALUE 到 os.environ（不覆盖已存在的环境变量）"""
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


load_env()

app = FastAPI(title='dataTrans', version='1.0.0')

# ---------- 简道云配置（APIKey 来自 .env） ----------
JDY_API_KEY = os.environ.get('JDY_API_KEY', '')
JDY_APP_ID = os.environ.get('JDY_APP_ID', '598d1c563788a36bcba8a193')
JDY_ENTRY_ID = os.environ.get('JDY_ENTRY_ID', '6aa0bbef65115c860c3a7bfd')
JDY_BASE = 'https://api.jiandaoyun.com/api/v5'

JDY_HEADERS = {
    'Authorization': 'Bearer ' + JDY_API_KEY,
    'Content-Type': 'application/json',
}

# 简道云每次批量写入的上限
JDY_BATCH_SIZE = 100

# 简道云表单固定字段 key（来自该应用表单）
JDY_PLATFORM_KEY = '_widget_1788918827859'   # 平台
JDY_CODE_KEY = '_widget_1788918827860'       # 项目编号
JDY_NAME_KEY = '_widget_1788918827861'       # 项目名称
JDY_TIME_KEY = '_widget_1788918827862'       # 平台数据创建时间

# ---------- 数据源配置（来自 config.json，密码来自 .env） ----------


def password_env_key(source_id):
    """数据源 id -> 环境变量名，如 dpt-182 -> DB_DPT_182_PASSWORD"""
    return 'DB_' + source_id.replace('-', '_').upper() + '_PASSWORD'


# 默认数据源（config.json 缺失/为空时的回退）
DEFAULT_SOURCE = {
    'id': 'dpt-182',
    'name': '主库 192.168.2.182',
    'host': '192.168.2.182',
    'port': 3306,
    'user': 'dpt_dev',
    'charset': 'gbk',
}

SOURCES = {}


def load_config():
    global SOURCES
    sources = []
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            sources = data.get('sources', []) if isinstance(data, dict) else data
        except Exception:
            sources = []
    if not sources:
        sources = [DEFAULT_SOURCE]
    SOURCES = {s.get('id'): s for s in sources if s.get('id')}


load_config()


def get_source(source_id):
    if source_id not in SOURCES:
        raise HTTPException(400, '未知数据源: %s' % source_id)
    return SOURCES[source_id]


def get_password(source_id):
    """优先取 config.json 里的 password（向后兼容），否则取环境变量"""
    s = get_source(source_id)
    if s.get('password'):
        return s['password']
    pwd = os.environ.get(password_env_key(source_id), '')
    if not pwd:
        raise HTTPException(400, '数据源 %s 缺少密码：请在 .env 中配置 %s' %
                            (source_id, password_env_key(source_id)))
    return pwd


def get_conn(source_id):
    s = get_source(source_id)
    return pymysql.connect(host=s['host'], port=int(s.get('port', 3306)),
                           user=s['user'], password=get_password(source_id),
                           charset=s.get('charset', 'utf8mb4'))


def safe_ident(name):
    """校验并反引号包裹标识符，防注入"""
    if not re.match(r'^[A-Za-z0-9_\-]+$', name or ''):
        raise HTTPException(400, '非法标识符: %s' % name)
    return '`' + name + '`'


def get_primary_key(source_id, database, table):
    conn = get_conn(source_id)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_KEY='PRI'",
                    (database, table))
        r = cur.fetchone()
        return r[0] if r else 'id'
    finally:
        conn.close()


# ---------- 模型 ----------
class TransferRequest(BaseModel):
    source: str                       # 数据源 id
    database: str
    table: str
    source_fields: List[str]          # 数据库表字段名（映射字段）
    target_fields: List[str]          # 简道云字段 key（一一对应）
    platform_value: Optional[str] = ''  # “平台”字段手写固定值
    limit: Optional[int] = 0          # 0 表示全部
    time_field: Optional[str] = ''    # 时间字段（筛选 + 平台数据创建时间来源）
    date_from: Optional[str] = ''     # 如 2025 / 2025-06 / 2025-06-01
    date_to: Optional[str] = ''
    excluded_ids: Optional[List[int]] = None  # 取消勾选（不传输）的主键 id 列表


# ---------- 时间范围 ----------
def _detect_precision(s):
    """根据日期字符串格式推断精度：'2025'->year, '2025-06'->month, '2025-06-01'->day"""
    n = len(s.split('-'))
    return 'day' if n == 3 else 'month' if n == 2 else 'year'


def _parse_start(s):
    p = _detect_precision(s)
    if p == 'year':
        return datetime.datetime(int(s), 1, 1)
    if p == 'month':
        y, m = s.split('-')
        return datetime.datetime(int(y), int(m), 1)
    return datetime.datetime.strptime(s, '%Y-%m-%d')


def _range_end(s):
    p = _detect_precision(s)
    start = _parse_start(s)
    if p == 'year':
        return start.replace(year=start.year + 1)
    if p == 'month':
        if start.month == 12:
            return start.replace(year=start.year + 1, month=1)
        return start.replace(month=start.month + 1)
    return start + datetime.timedelta(days=1)


def build_time_where(time_field, date_from, date_to):
    """返回 (where_clause, params)，未填则返回 ('', [])"""
    if not time_field:
        return '', []
    f = safe_ident(time_field)
    parts = []
    params = []
    if date_from:
        parts.append(f + ' >= %s')
        params.append(_parse_start(date_from).strftime('%Y-%m-%d %H:%M:%S'))
    if date_to:
        parts.append(f + ' < %s')
        params.append(_range_end(date_to).strftime('%Y-%m-%d %H:%M:%S'))
    if parts:
        return ' WHERE ' + ' AND '.join(parts), params
    return '', []


# ---------- 值转换 ----------
def normalize_value(val):
    """数据库值 -> 简道云字段 {'value': ...}"""
    if val is None:
        return {'value': ''}
    if isinstance(val, datetime.datetime):
        utc = val - datetime.timedelta(hours=8)
        return {'value': utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')}
    if isinstance(val, datetime.date):
        return {'value': val.strftime('%Y-%m-%d')}
    if isinstance(val, bytes):
        return {'value': val.decode('utf-8', errors='ignore')}
    return {'value': str(val)}


def to_display(val):
    """数据库值 -> 展示字符串（预览用）"""
    if val is None:
        return ''
    if isinstance(val, datetime.datetime):
        return val.strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(val, datetime.date):
        return val.strftime('%Y-%m-%d')
    if isinstance(val, bytes):
        return val.decode('utf-8', errors='ignore')
    return str(val)


def __json(obj):
    return json.dumps(obj, ensure_ascii=False)


# ---------- 数据库 API ----------
@app.get('/api/sources')
def list_sources():
    """返回数据源列表（不含密码）"""
    return {'sources': [{'id': s['id'], 'name': s.get('name', s['id'])}
                        for s in SOURCES.values()]}


@app.get('/api/databases')
def list_databases(source: str):
    conn = get_conn(source)
    try:
        cur = conn.cursor()
        cur.execute('SHOW DATABASES')
        return {'databases': [r[0] for r in cur.fetchall()]}
    finally:
        conn.close()


@app.get('/api/tables')
def list_tables(source: str, database: str):
    conn = get_conn(source)
    try:
        cur = conn.cursor()
        cur.execute('SHOW TABLES FROM %s' % safe_ident(database))
        return {'tables': [r[0] for r in cur.fetchall()]}
    finally:
        conn.close()


@app.get('/api/columns')
def list_columns(source: str, database: str, table: str):
    conn = get_conn(source)
    try:
        cur = conn.cursor()
        cur.execute('SHOW FULL COLUMNS FROM %s.%s' % (safe_ident(database), safe_ident(table)))
        cols = [{'name': r[0], 'type': r[1], 'comment': r[8] or ''} for r in cur.fetchall()]
        return {'columns': cols, 'primary_key': get_primary_key(source, database, table)}
    finally:
        conn.close()


@app.get('/api/preview')
def preview(source: str, database: str, table: str, limit: int = 5):
    conn = get_conn(source)
    try:
        cur = conn.cursor()
        cur.execute('SELECT * FROM %s.%s LIMIT %d' % (safe_ident(database), safe_ident(table), limit))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return {'columns': cols, 'rows': [list(r) for r in rows]}
    finally:
        conn.close()


# ---------- 简道云 API ----------
@app.get('/api/jdy/fields')
def jdy_fields():
    r = requests.post(JDY_BASE + '/app/entry/widget/list', headers=JDY_HEADERS,
                      data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID}), timeout=30)
    if r.status_code != 200:
        raise HTTPException(500, '简道云字段获取失败: %s' % r.text)
    data = r.json()
    widgets = [{'name': w['name'], 'label': w.get('label', ''), 'type': w.get('type', '')}
               for w in data.get('widgets', [])]
    return {'widgets': widgets}


def _jdy_widgets():
    r = requests.post(JDY_BASE + '/app/entry/widget/list', headers=JDY_HEADERS,
                      data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID}), timeout=30)
    if r.status_code != 200:
        raise HTTPException(500, '简道云字段获取失败: %s' % r.text)
    return r.json().get('widgets', [])


@app.get('/api/jdy/preview')
def jdy_preview(limit: int = 20):
    """预览简道云表单已有数据（表格形式）"""
    widgets = _jdy_widgets()
    names = [w['name'] for w in widgets]
    labels = [w['label'] for w in widgets]
    r = requests.post(JDY_BASE + '/app/entry/data/list', headers=JDY_HEADERS,
                      data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID,
                                   'limit': limit, 'fields': names, 'filter': {}, 'sort': []}),
                      timeout=30)
    if r.status_code != 200:
        raise HTTPException(500, '简道云数据查询失败: %s' % r.text)
    data = r.json().get('data', [])
    rows = []
    for d in data:
        row = [d.get(n, '') for n in names]
        rows.append([('' if v is None else (v if isinstance(v, str) else str(v))) for v in row])
    return {'columns': labels, 'rows': rows}


def _read_rows(req, exclude=False):
    """按请求读源表数据，返回 (fields, time_field, rows)。
    主键字段始终加入 fields 列表（用于行标识/排除）。"""
    pk = get_primary_key(req.source, req.database, req.table)
    fields = list(req.source_fields)
    time_field = req.time_field or ''
    if time_field and time_field not in fields:
        fields.append(time_field)
    if pk not in fields:
        fields.append(pk)

    where, params = build_time_where(time_field, req.date_from, req.date_to)

    if exclude and req.excluded_ids:
        placeholders = ','.join(['%s'] * len(req.excluded_ids))
        where += (' AND ' if where else ' WHERE ') + safe_ident(pk) + ' NOT IN (' + placeholders + ')'
        params += list(req.excluded_ids)

    sql = 'SELECT %s FROM %s.%s' % (
        ','.join(safe_ident(f) for f in fields),
        safe_ident(req.database), safe_ident(req.table)) + where
    if req.limit and req.limit > 0:
        sql += ' LIMIT %d' % req.limit

    conn = get_conn(req.source)
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        return fields, time_field, cur.fetchall()
    finally:
        conn.close()


@app.post('/api/transfer/preview')
def transfer_preview(req: TransferRequest):
    """传输前预览：源表总数 + 待传输条数 + 全部待传输行（dry-run，不写库）"""
    if not req.source_fields or not req.target_fields:
        raise HTTPException(400, '请先配置字段映射')
    if len(req.source_fields) != len(req.target_fields):
        raise HTTPException(400, '映射字段数量不匹配')

    # 源表总数（无筛选）
    conn = get_conn(req.source)
    try:
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM %s.%s' % (safe_ident(req.database), safe_ident(req.table)))
        source_total = cur.fetchone()[0]
    finally:
        conn.close()

    # 应用时间筛选后的总数（无 limit）
    where, params = build_time_where(req.time_field, req.date_from, req.date_to)
    conn = get_conn(req.source)
    try:
        cur = conn.cursor()
        cur.execute('SELECT COUNT(*) FROM %s.%s%s' % (
            safe_ident(req.database), safe_ident(req.table), where), params)
        filtered_total = cur.fetchone()[0]
    finally:
        conn.close()

    pk = get_primary_key(req.source, req.database, req.table)
    fields, time_field, rows = _read_rows(req, exclude=False)
    total = len(rows)

    # 构建列（简道云字段 key，顺序）和每行数据
    columns = list(req.target_fields)
    if time_field and JDY_TIME_KEY not in columns:
        columns.append(JDY_TIME_KEY)

    col_index = {name: idx for idx, name in enumerate(fields)}
    out_rows = []
    for row in rows:
        obj = {'_row_id': row[col_index[pk]]}
        for j, src in enumerate(req.source_fields):
            obj[req.target_fields[j]] = to_display(row[col_index[src]])
        if time_field:
            obj[JDY_TIME_KEY] = to_display(row[col_index[time_field]])
        if req.platform_value and JDY_PLATFORM_KEY in req.target_fields:
            obj[JDY_PLATFORM_KEY] = req.platform_value
        out_rows.append(obj)

    return {
        'source_total': source_total,
        'filtered_total': filtered_total,
        'total': total,
        'columns': columns,
        'rows': out_rows,
    }


@app.post('/api/transfer')
def transfer(req: TransferRequest):
    """真正写入简道云（可带时间筛选 + 排除勾选）"""
    if not req.source_fields or not req.target_fields:
        raise HTTPException(400, '请先配置字段映射')
    if len(req.source_fields) != len(req.target_fields):
        raise HTTPException(400, '映射字段数量不匹配')

    pk = get_primary_key(req.source, req.database, req.table)
    fields, time_field, rows = _read_rows(req, exclude=True)

    total = len(rows)
    if total == 0:
        return {'success': 0, 'fail': 0, 'total': 0, 'message': '没有要传输的数据'}

    col_index = {name: idx for idx, name in enumerate(fields)}
    n_fields = len(req.target_fields)
    success = 0
    fail = 0
    errors = []

    for i in range(0, total, JDY_BATCH_SIZE):
        batch = rows[i:i + JDY_BATCH_SIZE]
        data_list = []
        for row in batch:
            rec = {}
            for j in range(n_fields):
                rec[req.target_fields[j]] = normalize_value(row[col_index[req.source_fields[j]]])
            if time_field:
                rec[JDY_TIME_KEY] = normalize_value(row[col_index[time_field]])
            if req.platform_value and JDY_PLATFORM_KEY in req.target_fields:
                rec[JDY_PLATFORM_KEY] = {'value': req.platform_value}
            data_list.append(rec)

        r = requests.post(JDY_BASE + '/app/entry/data/batch_create', headers=JDY_HEADERS,
                          data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID,
                                       'data_list': data_list}), timeout=60)
        if r.status_code != 200:
            fail += len(batch)
            errors.append('批次 %d-%d 请求失败: %s' % (i + 1, i + len(batch), r.text))
            continue
        resp = r.json()
        ok = resp.get('success_count', 0)
        success += ok
        f = len(batch) - ok
        if f > 0:
            fail += f
            errors.append('批次 %d-%d 部分失败: %s' % (i + 1, i + len(batch), __json(resp)))
        time.sleep(0.2)

    return {
        'success': success,
        'fail': fail,
        'total': total,
        'message': '完成：成功 %d 条，失败 %d 条' % (success, fail),
        'errors': errors[:20],
    }


app.mount('/', StaticFiles(directory='static', html=True), name='static')
