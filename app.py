# -*- coding: utf-8 -*-
"""数据迁移工具后端：后台 MySQL <-> 简道云"""
import os
import sys
import re
import json
import time
import datetime
import hashlib
import threading
import requests
import pymysql
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
from typing import List, Optional

sys.stdout.reconfigure(encoding='utf-8')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
ENV_PATH = os.path.join(BASE_DIR, '.env')
MAPPINGS_PATH = os.path.join(BASE_DIR, 'mappings.json')
AUTO_FILL_RULES_PATH = os.path.join(BASE_DIR, 'auto_fill_rules.json')
SYNC_STATE_PATH = os.path.join(BASE_DIR, 'sync_state.json')
SYNC_LOG_PATH = os.path.join(BASE_DIR, 'sync.log')

# ---------- 轮巡配置 ----------
SYNC_INTERVAL = 300      # 轮巡间隔（秒），默认 5 分钟
SYNC_OFF_START = 20      # 休眠开始时刻（时），含 20:00
SYNC_OFF_END = 8         # 休眠结束时刻（时），次日 08:00 恢复
SYNC_TIME_FROM = '2026'  # 时间筛选：只同步该年份及之后的数据
SYNC_LOCK = threading.Lock()

# 自动同步开关（前端按钮控制，内存态，重启后默认关闭）
#   all:     是否全部数据源自动同步
#   sources: {数据源id: 是否自动同步}，控制单个数据源
AUTO_SYNC_LOCK = threading.Lock()
AUTO_SYNC = {'all': False, 'sources': {}}


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
JDY_ID_KEY = '_widget_1789030761638'         # id（存源表主键，匹配键）
JDY_PLATFORM_KEY = '_widget_1788918827859'   # 平台
JDY_DATA_TYPE_KEY = '_widget_1789030761637'  # 数据类型
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
    data_type_value: Optional[str] = ''  # “数据类型”字段手写固定值
    limit: Optional[int] = 0          # 0 表示全部
    time_field: Optional[str] = ''    # 时间字段（筛选 + 平台数据创建时间来源）
    date_from: Optional[str] = ''     # 如 2025 / 2025-06 / 2025-06-01
    date_to: Optional[str] = ''
    excluded_ids: Optional[List[str]] = None  # 取消勾选（不传输）的行标识列表
    schemas: Optional[List[str]] = None       # Oracle 多 schema 合并（如 ['BFEC','SHFEC','GZFEC']）
    transforms: Optional[List[str]] = None    # 每个映射字段的转换类型（batch_number / city_name / ''），与 source_fields 等长


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
        return {'value': val.strftime('%Y-%m-%d %H:%M:%S')}
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


# ---------- 值转换规则（传输映射规则） ----------
PROVINCES = [
    '内蒙古', '黑龙江',            # 3 字，优先匹配
    '北京', '天津', '上海', '重庆', '香港', '澳门',
    '河北', '山西', '辽宁', '吉林', '江苏', '浙江', '安徽', '福建',
    '江西', '山东', '河南', '湖北', '湖南', '广东', '海南', '四川',
    '贵州', '云南', '陕西', '甘肃', '青海', '台湾',
    '广西', '宁夏', '新疆', '西藏',
]


def extract_city(area):
    """去掉省级前缀，返回市名（无匹配时原样去尾"市"）"""
    a = (area or '').strip()
    if not a:
        return ''
    for p in PROVINCES:
        if a.startswith(p):
            city = a[len(p):] or p   # 直辖市：北京北京 -> 北京
            return city.rstrip('市')
    return a.rstrip('市')


def apply_transform(value, transform_type):
    """按转换类型处理字段值；返回 None 表示该行应跳过（不传输）"""
    if not transform_type:
        return value
    if transform_type == 'batch_number':
        s = '' if value is None else str(value)
        m = re.search(r'(\d+)批', s)
        return m.group(1) if m else s
    if transform_type == 'city_name':
        a = '' if value is None else str(value)
        if '在线考试' in a:
            return None                      # 问题三：在线考试忽略
        city = extract_city(a)
        if not city or city == '其他':
            return None                      # 问题二：无明确市名排除
        return city + '考场'
    return value


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


@app.get('/api/sources/{source_id}')
def get_source_detail(source_id: str):
    """返回单个数据源完整信息（不含密码）"""
    if source_id not in SOURCES:
        raise HTTPException(404, '数据源不存在: %s' % source_id)
    return SOURCES[source_id]


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


@app.put('/api/sources/{source_id}')
def update_source(source_id: str, req: SourceRequest):
    """修改数据源（含改名等），密码留空则保留原密码"""
    if source_id not in SOURCES:
        raise HTTPException(404, '数据源不存在: %s' % source_id)
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

    old = SOURCES[source_id]
    s = _build_source_dict(req)
    password = (s.pop('password', '') or '').strip()
    s['id'] = source_id
    s['user_added'] = old.get('user_added', False)

    SOURCES[source_id] = s
    save_config()
    if password:
        save_env_password(source_id, password)

    return {'source': {'id': source_id, 'name': s['name'], 'type': s['type'],
                       'schemas': s.get('schemas', []), 'user_added': bool(s.get('user_added'))}}


@app.delete('/api/sources/{source_id}')
def delete_source(source_id: str):
    """删除数据源（含内置）"""
    if source_id not in SOURCES:
        raise HTTPException(404, '数据源不存在: %s' % source_id)
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
def jdy_preview(limit: int = 50, skip: int = 0):
    """预览简道云表单已有数据（分页表格）"""
    widgets = _jdy_widgets()
    names = [w['name'] for w in widgets]
    labels = [w['label'] for w in widgets]
    r = requests.post(JDY_BASE + '/app/entry/data/list', headers=JDY_HEADERS,
                      data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID,
                                   'limit': limit, 'skip': skip,
                                   'fields': names, 'filter': {}, 'sort': []}),
                      timeout=60)
    if r.status_code != 200:
        raise HTTPException(500, '简道云数据查询失败: %s' % r.text)
    data = r.json().get('data', [])
    rows = []
    for d in data:
        row = [d.get(n, '') for n in names]
        rows.append([('' if v is None else (v if isinstance(v, str) else str(v))) for v in row])
    return {'columns': labels, 'rows': rows, 'skip': skip, 'limit': limit}


@app.get('/api/jdy/stats')
def jdy_stats():
    """统计简道云表单各平台的数据条数（全量拉取平台字段后分组计数）"""
    counts = {}
    total = 0
    skip = 0
    limit = 100
    guard = 0
    while True:
        r = requests.post(JDY_BASE + '/app/entry/data/list', headers=JDY_HEADERS,
                          data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID,
                                       'limit': limit, 'skip': skip,
                                       'fields': [JDY_PLATFORM_KEY],
                                       'filter': {}, 'sort': []}),
                          timeout=60)
        if r.status_code != 200:
            raise HTTPException(500, '简道云数据查询失败: %s' % r.text)
        data = r.json().get('data', [])
        if not data:
            break
        for d in data:
            p = d.get(JDY_PLATFORM_KEY)
            p = p if p not in (None, '') else '（未填平台）'
            counts[p] = counts.get(p, 0) + 1
            total += 1
        if len(data) < limit:
            break
        skip += limit
        guard += 1
        if guard > 500:
            break
    platforms = sorted(counts.items(), key=lambda x: -x[1])
    return {'total': total, 'platforms': [{'platform': p, 'count': c} for p, c in platforms]}


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


def _transform_and_filter_rows(req, fields, rows, has_schema):
    """对读出的原始行做：值转换 + 跳过（无明确市名/在线考试）+ 去重（批次号相同且市名相同只留一条）。
    返回 [(row, transformed_values)]，transformed_values 与 source_fields 等长（转换后的原始值）。"""
    transforms = req.transforms or []
    col_index = {name: idx for idx, name in enumerate(fields)}
    # 去重键：非时间、非平台的映射目标字段（对应批次号 + 市名等业务字段）
    dedup_indexes = [j for j, t in enumerate(req.target_fields)
                     if t not in (JDY_TIME_KEY, JDY_PLATFORM_KEY)]
    has_transform = any(transforms)
    seen = set()
    kept = []
    for row in rows:
        transformed = []
        skip = False
        for j, src in enumerate(req.source_fields):
            tt = transforms[j] if j < len(transforms) else None
            val = apply_transform(row[col_index[src]], tt)
            if tt and val is None:
                skip = True
                break
            transformed.append(val)
        if skip:
            continue
        if has_transform and dedup_indexes:
            key = tuple(to_display(transformed[j]) for j in dedup_indexes)
            if key in seen:
                continue
            seen.add(key)
        kept.append((row, transformed))
    return kept


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
    kept = _transform_and_filter_rows(req, fields, rows, has_schema)
    total = len(kept)

    columns = list(req.target_fields)
    if time_field and JDY_TIME_KEY not in columns:
        columns.append(JDY_TIME_KEY)
    if req.platform_value and JDY_PLATFORM_KEY not in columns:
        columns.append(JDY_PLATFORM_KEY)
    if req.data_type_value and JDY_DATA_TYPE_KEY not in columns:
        columns.append(JDY_DATA_TYPE_KEY)

    col_index = {name: idx for idx, name in enumerate(fields)}
    out_rows = []
    for row, transformed in kept:
        obj = {'_row_id': _row_id(pk, row, fields, has_schema)}
        for j, src in enumerate(req.source_fields):
            obj[req.target_fields[j]] = to_display(transformed[j])
        if time_field:
            obj[JDY_TIME_KEY] = to_display(_time_value(row[col_index[time_field]]))
        if req.platform_value:
            obj[JDY_PLATFORM_KEY] = req.platform_value
        if req.data_type_value:
            obj[JDY_DATA_TYPE_KEY] = req.data_type_value
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
    """真正写入简道云（查重覆盖：已存在则 update，不存在则 batch_create）"""
    if not req.source_fields or not req.target_fields:
        raise HTTPException(400, '请先配置字段映射')
    if len(req.source_fields) != len(req.target_fields):
        raise HTTPException(400, '映射字段数量不匹配')

    fields, time_field, rows, has_schema = _read_rows(req, exclude=True)
    kept = _transform_and_filter_rows(req, fields, rows, has_schema)

    total = len(kept)
    if total == 0:
        return {'success': 0, 'fail': 0, 'total': 0, 'message': '没有要传输的数据'}

    col_index = {name: idx for idx, name in enumerate(fields)}
    n_fields = len(req.target_fields)
    pk_col = get_primary_key(req.source, req.database, req.table)
    pk_idx = col_index.get(pk_col)

    success = 0
    fail = 0
    updated = 0
    errors = []

    existing_map = jdy_list_id_map(req.data_type_value) if req.data_type_value else {}

    to_create = []
    for row, transformed in kept:
        rec = {}
        for j in range(n_fields):
            rec[req.target_fields[j]] = normalize_value(transformed[j])
        if time_field:
            rec[JDY_TIME_KEY] = normalize_value(_time_value(row[col_index[time_field]]))
        if req.platform_value:
            rec[JDY_PLATFORM_KEY] = {'value': req.platform_value}
        if req.data_type_value:
            rec[JDY_DATA_TYPE_KEY] = {'value': req.data_type_value}

        pk_val = str(row[pk_idx]) if pk_idx is not None else ''
        if pk_val:
            rec[JDY_ID_KEY] = {'value': pk_val}
        data_id = existing_map.get(pk_val) if pk_val else None
        if data_id:
            try:
                jdy_update(data_id, rec)
                updated += 1
                success += 1
            except HTTPException as e:
                fail += 1
                errors.append('更新 pk=%s 失败: %s' % (pk_val, e.detail))
            time.sleep(0.05)
        else:
            to_create.append(rec)

    for i in range(0, len(to_create), JDY_BATCH_SIZE):
        chunk = to_create[i:i + JDY_BATCH_SIZE]
        r = requests.post(JDY_BASE + '/app/entry/data/batch_create', headers=JDY_HEADERS,
                          data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID,
                                       'data_list': chunk}), timeout=60)
        if r.status_code != 200:
            fail += len(chunk)
            errors.append('批次 %d-%d 请求失败: %s' % (i + 1, i + len(chunk), r.text))
            continue
        resp = r.json()
        ok = resp.get('success_count', 0)
        success += ok
        f = len(chunk) - ok
        if f > 0:
            fail += f
            errors.append('批次 %d-%d 部分失败: %s' % (i + 1, i + len(chunk), __json(resp)))
        time.sleep(0.2)

    return {
        'success': success,
        'fail': fail,
        'total': total,
        'updated': updated,
        'message': '完成：成功 %d 条（含覆盖 %d 条），失败 %d 条' % (success, updated, fail),
        'errors': errors[:20],
    }


# ---------- 即时同步（回调） ----------


def load_mappings():
    if os.path.exists(MAPPINGS_PATH):
        try:
            with open(MAPPINGS_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_mappings(mappings):
    with open(MAPPINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(mappings, f, ensure_ascii=False, indent=2)


class MappingRequest(BaseModel):
    id: str
    source: str
    database: str
    table: str
    source_fields: List[str]
    target_fields: List[str]
    platform_value: Optional[str] = ''
    data_type_value: Optional[str] = ''
    transforms: Optional[List[str]] = None
    enabled: Optional[bool] = True


class NotifyRequest(BaseModel):
    table: str
    pk: str
    database: Optional[str] = ''

    @field_validator('pk', mode='before')
    @classmethod
    def _pk_to_str(cls, v):
        return str(v)


class NotifyBatchRequest(BaseModel):
    table: str
    database: Optional[str] = ''
    job: int = 1
    rows: List[dict]


def find_mapping_by_table(table, database=''):
    matches = [m for m in load_mappings() if m.get('table') == table and m.get('enabled', True)]
    if database:
        matches = [m for m in matches if m.get('database', '') == database]
    if not matches:
        raise HTTPException(400, '表 %s 未配置映射' % table)
    if len(matches) > 1:
        raise HTTPException(400, '表 %s 配置了多套映射，无法按表名唯一定位' % table)
    return matches[0]


def _ident_for(source_id):
    return oracle_ident if is_oracle(source_id) else safe_ident


def read_source_row(mapping, pk):
    src = mapping['source']
    pk_col = get_primary_key(src, mapping['database'], mapping['table'])
    fields = list(mapping['source_fields'])
    if pk_col not in fields:
        fields.append(pk_col)
    ident = _ident_for(src)
    conn = get_conn(src)
    try:
        cur = conn.cursor()
        if is_oracle(src):
            sql = 'SELECT %s FROM %s.%s WHERE %s = :1' % (
                ','.join(ident(f) for f in fields),
                ident(mapping['database']), ident(mapping['table']), ident(pk_col))
            cur.execute(sql, (pk,))
        else:
            sql = 'SELECT %s FROM %s.%s WHERE %s = %%s' % (
                ','.join(ident(f) for f in fields),
                ident(mapping['database']), ident(mapping['table']), ident(pk_col))
            cur.execute(sql, (pk,))
        row = cur.fetchone()
        return row, fields, pk_col
    finally:
        conn.close()


def build_jdy_record(mapping, row, col_index, pk_val):
    rec = {JDY_ID_KEY: {'value': str(pk_val)}}
    if mapping.get('data_type_value'):
        rec[JDY_DATA_TYPE_KEY] = {'value': mapping['data_type_value']}
    if mapping.get('platform_value'):
        rec[JDY_PLATFORM_KEY] = {'value': mapping['platform_value']}
    transforms = mapping.get('transforms') or []
    for j, (src, tgt) in enumerate(zip(mapping['source_fields'], mapping['target_fields'])):
        raw = row[col_index[src]]
        tt = transforms[j] if j < len(transforms) else None
        val = apply_transform(raw, tt)
        if tt and val is None:
            return None
        rec[tgt] = normalize_value(val)
    return rec


def build_jdy_record_from_dict(mapping, row, pk, partial=False):
    rec = {} if partial else {JDY_ID_KEY: {'value': str(pk)}}
    if not partial:
        if mapping.get('data_type_value'):
            rec[JDY_DATA_TYPE_KEY] = {'value': mapping['data_type_value']}
        if mapping.get('platform_value'):
            rec[JDY_PLATFORM_KEY] = {'value': mapping['platform_value']}
    transforms = mapping.get('transforms') or []
    for j, (src, tgt) in enumerate(zip(mapping['source_fields'], mapping['target_fields'])):
        if partial and src not in row:
            continue
        raw = row.get(src)
        tt = transforms[j] if j < len(transforms) else None
        val = apply_transform(raw, tt)
        if tt and val is None:
            if partial:
                continue
            return None
        rec[tgt] = normalize_value(val)
    return rec


def jdy_batch_create(data_list):
    r = requests.post(JDY_BASE + '/app/entry/data/batch_create', headers=JDY_HEADERS,
                      data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID,
                                   'data_list': data_list}), timeout=60)
    if r.status_code != 200:
        raise HTTPException(500, '简道云 batch_create 失败: %s' % r.text)
    return r.json()


def jdy_update(data_id, data):
    r = requests.post(JDY_BASE + '/app/entry/data/update', headers=JDY_HEADERS,
                      data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID,
                                   'data_id': data_id, 'data': data}), timeout=30)
    if r.status_code != 200:
        raise HTTPException(500, '简道云 update 失败: %s' % r.text)
    return r.json()


def jdy_find_by_id(pk, data_type_value):
    cond = [{'field': JDY_ID_KEY, 'type': 'text', 'method': 'eq', 'value': [str(pk)]}]
    if data_type_value:
        cond.append({'field': JDY_DATA_TYPE_KEY, 'type': 'text',
                     'method': 'eq', 'value': [data_type_value]})
    r = requests.post(JDY_BASE + '/app/entry/data/list', headers=JDY_HEADERS,
                      data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID,
                                   'limit': 1,
                                   'fields': [JDY_ID_KEY],
                                   'filter': {'rel': 'and', 'cond': cond},
                                   'sort': []}), timeout=30)
    if r.status_code != 200:
        raise HTTPException(500, '简道云数据查询失败: %s' % r.text)
    data = r.json().get('data', [])
    if data:
        d = data[0]
        return d.get('_id') or d.get('data_id') or d.get('dataId')
    return None


@app.get('/api/mappings')
def list_mappings():
    return {'mappings': load_mappings()}


@app.get('/api/mappings/{mapping_id}')
def get_mapping_detail(mapping_id: str):
    for m in load_mappings():
        if m.get('id') == mapping_id:
            return m
    raise HTTPException(404, '映射不存在: %s' % mapping_id)


@app.post('/api/mappings')
def save_mapping(req: MappingRequest):
    m = req.model_dump()
    if len(m['source_fields']) != len(m['target_fields']):
        raise HTTPException(400, '映射字段数量不匹配')
    mappings = load_mappings()
    replaced = False
    for i, old in enumerate(mappings):
        if old.get('id') == m['id']:
            mappings[i] = m
            replaced = True
            break
    if not replaced:
        mappings.append(m)
    save_mappings(mappings)
    return {'ok': True, 'mapping': m}


@app.delete('/api/mappings/{mapping_id}')
def delete_mapping(mapping_id: str):
    mappings = load_mappings()
    new = [m for m in mappings if m.get('id') != mapping_id]
    if len(new) == len(mappings):
        raise HTTPException(404, '映射不存在: %s' % mapping_id)
    save_mappings(new)
    return {'ok': True}


def load_auto_fill_rules():
    if os.path.exists(AUTO_FILL_RULES_PATH):
        try:
            with open(AUTO_FILL_RULES_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_auto_fill_rules(rules):
    with open(AUTO_FILL_RULES_PATH, 'w', encoding='utf-8') as f:
        json.dump(rules, f, ensure_ascii=False, indent=2)


@app.get('/api/auto-fill-rules')
def get_auto_fill_rules():
    return {'rules': load_auto_fill_rules()}


@app.post('/api/auto-fill-rules')
def set_auto_fill_rules(rules: List[dict]):
    save_auto_fill_rules(rules)
    return {'ok': True, 'rules': rules}


@app.post('/api/sync/notify')
def sync_notify(req: NotifyRequest):
    if not req.table or req.pk is None or req.pk == '':
        raise HTTPException(400, 'table / pk 不能为空')
    mapping = find_mapping_by_table(req.table, req.database or '')

    row, fields, pk_col = read_source_row(mapping, req.pk)
    if row is None:
        return {'ok': True, 'skipped': 'row_not_found'}

    col_index = {name: i for i, name in enumerate(fields)}
    pk_val = str(row[col_index[pk_col]])
    record = build_jdy_record(mapping, row, col_index, pk_val)
    if record is None:
        return {'ok': True, 'skipped': 'transform_skip'}

    data_id = jdy_find_by_id(pk_val, mapping.get('data_type_value', ''))
    if data_id:
        jdy_update(data_id, record)
        action = 'update'
    else:
        resp = jdy_batch_create([record])
        if resp.get('success_count', 0) <= 0:
            raise HTTPException(500, 'batch_create 失败: %s' % __json(resp))
        action = 'create'

    return {'ok': True, 'action': action, 'pk': pk_val}


@app.post('/api/sync/notify-batch')
def sync_notify_batch(req: NotifyBatchRequest):
    """批量即时同步：对方直接传行数据（源字段名 + 固定 pk），job=1 修改 / job=2 增加"""
    if not req.table or not req.rows:
        raise HTTPException(400, 'table / rows 不能为空')
    if req.job not in (1, 2):
        raise HTTPException(400, 'job 只能是 1(修改) 或 2(增加)')
    mapping = find_mapping_by_table(req.table, req.database or '')
    data_type_value = mapping.get('data_type_value', '')

    success = 0
    fail = 0
    skipped = 0
    results = []

    if req.job == 2:
        records = []
        for row in req.rows:
            pk_raw = row.get('pk')
            if pk_raw is None or pk_raw == '':
                fail += 1
                results.append({'pk': '', 'ok': False, 'reason': 'missing_pk'})
                continue
            pk = str(pk_raw)
            rec = build_jdy_record_from_dict(mapping, row, pk)
            if rec is None:
                skipped += 1
                results.append({'pk': pk, 'ok': False, 'reason': 'transform_skip'})
                continue
            records.append((pk, rec))
        for i in range(0, len(records), JDY_BATCH_SIZE):
            chunk = records[i:i + JDY_BATCH_SIZE]
            resp = jdy_batch_create([c[1] for c in chunk])
            ok_count = resp.get('success_count', 0)
            success += ok_count
            fail += len(chunk) - ok_count
            for c in chunk:
                results.append({'pk': c[0], 'ok': True})
            time.sleep(0.2)
    else:
        for row in req.rows:
            pk_raw = row.get('pk')
            if pk_raw is None or pk_raw == '':
                fail += 1
                results.append({'pk': '', 'ok': False, 'reason': 'missing_pk'})
                continue
            pk = str(pk_raw)
            rec = build_jdy_record_from_dict(mapping, row, pk, partial=True)
            if not rec:
                skipped += 1
                results.append({'pk': pk, 'ok': False, 'reason': 'no_fields'})
                continue
            data_id = jdy_find_by_id(pk, data_type_value)
            if not data_id:
                fail += 1
                results.append({'pk': pk, 'ok': False, 'reason': 'not_found'})
                continue
            try:
                jdy_update(data_id, rec)
                success += 1
                results.append({'pk': pk, 'ok': True})
            except HTTPException as e:
                fail += 1
                results.append({'pk': pk, 'ok': False, 'reason': str(e.detail)})

    return {'ok': True, 'job': req.job, 'success': success, 'fail': fail, 'skipped': skipped, 'results': results}


# ---------- 轮巡即时同步 ----------


def load_sync_state():
    if os.path.exists(SYNC_STATE_PATH):
        try:
            with open(SYNC_STATE_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_sync_state(state):
    with open(SYNC_STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def log_sync(msg):
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = '[%s] %s' % (ts, msg)
    try:
        with open(SYNC_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass
    print(line)


def in_sync_window(now=None):
    """是否在轮巡时间段内：08:00 <= hour < 20:00"""
    h = (now or datetime.datetime.now()).hour
    return SYNC_OFF_END <= h < SYNC_OFF_START


def seconds_until_next_window(now=None):
    """休眠到下一个轮巡时间段起点（次日 08:00）的秒数"""
    now = now or datetime.datetime.now()
    nxt = (now + datetime.timedelta(days=1)).replace(
        hour=SYNC_OFF_END, minute=0, second=0, microsecond=0)
    return max(1.0, (nxt - now).total_seconds())


def _pk_str(v):
    return str(v)


def _row_hash(values):
    s = '\x1f'.join('' if v is None else str(v) for v in values)
    return hashlib.md5(s.encode('utf-8')).hexdigest()


def mapping_time_field(mapping):
    """从映射里找映射到 JDY_TIME_KEY 的源字段（即时间字段），找不到返回 None"""
    for src, tgt in zip(mapping.get('source_fields', []), mapping.get('target_fields', [])):
        if tgt == JDY_TIME_KEY:
            return src
    return None


def _time_condition(mapping, time_field):
    """返回 (cond, params)：时间字段 >= SYNC_TIME_FROM 的条件（不带 WHERE 前缀）"""
    if not time_field:
        return '', []
    ident = oracle_ident if is_oracle(mapping['source']) else safe_ident
    f = ident(time_field)
    if is_oracle(mapping['source']):
        return f + ' >= ' + str(int(_parse_start(SYNC_TIME_FROM).year)), []
    return f + ' >= %s', [_parse_start(SYNC_TIME_FROM).strftime('%Y-%m-%d %H:%M:%S')]


def _query_rows(src, database, table, fields, where='', params=None):
    """读源表多行，返回 (col_index, rows)。col_index 按 fields 顺序，row 为 tuple。"""
    ident = oracle_ident if is_oracle(src) else safe_ident
    conn = get_conn(src)
    try:
        cur = conn.cursor()
        sql = 'SELECT %s FROM %s.%s%s' % (
            ','.join(ident(f) for f in fields), ident(database), ident(table), where)
        cur.execute(sql, list(params or []))
        rows = cur.fetchall()
        return {name: i for i, name in enumerate(fields)}, rows
    finally:
        conn.close()


def jdy_list_id_map(data_type_value):
    """拉取简道云该数据类型下所有数据的 {源主键: data_id}"""
    result = {}
    cond = []
    if data_type_value:
        cond.append({'field': JDY_DATA_TYPE_KEY, 'type': 'text',
                     'method': 'eq', 'value': [data_type_value]})
    skip = 0
    limit = 100
    guard = 0
    while True:
        r = requests.post(JDY_BASE + '/app/entry/data/list', headers=JDY_HEADERS,
                          data=__json({'app_id': JDY_APP_ID, 'entry_id': JDY_ENTRY_ID,
                                       'limit': limit, 'skip': skip,
                                       'fields': [JDY_ID_KEY],
                                       'filter': {'rel': 'and', 'cond': cond},
                                       'sort': []}), timeout=60)
        if r.status_code != 200:
            raise HTTPException(500, '简道云数据查询失败: %s' % r.text)
        data = r.json().get('data', [])
        if not data:
            break
        for d in data:
            pk = d.get(JDY_ID_KEY)
            data_id = d.get('_id') or d.get('data_id') or d.get('dataId')
            if pk is not None and data_id:
                result[str(pk)] = data_id
        if len(data) < limit:
            break
        skip += limit
        guard += 1
        if guard > 500:
            log_sync('简道云对账拉取超过 500 页，可能分页参数异常，中断拉取')
            break
    return result


def _create_batch(mapping, rows, col_index, pk_idx, state_rows, existing_map=None):
    """把源表行写入简道云：已存在则 update 覆盖，不存在则 batch_create。
    existing_map 提供已知的 {主键: data_id}（对账时传入，避免重复查询）；为 None 时逐条查重。
    返回 (created, updated)。"""
    source_fields = mapping['source_fields']
    data_type_value = mapping.get('data_type_value', '')
    created = 0
    updated = 0
    records = []
    for row in rows:
        pk_val = _pk_str(row[pk_idx])
        values = [row[col_index[f]] for f in source_fields]
        h = _row_hash(values)
        rec = build_jdy_record(mapping, row, col_index, pk_val)
        if rec is None:
            continue
        data_id = None
        if existing_map is not None:
            data_id = existing_map.get(pk_val)
        else:
            data_id = jdy_find_by_id(pk_val, data_type_value)
        if data_id:
            try:
                jdy_update(data_id, rec)
                state_rows[pk_val] = {'data_id': data_id, 'hash': h}
                updated += 1
            except HTTPException as e:
                log_sync('[%s] 覆盖 pk=%s 失败: %s' % (mapping.get('id'), pk_val, e.detail))
            time.sleep(0.05)
        else:
            records.append((pk_val, h, rec))
    for i in range(0, len(records), JDY_BATCH_SIZE):
        chunk = records[i:i + JDY_BATCH_SIZE]
        resp = jdy_batch_create([c[2] for c in chunk])
        ok_ids = resp.get('success_ids') or []
        for j, (pk_val, h, rec) in enumerate(chunk):
            if j < len(ok_ids):
                state_rows[pk_val] = {'data_id': ok_ids[j], 'hash': h}
        ok = int(resp.get('success_count', 0) or 0)
        created += ok
        if ok < len(chunk):
            log_sync('[%s] batch_create 部分失败：%d/%d 条' % (mapping.get('id'), ok, len(chunk)))
        time.sleep(0.2)
    return created, updated


def _reconcile(mapping, pk_col, fields, source_fields):
    """首次对账：接管简道云已有数据 + 补新增，返回该映射的 state"""
    src = mapping['source']
    database = mapping['database']
    table = mapping['table']
    data_type_value = mapping.get('data_type_value', '')

    jdy_map = jdy_list_id_map(data_type_value)

    time_field = mapping_time_field(mapping)
    cond, cparams = _time_condition(mapping, time_field)
    where = (' WHERE ' + cond) if cond else ''
    col_index, rows = _query_rows(src, database, table, fields, where, cparams)
    pk_idx = col_index[pk_col]

    state_rows = {}
    to_create = []
    max_id = 0
    for row in rows:
        pk_val = _pk_str(row[pk_idx])
        values = [row[col_index[f]] for f in source_fields]
        h = _row_hash(values)
        try:
            n = int(pk_val)
        except (ValueError, TypeError):
            n = 0
        if n > max_id:
            max_id = n
        if pk_val in jdy_map:
            state_rows[pk_val] = {'data_id': jdy_map[pk_val], 'hash': h}
        else:
            to_create.append(row)

    created, _ = _create_batch(mapping, to_create, col_index, pk_idx, state_rows, existing_map=jdy_map)
    reconciled = len(state_rows) - created

    return {
        'max_id': max_id,
        'rows': state_rows,
        'last_sync_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'reconciled': reconciled,
        'created': created,
    }


def _query_new_rows(mapping, fields, pk_col, max_id, time_field=''):
    src = mapping['source']
    database = mapping['database']
    table = mapping['table']
    ident = oracle_ident if is_oracle(src) else safe_ident
    cond, cparams = _time_condition(mapping, time_field)
    conn = get_conn(src)
    try:
        cur = conn.cursor()
        col_list = ','.join(ident(f) for f in fields)
        if is_oracle(src):
            sql = 'SELECT %s FROM %s.%s WHERE %s > :1' % (
                col_list, ident(database), ident(table), ident(pk_col))
            params = [max_id]
            if cond:
                sql += ' AND ' + cond
            cur.execute(sql, params)
        else:
            sql = 'SELECT %s FROM %s.%s WHERE %s > %%s' % (
                col_list, ident(database), ident(table), ident(pk_col))
            params = [max_id]
            if cond:
                sql += ' AND ' + cond
                params += cparams
            cur.execute(sql, params)
        return cur.fetchall()
    finally:
        conn.close()


def _update_by_hash(mapping, fields, col_index, pk_idx, rows_map):
    src = mapping['source']
    database = mapping['database']
    table = mapping['table']
    time_field = mapping_time_field(mapping)
    cond, cparams = _time_condition(mapping, time_field)
    where = (' WHERE ' + cond) if cond else ''
    _, rows = _query_rows(src, database, table, fields, where, cparams)

    updated = 0
    missing = []
    for row in rows:
        pk_val = _pk_str(row[pk_idx])
        entry = rows_map.get(pk_val)
        if not entry:
            missing.append(row)
            continue
        values = [row[col_index[f]] for f in mapping['source_fields']]
        h = _row_hash(values)
        if h == entry['hash']:
            continue
        rec = build_jdy_record(mapping, row, col_index, pk_val)
        if rec is None:
            continue
        try:
            jdy_update(entry['data_id'], rec)
        except HTTPException as e:
            log_sync('[%s] 更新 pk=%s 失败: %s' % (mapping.get('id'), pk_val, e.detail))
            continue
        entry['hash'] = h
        updated += 1
        detail = ', '.join('%s=%s' % (f, to_display(row[col_index[f]]))
                           for f in mapping['source_fields'])
        log_sync('[%s] 修改 pk=%s：%s' % (mapping.get('id'), pk_val, detail))
        time.sleep(0.05)

    created = 0
    if missing:
        created, _ = _create_batch(mapping, missing, col_index, pk_idx, rows_map, existing_map=None)
    return updated, created


def run_one_mapping(mapping):
    """对单套映射执行一轮同步，返回统计结果"""
    mid = mapping.get('id')
    src = mapping['source']
    database = mapping['database']
    table = mapping['table']
    pk_col = get_primary_key(src, database, table)
    source_fields = list(mapping['source_fields'])

    fields = list(source_fields)
    if pk_col not in fields:
        fields.append(pk_col)

    state = load_sync_state()
    st = state.get(mid)

    if not st or st.get('max_id') is None:
        log_sync('[%s] 首次运行，开始对账' % mid)
        st = _reconcile(mapping, pk_col, fields, source_fields)
        state[mid] = st
        save_sync_state(state)
        log_sync('[%s] 对账完成：接管 %d 条，新增 %d 条，max_id=%s' % (
            mid, st.get('reconciled', 0), st.get('created', 0), st.get('max_id')))
        return {'id': mid, 'created': st.get('created', 0), 'updated': 0,
                'reconciled': st.get('reconciled', 0), 'first_sync': True}

    rows_map = st.get('rows', {})
    max_id = st.get('max_id') or 0

    col_index = {name: i for i, name in enumerate(fields)}
    pk_idx = col_index[pk_col]

    created = 0
    updated = 0

    time_field = mapping_time_field(mapping)

    new_rows = _query_new_rows(mapping, fields, pk_col, max_id, time_field)
    if new_rows:
        c, u = _create_batch(mapping, new_rows, col_index, pk_idx, rows_map, existing_map=None)
        created += c
        updated += u
        for row in new_rows:
            try:
                n = int(row[pk_idx])
            except (ValueError, TypeError):
                continue
            if n > max_id:
                max_id = n

    u, c = _update_by_hash(mapping, fields, col_index, pk_idx, rows_map)
    updated += u
    created += c

    st['max_id'] = max_id
    st['rows'] = rows_map
    st['last_sync_time'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    state[mid] = st
    save_sync_state(state)

    log_sync('[%s] 完成：新增 %d，修改 %d' % (mid, created, updated))
    return {'id': mid, 'created': created, 'updated': updated}


def run_mappings(mappings):
    log_sync('开始一轮同步，共 %d 套映射' % len(mappings))
    results = []
    for m in mappings:
        try:
            results.append(run_one_mapping(m))
        except Exception as e:
            log_sync('[%s] 同步失败: %s' % (m.get('id'), e))
            results.append({'id': m.get('id'), 'error': str(e)})
    log_sync('本轮同步结束')
    return results


def run_all_sync():
    mappings = [m for m in load_mappings() if m.get('enabled', True)]
    return run_mappings(mappings)


def get_auto_sync_state():
    with AUTO_SYNC_LOCK:
        return {'all': AUTO_SYNC['all'], 'sources': dict(AUTO_SYNC['sources'])}


def toggle_auto_sync(mode, source=''):
    with AUTO_SYNC_LOCK:
        if mode == 'all':
            AUTO_SYNC['all'] = not AUTO_SYNC['all']
        else:
            AUTO_SYNC['sources'][source] = not AUTO_SYNC['sources'].get(source, False)
        return {'all': AUTO_SYNC['all'], 'sources': dict(AUTO_SYNC['sources'])}


def _mappings_to_run():
    mappings = [m for m in load_mappings() if m.get('enabled', True)]
    st = get_auto_sync_state()
    if st['all']:
        return mappings
    return [m for m in mappings if st['sources'].get(m.get('source'), False)]


def sync_loop():
    while True:
        try:
            if in_sync_window():
                targets = _mappings_to_run()
                if targets:
                    with SYNC_LOCK:
                        run_mappings(targets)
                else:
                    log_sync('本轮无开启自动同步的数据源，跳过')
                time.sleep(SYNC_INTERVAL)
            else:
                secs = seconds_until_next_window()
                log_sync('休眠时间段，睡 %.0f 秒至次日 08:00' % secs)
                time.sleep(secs)
        except Exception as e:
            log_sync('轮巡异常: %s' % e)
            time.sleep(SYNC_INTERVAL)


@app.on_event('startup')
def start_sync_loop():
    t = threading.Thread(target=sync_loop, daemon=True)
    t.start()
    log_sync('轮巡线程已启动（间隔 %d 秒，%02d:00~%02d:00 休眠；默认不自动同步，由前端按钮开启）' % (
        SYNC_INTERVAL, SYNC_OFF_START, SYNC_OFF_END))


class SyncRunRequest(BaseModel):
    mapping_id: Optional[str] = ''


@app.post('/api/sync/run')
def sync_run(req: SyncRunRequest):
    mid = (req.mapping_id or '').strip()
    with SYNC_LOCK:
        if mid:
            mapping = next((m for m in load_mappings() if m.get('id') == mid), None)
            if not mapping:
                raise HTTPException(404, '映射不存在: %s' % mid)
            return {'ok': True, 'result': run_one_mapping(mapping)}
        return {'ok': True, 'result': run_all_sync()}


@app.get('/api/sync/status')
def sync_status():
    state = load_sync_state()
    out = []
    for m in load_mappings():
        st = state.get(m.get('id'))
        out.append({
            'id': m.get('id'),
            'table': m.get('table'),
            'database': m.get('database'),
            'data_type': m.get('data_type_value'),
            'enabled': m.get('enabled', True),
            'max_id': st.get('max_id') if st else None,
            'last_sync_time': st.get('last_sync_time') if st else None,
            'rows': len(st.get('rows', {})) if st else 0,
        })
    return {'syncs': out}


_SYNC_LOG_LINE_RE = re.compile(r'^\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\]\s?(.*)$')


def _tail_lines(path, n):
    """读取文件末尾 n 行（按块倒读，避免整文件载入）"""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b''
            while size > 0 and data.count(b'\n') <= n:
                read = min(block, size)
                size -= read
                f.seek(size)
                data = f.read(read) + data
    except Exception:
        return []
    return data.decode('utf-8', errors='replace').splitlines()[-n:]


def _parse_sync_log_line(line):
    """把一行 sync.log 解析为结构化事件（变更/对账/错误/空闲/信息）"""
    m = _SYNC_LOG_LINE_RE.match(line)
    ts = m.group(1) if m else ''
    body = m.group(2) if m else line
    e = {'time': ts, 'mapping': '', 'kind': 'info', 'created': 0, 'updated': 0,
         'text': body, 'raw': line}
    mm = re.match(r'^\[([^\]]+)\]\s*(.*)$', body)
    inner = body
    if mm:
        e['mapping'] = mm.group(1)
        inner = mm.group(2)
    result = re.search(r'完成[：:]\s*新增\s*(\d+)\s*[，,]\s*修改\s*(\d+)', inner)
    if result:
        e['kind'] = 'result'
        e['created'] = int(result.group(1))
        e['updated'] = int(result.group(2))
    elif ('对账完成' in inner) or inner.startswith('首次运行'):
        e['kind'] = 'reconcile'
    elif '修改 pk=' in inner:
        e['kind'] = 'update'
    elif ('失败' in inner) or ('异常' in inner):
        e['kind'] = 'error'
    elif ('跳过' in inner) or ('休眠' in inner) or ('轮巡线程已启动' in inner) \
            or inner.startswith('开始一轮同步') or ('本轮同步结束' in inner):
        e['kind'] = 'idle'
    return e


@app.get('/api/sync/log')
def sync_log(lines: int = 100):
    """读取 sync.log 末尾内容，返回结构化事件（供前端展示新增/修改）"""
    try:
        n = int(lines)
    except (TypeError, ValueError):
        n = 100
    n = max(1, min(n, 2000))
    if not os.path.exists(SYNC_LOG_PATH):
        return {'entries': [], 'lines': [], 'lines_requested': n, 'mtime': None, 'size': 0,
                'last_round': {'created': 0, 'updated': 0, 'errors': 0}, 'newest_change': None}
    raw = _tail_lines(SYNC_LOG_PATH, n)
    entries = [_parse_sync_log_line(l) for l in raw]
    start = 0
    for i, e in enumerate(entries):
        if e['kind'] == 'idle' and e['text'].startswith('开始一轮同步'):
            start = i
    window = entries[start:]
    created = sum(e['created'] for e in window if e['kind'] == 'result')
    updated = sum(e['updated'] for e in window if e['kind'] == 'result')
    errors = sum(1 for e in window if e['kind'] == 'error')
    newest_change = None
    for e in entries:
        if e['kind'] == 'update' or (e['kind'] == 'result' and (e['created'] or e['updated'])):
            newest_change = e
    try:
        st = os.stat(SYNC_LOG_PATH)
        mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M:%S')
        size = st.st_size
    except Exception:
        mtime, size = None, 0
    return {'entries': entries, 'lines': raw, 'lines_requested': n, 'mtime': mtime,
            'size': size, 'last_round': {'created': created, 'updated': updated, 'errors': errors},
            'newest_change': newest_change}


class SyncResetRequest(BaseModel):
    mapping_id: str = ''


@app.post('/api/sync/reset')
def sync_reset(req: SyncResetRequest):
    mid = (req.mapping_id or '').strip()
    if not mid:
        raise HTTPException(400, '需要 mapping_id')
    with SYNC_LOCK:
        state = load_sync_state()
        existed = mid in state
        if existed:
            del state[mid]
            save_sync_state(state)
    log_sync('[%s] 已重置同步状态（下次同步将重新对账）' % mid)
    return {'ok': True, 'reset': mid, 'existed': existed}


class AutoSyncToggleRequest(BaseModel):
    mode: str = 'single'          # 'single' | 'all'
    source: Optional[str] = ''


@app.get('/api/sync/auto/status')
def sync_auto_status():
    return get_auto_sync_state()


@app.post('/api/sync/auto/toggle')
def sync_auto_toggle(req: AutoSyncToggleRequest):
    if req.mode not in ('single', 'all'):
        raise HTTPException(400, 'mode 只能是 single 或 all')
    if req.mode == 'single' and not req.source:
        raise HTTPException(400, 'single 模式需要 source')

    src = req.source or ''
    cur = get_auto_sync_state()
    will_on = not (cur['all'] if req.mode == 'all' else cur['sources'].get(src, False))

    if will_on and not in_sync_window():
        raise HTTPException(400, '不在同步时间范围内（同步时间：08:00 ~ 20:00）')

    state = toggle_auto_sync(req.mode, src)

    result = None
    if will_on:
        with SYNC_LOCK:
            if req.mode == 'all':
                result = run_all_sync()
            else:
                mappings = [m for m in load_mappings()
                            if m.get('enabled', True) and m.get('source') == src]
                result = run_mappings(mappings)

    return {'all': state['all'], 'sources': state['sources'],
            'turned_on': will_on, 'synced': result}


app.mount('/', StaticFiles(directory='static', html=True), name='static')
