# Oracle 数据源支持 — 开发计划

## 目标
在现有多数据源框架上新增 `oracle` 类型数据源，与 MySQL 并存，前端切换数据源即可使用。

## 现状与差距
当前后端按 MySQL 写死，Oracle 需在以下层面差异化：

| 功能 | 现状（MySQL） | Oracle 实现 |
|---|---|---|
| 驱动 | `pymysql` | `oracledb`（thick 模式） |
| 连接 | `pymysql.connect(host,port,user,password,charset)` | `oracledb.connect(user, password, dsn="host:1521/ORCL", encoding="GBK", nencoding="UTF-8")` |
| 列库 | `SHOW DATABASES` | schema（用户）当"库"处理，三级层级 |
| 列表 | `SHOW TABLES FROM db` | `SELECT table_name FROM ALL_TABLES WHERE owner=:1` |
| 列字段 | `SHOW FULL COLUMNS` | `ALL_TAB_COLUMNS` + `ALL_COL_COMMENTS` |
| 主键 | `information_schema.COLUMNS` | `ALL_CONSTRAINTS` + `ALL_CONS_COLUMNS` |
| 分页 | `LIMIT n` | `ROWNUM`（10g 无 `FETCH FIRST`） |
| 占位符 | `%s` | `:1`（oracledb） |
| 标识符 | 反引号 `` ` `` | 双引号 `"`（Oracle 默认大写） |
| 字符集 | `charset=gbk` | `encoding="GBK"`（见"字符集"结论） |

## 关键结论（已实际探测确认）

### 1. 服务器与客户端版本
- **服务器：Oracle Database 10g (10.2.0.5.0)**，非常老。
- **客户端：Instant Client 21.15**（`D:\Program Files\OracleInstantClient\instantclient_21_15`，已下载并解压）。
- 必须用 **thick 模式**：`oracledb.init_oracle_client(lib_dir=<instantclient路径>)`。
  - thin 模式报 `DPY-3010`（10g 不支持 thin）。
  - 原 12.2 客户端报 `DPI-1050`（oracledb 4.x 要求客户端 ≥19.1）。
- 已解决：装了 VS2013 运行库（`msvcr120.dll`，Instant Client 12.2 需要；21.15 用 `vcruntime140.dll` 已具备）。

### 2. 数据库结构（重要）
- 登录用户 `CLAZZ` 下**没有表**（0 张）。
- 实际表分布在其他 schema：`BFEC`（3张）、`GZFEC`（1张）、`SHFEC`（1张）。
- 核心表：**`BFEC.CLAZZ`**（班级表，对应 MySQL 的 `coaching_class`）。
- 因此"库"这一级应映射为 **schema（BFEC/GZFEC/SHFEC）**，而非"数据库"或用户。

### 3. 字符集
- 数据库字符集：**`ZHS16GBK`**（中文 GBK），NCHAR 字符集 `AL16UTF16`。
- 连接时需 `oracledb.connect(..., encoding="GBK", nencoding="UTF-8")`，否则中文乱码。
- 验证方式：读取 `all_col_comments` 的中文注释（已实测能取到"收款人""机构名称"等）。

### 4. 目标表字段参考（BFEC.CLAZZ）
- 关键字段：`CLASS_CODE`（班级编码）、`CLASS_NAME`（班级名称）、`BEGIN_DATE`（开课日期）、`CREATE_DATE`、`CLASS_ID`（主键，NUMBER）。
- 字段命名风格与 MySQL 的 `coaching_class`（`class_number`/`class_name`）不同，映射关系需单独确认。

## 已确认的设计决策
1. **层级**：三级（**schema 当"库"** → 表 → 字段），前端沿用现有三级结构。
2. **表归属**：默认 `BFEC` schema（核心班级表 `BFEC.CLAZZ`），"库"下拉列出 BFEC/GZFEC/SHFEC。
3. **字符集**：`encoding="GBK"` + `nencoding="UTF-8"`。
4. **版本**：Oracle 10g，分页用 `ROWNUM`。

## 实施步骤

### 1. 前置依赖（已完成）
- ✅ `pip install oracledb`（4.0.2）
- ✅ Instant Client 21.15 已解压到 `D:\Program Files\OracleInstantClient\instantclient_21_15`
- ✅ VS2013 / VS2015 运行库已具备

### 2. 配置扩展 `config.json`
```json
{
  "id": "oracle-bfec",
  "type": "oracle",
  "name": "Oracle 线上库(BFEC)",
  "host": "114.113.151.60",
  "port": 1521,
  "user": "clazz",
  "service": "ORCL",
  "instant_client": "D:/Program Files/OracleInstantClient/instantclient_21_15"
}
```
- 密码走 `.env`，key = `DB_ORACLE_BFEC_PASSWORD`（沿用命名规则）。
- MySQL 源补 `"type": "mysql"`，后端默认 mysql 以兼容旧配置。

### 3. 后端方言抽象
- 新增 `dialects/` 层（或单文件分派），按 `type` 走不同实现：
  - `get_conn(source_id)` → pymysql / oracledb 连接
  - `list_databases / list_tables / list_columns / get_primary_key / preview / _read_rows / build_time_where`
  - 每处 SQL 按 type 分派（MySQL 版 vs Oracle 版）
- `safe_ident` 区分：Oracle 用 `"`，标识符大写。
- Oracle 分页用 `ROWNUM`：`SELECT * FROM (...) WHERE ROWNUM <= n`。

### 4. 前端
- 基本无需大改（前端只面对统一抽象）。
- 三级层级下前端不变。

### 5. 测试
- 用真实连接做只读验证（`list_tables` / `list_columns` / `preview`），确认中文不乱码后再放开传输。

## 待办 / 阻塞项
- [x] 安装 Oracle Instant Client 21.15（已完成）
- [x] thick 模式探测版本、字符集、表清单（已完成）
- [x] 确定分页写法（10g → ROWNUM）
- [ ] 字段映射关系确认（BFEC.CLAZZ 与简道云字段的对应）
- [ ] 中文乱码验证与处理（GBK，已定位方案）

## 部署说明（别人主机使用怎么办）

**核心问题**：Oracle 连接依赖 Instant Client（本地 DLL）+ VS 运行库，别人主机没有就无法连 Oracle。

**方案 A：随项目分发 Instant Client（推荐）**
- 把 Instant Client 目录放进项目（或独立压缩包），`config.json` 里写相对路径。
- 后端 `oracledb.init_oracle_client(lib_dir=<相对路径>)`。
- 别人拿到代码 + Instant Client 目录即可用，无需系统级安装。

**方案 B：文档说明，各自安装**
- README 写明需要安装 Instant Client 21+ 和 VS2015 运行库，`config.json` 填各自路径。

**方案 C：混合（推荐落地方式）**
- `config.json` 的 `instant_client` 字段写路径，留空则尝试：
  1. 环境变量 `ORACLE_CLIENT_LIB`
  2. 项目内置的 `instantclient/` 目录
  3. 系统 PATH 里的 oci.dll
- 纯 MySQL 用户**完全不装 Instant Client 也不影响**（只有选了 oracle 源才 init client，且失败时给清晰报错）。

> 建议：Oracle 支持做成**惰性初始化**——不选 Oracle 源就不加载 oracledb thick 模式，避免所有用户都必须装客户端。

## 建议分支流程
```bash
git checkout main
git pull origin main
git checkout -b feature/oracle
# ...开发、提交...
git push -u origin feature/oracle
# 完成后合并回 main
```
