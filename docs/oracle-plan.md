# Oracle 数据源支持 — 开发计划

## 目标
在现有多数据源框架上新增 `oracle` 类型数据源，与 MySQL 并存，前端切换数据源即可使用。

## 现状与差距
当前后端按 MySQL 写死，Oracle 需在以下层面差异化：

| 功能 | 现状（MySQL） | Oracle 实现 |
|---|---|---|
| 驱动 | `pymysql` | `oracledb`（见下方"驱动/模式"结论） |
| 连接 | `pymysql.connect(host,port,user,password,charset)` | `oracledb.connect(user, password, dsn="host:1521/ORCL")` |
| 列库 | `SHOW DATABASES` | schema（用户）当"库"处理，三级层级 |
| 列表 | `SHOW TABLES FROM db` | `SELECT table_name FROM ALL_TABLES WHERE owner=:1` |
| 列字段 | `SHOW FULL COLUMNS` | `ALL_TAB_COLUMNS` + `ALL_COL_COMMENTS` |
| 主键 | `information_schema.COLUMNS` | `ALL_CONSTRAINTS` + `ALL_CONS_COLUMNS` |
| 分页 | `LIMIT n` | 待确认 Oracle 版本后定（`FETCH FIRST n ROWS ONLY` 或 `ROWNUM`） |
| 占位符 | `%s` | `:1`（oracledb） |
| 标识符 | 反引号 `` ` `` | 双引号 `"`（Oracle 默认大写） |
| 字符集 | `charset=gbk` | 需处理（见"字符集"结论） |

## 关键结论（已实际探测）

### 1. 驱动与连接模式
- 已安装 `oracledb==4.0.2`。
- 实测 thin 模式（纯 Python）连接 `114.113.151.60:1521/ORCL` **失败**，报 `DPY-3010: connections to this database server version are not supported by python-oracledb in thin mode`。
- **结论**：该 Oracle 服务器版本较老，thin 模式不支持，必须使用 **thick 模式**，且需安装 **Oracle Instant Client**（本机当前未安装，`ORACLE_HOME` 为空）。
- 实施前需先解决 Instant Client 安装，否则无法连接。

### 2. Oracle 版本
- 因 thin 模式连不上，无法直接探测 `v$version`。
- 由 DPY-3010 判断：服务器版本低于 thin 模式支持下限（低于 12.1，大概率 11g 或更早）。
- 需在安装 Instant Client 后用 thick 模式重新探测确认。

### 3. 字符集
- 需要处理。Oracle 端中文编码（`NLS_LANG` / 数据库字符集如 `AL32UTF8` 或 `ZHS16GBK`）与 Python 端不一致时，预览会乱码。
- 计划在连接时显式设置 `encoding` / `nencoding`（如 `oracledb.connect(..., encoding="UTF-8", nencoding="UTF-8")`），并以探测到的 `NLS_CHARACTERSET` 为准调整。

## 已确认的设计决策
1. **层级**：三级（schema/用户 当"库" → 表 → 字段），前端沿用现有三级结构。
2. **表归属**：默认 `CLAZZ` schema（用户 `clazz` 对应 schema `CLAZZ`），后续如需跨 schema 再扩展。
3. **字符集**：需要处理（见上）。
4. **版本**：待 thick 模式可用后探测。

## 实施步骤

### 1. 前置依赖
- 安装 Oracle Instant Client（Basic 包），并配置 `oracledb.init_oracle_client(lib_dir=...)` 或系统环境变量。
- `pip install oracledb`（已完成）。

### 2. 配置扩展 `config.json`
```json
{
  "id": "oracle-clazz",
  "type": "oracle",
  "name": "Oracle 线上库",
  "host": "114.113.151.60",
  "port": 1521,
  "user": "clazz",
  "service": "ORCL"
}
```
- 密码走 `.env`，key = `DB_ORACLE_CLAZZ_PASSWORD`（沿用命名规则）。
- MySQL 源补 `"type": "mysql"`，后端默认 mysql 以兼容旧配置。

### 3. 后端方言抽象
- 新增 `dialects/` 层（或单文件分派），按 `type` 走不同实现：
  - `get_conn(source_id)` → pymysql / oracledb 连接
  - `list_databases / list_tables / list_columns / get_primary_key / preview / _read_rows / build_time_where`
  - 每处 SQL 按 type 分派（MySQL 版 vs Oracle 版）
- `safe_ident` 区分：Oracle 用 `"`，标识符大写。

### 4. 前端
- 基本无需大改（前端只面对统一抽象）。
- 三级层级下前端不变。

### 5. 测试
- 用真实连接做只读验证（`list_tables` / `list_columns` / `preview`），确认中文不乱码后再放开传输。

## 待办 / 阻塞项
- [ ] 安装 Oracle Instant Client（阻塞，无它无法 thick 模式连接）
- [ ] thick 模式下重新探测 Oracle 版本、`NLS_CHARACTERSET`、`CLAZZ` 表清单
- [ ] 确定分页写法（取决于版本）
- [ ] 中文乱码验证与处理

## 建议分支流程
```bash
git checkout main
git pull origin main
git checkout -b feature/oracle
# ...开发、提交...
git push -u origin feature/oracle
# 完成后合并回 main
```
