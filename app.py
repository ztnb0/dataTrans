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


def save_config():
    """把内存 SOURCES 写回 config.json"""
    sources = list(SOURCES.values())
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump({'sources': sources}, f, ensure_ascii=False, indent=2)
        f.write('\n')


def save_env_password(source_id, password):
    """在 .env 中新增/更新该数据源密码行，并同步到 os.environ"""
    key = password_env_key(source_id)
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, 'r', encoding='utf-8') as f:
            lines = f.read().splitlines()
    else:
        lines = []
    found = False
    for i, line in enumerate(lines):
        if line.startswith(key + '='):
            lines[i] = key + '=' + password
            found = True
            break
    if not found:
        lines.append(key + '=' + password)
    with open(ENV_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    os.environ[key] = password


def delete_env_password(source_id):
    """从 .env 删除该数据源密码行"""
    key = password_env_key(source_id)
    if not os.path.exists(ENV_PATH):
        return
    with open(ENV_PATH, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()
    lines = [l for l in lines if not l.startswith(key + '=')]
    with open(ENV_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    os.environ.pop(key, None)


def gen_source_id(name):
    """由名称生成唯一数据源 id"""
    slug = re.sub(r'[^A-Za-z0-9]+', '-', (name or '').strip()).strip('-').lower()
    if not slug:
        slug = 'src'
    base = 'user-%s-%d' % (slug[:20], int(time.time()))
    candidate = base
    i = 2
    while candidate in SOURCES:
        candidate = base + '-' + str(i)
        i += 1
    return candidate


def get_source(source_id):
    if source_id not in SOURCES:
        raise HTTPException(400, '未知数据源: %s' % source_id)
    return SOURCES[source_id]


def source_type(source_id):
    s = get_source(source_id)
    t = s.get('type', 'mysql')
    return t if t in ('mysql', 'oracle') else 'mysql'


def is_oracle(source_id):
    return source_type(source_id) == 'oracle'


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


_oracle_client_initialized = False


def get_conn(source_id):
    s = get_source(source_id)
    if s.get('type') == 'oracle':
        return get_oracle_conn(source_id, s)
    return pymysql.connect(host=s['host'], port=int(s.get('port', 3306)),
                           user=s['user'], password=get_password(source_id),
                           charset=s.get('charset', 'utf8mb4'))


def get_oracle_conn(source_id, s):
    """Oracle 连接（thick 模式，需 Instant Client）"""
    global _oracle_client_initialized
    try:
        import oracledb
    except ImportError:
        raise HTTPException(500, '未安装 oracledb，请执行 pip install oracledb')
    if not _oracle_client_initialized:
        lib_dir = s.get('instant_client', '')
        if lib_dir and os.path.isdir(lib_dir):
            oracledb.init_oracle_client(lib_dir=lib_dir)
        else:
            oracledb.init_oracle_client()
        _oracle_client_initialized = True
    host = s['host']
    port = int(s.get('port', 1521))
    service = s.get('service', '')
    dsn = oracledb.makedsn(host, port, service_name=service) if service else oracledb.makedsn(host, port)
    return oracledb.connect(user=s['user'], password=get_password(source_id), dsn=dsn)


def _connect(s):
    """使用 source dict 建立连接（密码来自 dict，供测试连接用，不依赖 SOURCES）"""
    if s.get('type') == 'oracle':
        global _oracle_client_initialized
        try:
            import oracledb
        except ImportError:
            raise HTTPException(500, '未安装 oracledb，请执行 pip install oracledb')
        if not _oracle_client_initialized:
            lib_dir = s.get('instant_client', '')
            if lib_dir and os.path.isdir(lib_dir):
                oracledb.init_oracle_client(lib_dir=lib_dir)
            else:
                oracledb.init_oracle_client()
            _oracle_client_initialized = True
        host = s['host']
        port = int(s.get('port', 1521))
        service = s.get('service', '')
        dsn = oracledb.makedsn(host, port, service_name=service) if service else oracledb.makedsn(host, port)
        return oracledb.connect(user=s['user'], password=s.get('password', ''), dsn=dsn)
    return pymysql.connect(host=s['host'], port=int(s.get('port', 3306)),
                           user=s['user'], password=s.get('password', ''),
                           charset=s.get('charset', 'utf8mb4'))


def safe_ident(name):
    """校验并反引号包裹标识符，防注入"""
    if not re.match(r'^[A-Za-z0-9_\-]+$', name or ''):
        raise HTTPException(400, '非法标识符: %s' % name)
    return '`' + name + '`'


def oracle_ident(name):
    """Oracle 标识符：校验后用双引号包裹（保留大写）"""
    if not re.match(r'^[A-Za-z0-9_\-]+$', name or ''):
        raise HTTPException(400, '非法标识符: %s' % name)
    return '"' + name.upper() + '"'


def get_primary_key(source_id, database, table):
    conn = get_conn(source_id)
    try:
        cur = conn.cursor()
        if is_oracle(source_id):
            cur.execute("SELECT cc.column_name FROM all_constraints c, all_cons_columns cc "
                        "WHERE c.owner = cc.owner AND c.constraint_name = cc.constraint_name "
                        "AND c.constraint_type = 'P' AND c.owner = :1 AND c.table_name = :2",
                        (database.upper(), table.upper()))
        else:
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
    excluded_ids: Optional[List[str]] = None  # 取消勾选（不传输）的行标识列表
    schemas: Optional[List[str]] = None       # Oracle 多 schema 合并（如 ['BFEC','SHFEC','GZFEC']）


class SourceRequest(BaseModel):
    type: str = 'mysql'            # mysql / oracle
    name: str
    host: str
    port: int = 0
    user: str
    password: str = ''
    charset: str = ''              # mysql 用
    service: str = ''              # oracle 用
    schemas: List[str] = []        # oracle 多 schema
    instant_client: str = ''       # oracle thick 模式路径


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


def build_time_where(time_field, date_from, date_to, oracle=False):
    """返回 (where_clause, params)，未填则返回 ('', [])"""
    if not time_field:
        return '', []
    f = oracle_ident(time_field) if oracle else safe_ident(time_field)
    parts = []
    params = []
    if date_from:
        if oracle:
            # Oracle 年份数字字段：>= 起始年
            parts.append(f + ' >= ' + str(int(_parse_start(date_from).year)))
        else:
            parts.append(f + ' >= %s')
            params.append(_parse_start(date_from).strftime('%Y-%m-%d %H:%M:%S'))
    if date_to:
        if oracle:
            # Oracle：<= 结束年（含当年）
            parts.append(f + ' <= ' + str(int(_parse_start(date_to).year)))
        else:
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
    return {'sources': [{'id': s['id'], 'name': s.get('name', s['id']),
                         'type': s.get('type', 'mysql'),
                         'schemas': s.get('schemas', []),
                         'user_added': bool(s.get('user_added'))}
                        for s in SOURCES.values()]}


def _build_source_dict(req):
    """SourceRequest -> source dict（不含 id、user_added）"""
    s = {
        'type': req.type,
        'name': req.name.strip(),
        'host': req.host.strip(),
        'port': int(req.port),
        'user': req.user.strip(),
        'password': req.password,
    }
    if req.type == 'mysql':
        s['charset'] = req.charset or 'gbk'
    elif req.type == 'oracle':
        if req.service:
            s['service'] = req.service.strip()
        if req.schemas:
            s['schemas'] = [x.strip() for x in req.schemas if x.strip()]
        if req.instant_client:
            s['instant_client'] = req.instant_client.strip()
    return s


@app.post('/api/sources/test')
def test_source(req: SourceRequest):
    """测试连接（不落盘）"""
    try:
        conn = _connect(_build_source_dict(req))
        conn.close()
        return {'ok': True, 'message': '连接成功'}
    except HTTPException as e:
        return {'ok': False, 'message': str(e.detail)}
    except Exception as e:
        return {'ok': False, 'message': str(e)}


@app.post('/api/sources')
def add_source(req: SourceRequest):
    """新增数据源（持久化到 config.json + .env）"""
    req.type = req.type.lower().strip()
    if req.type not in ('mysql', 'oracle'):
        raise HTTPException(400, 'type 只支持 mysql 或 oracle')
    if not (req.name and req.name.strip()):
        raise HTTPException(400, '名称不能为空')
    if not (req.host and req.host.strip()):
        raise HTTPException(400, '主机不能为空')
    if not (req.user and req.user.strip()):
        raise HTTPException(400, '用户名不能为空')
    if not req.port:
        req.port = 3306 if req.type == 'mysql' else 1521

    source_id = gen_source_id(req.name.strip())
    s = _build_source_dict(req)
    s['id'] = source_id
    s['user_added'] = True
    s.pop('password', None)

    SOURCES[source_id] = s
    save_config()
    if req.password:
        save_env_password(source_id, req.password)

    return {'source': {'id': source_id, 'name': s['name'], 'type': s['type'],
                       'schemas': s.get('schemas', []), 'user_added': True}}


@app.delete('/api/sources/{source_id}')
def delete_source(source_id: str):
    """删除数据源（仅限用户新增的）"""
    if source_id not in SOURCES:
        raise HTTPException(404, '数据源不存在: %s' % source_id)
    if not SOURCES[source_id].get('user_added'):
        raise HTTPException(400, '内置数据源不允许删除')
    del SOURCES[source_id]
    save_config()
    delete_env_password(source_id)
    return {'ok': True}


@app.get('/api/databases')
def list_databases(source: str):
    conn = get_conn(source)
    try:
        cur = conn.cursor()
        if is_oracle(source):
            # Oracle 列出可访问的 schema（表所在的 owner）
            cur.execute("SELECT DISTINCT owner FROM all_tables "
                        "WHERE owner NOT IN ('SYS','SYSTEM','XDB','MDSYS','CTXSYS','OLAPSYS','ORDSYS','OUTLN','DBSNMP','WMSYS','EXFSYS','SYSMAN','MGMT_VIEW') "
                        "ORDER BY owner")
            return {'databases': [r[0] for r in cur.fetchall()]}
        cur.execute('SHOW DATABASES')
        return {'databases': [r[0] for r in cur.fetchall()]}
    finally:
        conn.close()


@app.get('/api/tables')
def list_tables(source: str, database: str):
    conn = get_conn(source)
    try:
        cur = conn.cursor()
        if is_oracle(source):
            cur.execute("SELECT table_name FROM all_tables WHERE owner = :1 ORDER BY table_name",
                        (database.upper(),))
        else:
            cur.execute('SHOW TABLES FROM %s' % safe_ident(database))
        return {'tables': [r[0] for r in cur.fetchall()]}
    finally:
        conn.close()


@app.get('/api/columns')
def list_columns(source: str, database: str, table: str):
    conn = get_conn(source)
    try:
        cur = conn.cursor()
        if is_oracle(source):
            cur.execute("SELECT a.column_name, a.data_type, "
                        "COALESCE(c.comments, '') AS comments "
                        "FROM all_tab_columns a "
                        "LEFT JOIN all_col_comments c ON c.owner = a.owner AND c.table_name = a.table_name AND c.column_name = a.column_name "
                        "WHERE a.owner = :1 AND a.table_name = :2 ORDER BY a.column_id",
                        (database.upper(), table.upper()))
            cols = [{'name': r[0], 'type': r[1], 'comment': r[2] or ''} for r in cur.fetchall()]
            return {'columns': cols, 'primary_key': get_primary_key(source, database, table)}
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
        if is_oracle(source):
            cur.execute('SELECT * FROM %s.%s WHERE ROWNUM <= %d' % (
                oracle_ident(database), oracle_ident(table), limit))
        else:
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
    """按请求读源表数据，返回 (fields, time_field, rows, has_schema)。
    支持 Oracle 多 schema 合并（每行首列为 schema）。"""
    oracle = is_oracle(req.source)
    schemas = req.schemas if (oracle and req.schemas) else None
    pk = get_primary_key(req.source, req.database, req.table)

    fields = list(req.source_fields)
    time_field = req.time_field or ''
    if time_field and time_field not in fields:
        fields.append(time_field)
    if pk and pk not in fields:
        fields.append(pk)

    where, params = build_time_where(time_field, req.date_from, req.date_to, oracle=oracle)
    ident = oracle_ident if oracle else safe_ident

    conn = get_conn(req.source)
    try:
        cur = conn.cursor()
        rows = []
        if schemas:
            # Oracle 多 schema 合并
            for sch in schemas:
                sch_u = sch.upper()
                w2, p2 = where, list(params)
                if exclude and req.excluded_ids:
                    excl = [x.split(':', 1)[1] for x in req.excluded_ids if x.startswith(sch_u + ':')]
                    if excl:
                        ph = ','.join([':%d' % (len(p2) + i + 1) for i in range(len(excl))])
                        w2 += (' AND ' if w2 else ' WHERE ') + ident(pk) + ' NOT IN (' + ph + ')'
                        p2 += [int(x) if str(x).isdigit() else x for x in excl]
                sql = 'SELECT %s FROM %s.%s%s' % (
                    ','.join(ident(f) for f in fields), ident(sch_u), ident(req.table), w2)
                if req.limit and req.limit > 0:
                    sql = 'SELECT * FROM (%s) WHERE ROWNUM <= %d' % (sql, req.limit)
                cur.execute(sql, p2)
                for r in cur.fetchall():
                    rows.append((sch_u,) + tuple(r))
            fields = ['_schema'] + fields
        else:
            w2, p2 = where, list(params)
            if exclude and req.excluded_ids:
                excl = [x.split(':', 1)[-1] for x in req.excluded_ids]
                if excl:
                    ph = ','.join(['%s'] * len(excl)) if not oracle else \
                        ','.join([':%d' % (len(p2) + i + 1) for i in range(len(excl))])
                    w2 += (' AND ' if w2 else ' WHERE ') + ident(pk) + ' NOT IN (' + ph + ')'
                    p2 += [int(x) if str(x).isdigit() else x for x in excl]
            sql = 'SELECT %s FROM %s.%s%s' % (
                ','.join(ident(f) for f in fields), ident(req.database), ident(req.table), w2)
            if req.limit and req.limit > 0:
                if oracle:
                    sql = 'SELECT * FROM (%s) WHERE ROWNUM <= %d' % (sql, req.limit)
                else:
                    sql += ' LIMIT %d' % req.limit
            cur.execute(sql, p2)
            rows = cur.fetchall()
        return fields, time_field, rows, bool(schemas)
    finally:
        conn.close()


def _count_rows(req):
    """返回 (source_total, filtered_total)，支持 Oracle 多 schema"""
    oracle = is_oracle(req.source)
    schemas = req.schemas if (oracle and req.schemas) else None
    ident = oracle_ident if oracle else safe_ident
    where, params = build_time_where(req.time_field, req.date_from, req.date_to, oracle=oracle)

    conn = get_conn(req.source)
    try:
        cur = conn.cursor()
        def count(w, p):
            sql = 'SELECT COUNT(*) FROM %s.%s%s' % (ident(req.database), ident(req.table), w)
            cur.execute(sql, p)
            return cur.fetchone()[0]

        source_total = 0
        filtered_total = 0
        if schemas:
            for sch in schemas:
                sch_u = sch.upper()
                def cnt_for_schema(w):
                    sql = 'SELECT COUNT(*) FROM %s.%s%s' % (ident(sch_u), ident(req.table), w)
                    cur.execute(sql, list(params))
                    return cur.fetchone()[0]
                source_total += cnt_for_schema('')
                filtered_total += cnt_for_schema(where)
        else:
            source_total = count('', [])
            filtered_total = count(where, params)
        return source_total, filtered_total
    finally:
        conn.close()


def _row_id(pk, row, fields, has_schema):
    """生成行唯一标识：多 schema 时为 'SCHEMA:pk'，否则 pk 值"""
    col_index = {name: idx for idx, name in enumerate(fields)}
    pkv = row[col_index[pk]]
    if has_schema:
        return '%s:%s' % (row[col_index['_schema']], pkv)
    return str(pkv)


def _time_value(val):
    """Oracle 数字年份 -> datetime(1月1日)，供时间字段转换"""
    if isinstance(val, (int, float)) and 1000 <= val <= 9999:
        return datetime.datetime(int(val), 1, 1)
    return val


@app.post('/api/transfer/preview')
def transfer_preview(req: TransferRequest):
    """传输前预览：源表总数 + 待传输条数 + 全部待传输行（dry-run，不写库）"""
    if not req.source_fields or not req.target_fields:
        raise HTTPException(400, '请先配置字段映射')
    if len(req.source_fields) != len(req.target_fields):
        raise HTTPException(400, '映射字段数量不匹配')

    pk = get_primary_key(req.source, req.database, req.table)
    source_total, filtered_total = _count_rows(req)
    fields, time_field, rows, has_schema = _read_rows(req, exclude=False)
    total = len(rows)

    columns = list(req.target_fields)
    if time_field and JDY_TIME_KEY not in columns:
        columns.append(JDY_TIME_KEY)
    if req.platform_value and JDY_PLATFORM_KEY not in columns:
        columns.append(JDY_PLATFORM_KEY)

    col_index = {name: idx for idx, name in enumerate(fields)}
    out_rows = []
    for row in rows:
        obj = {'_row_id': _row_id(pk, row, fields, has_schema)}
        for j, src in enumerate(req.source_fields):
            obj[req.target_fields[j]] = to_display(row[col_index[src]])
        if time_field:
            obj[JDY_TIME_KEY] = to_display(_time_value(row[col_index[time_field]]))
        if req.platform_value:
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

    fields, time_field, rows, has_schema = _read_rows(req, exclude=True)

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
                rec[JDY_TIME_KEY] = normalize_value(_time_value(row[col_index[time_field]]))
            if req.platform_value:
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
