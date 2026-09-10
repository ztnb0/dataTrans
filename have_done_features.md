# 已完成功能记录（have_done_features）

> 本文档记录 dataTrans 工具**已经完成并验证**的功能，与"进行中/计划中"的功能区分开。
> 计划中的功能见 `docs/sync-feature-plan.md`、`docs/oracle-plan.md`。

---

## 一、工具概况

dataTrans 是一个 Web 小工具：后台数据库（MySQL / Oracle）→ 简道云表单 的数据传输工具。

- 后端：Python + FastAPI（`app.py`）
- 前端：单页 HTML（`static/index.html`）
- 运行：`python -m uvicorn app:app --host 127.0.0.1 --port 8088`

---

## 二、已完成功能清单

### 1. 多数据源管理
- 数据源信息在 `config.json` 配置，密码在 `.env`（已加入 `.gitignore`，不提交）。
- 前端左上角「数据源」搜索下拉，可切换多个数据源。
- 支持 `mysql` 和 `oracle` 两种类型（按 `type` 字段分派）。
- 当前配置了 3 个数据源：`dpt-182`（MySQL 测试库）、`dpt-aliyun`（MySQL 线上只读库）、`oracle-clazz`（Oracle 线上库）。

### 2. 三级结构浏览（可搜索）
- 数据源 → 数据库（Oracle 为 schema）→ 表 → 字段，逐级加载。
- 「数据库」「表」均为**可搜索下拉框**（输入关键词实时过滤）。
- 字段列表展示字段名、类型、注释。

### 3. 字段映射
- 勾选源字段后，自动与简道云字段配对，右侧可下拉调整对应字段。
- **智能默认映射**：按字段名/类型自动猜（`*number`/`*code`→项目编号、`*name`→项目名称、时间字段→平台数据创建时间）。
- 字段映射对应简道云表单 4 个字段：平台、项目编号、项目名称、平台数据创建时间。

### 4. 传输前预览 + 确认
- 点「开始传输」先进入预览（dry-run，不写库）。
- 预览界面显示：源表总数据条数、筛选后条数、本次将传输条数。
- 预览表格展示**映射后的数据**（表头为简道云字段名）。
- **逐行勾选排除**：默认全选，取消勾选 = 不传输；提供全选/全不选、分页。
- 确认无误后点「确认传输」才真正写入简道云。

### 5. 时间筛选
- 勾选时间字段后，映射区下方出现「起始时间」选择器。
- 年月日三级下拉，不选日=按月初，不选月=按年初。
- Oracle 数字年份字段（如 `BEGIN_YEAR`）只显示「年」一级。
- 截止日期默认显示今天。

### 6. 数据预览
- 「预览源数据」：查看源表前 5 条。
- 「查看表单已有数据」：表格形式预览简道云表单已有数据。

### 7. MySQL 数据源支持
- `pymysql` 连接，`SHOW DATABASES` / `SHOW TABLES` / `SHOW FULL COLUMNS` 等方言。
- `information_schema` 取主键。
- 支持 `LIMIT` 分页、`%s` 占位符、反引号标识符。
- 字符集 `gbk`（中文正常）。

### 8. Oracle 数据源支持
- `oracledb`（thick 模式）+ Oracle Instant Client 21。
- 元数据方言：`all_tables` / `all_tab_columns` / `all_col_comments` / `all_constraints`。
- **多 schema 一键合并**：`BFEC` / `SHFEC` / `GZFEC` 三个 schema 的 `CLAZZ` 表合并读取（合计 9481 条）。
- `ROWNUM` 分页、`:1` 占位符、双引号标识符。
- 字符集 `ZHS16GBK`（中文正常）。
- `BEGIN_YEAR`（NUMBER 年份）自动转 `01-01` 日期写入简道云。

### 9. 简道云数据写入
- 批量新增 `batch_create`（每批 ≤100 条，自动分批 + 限速）。
- 字段值用 `{"value": ...}` 包裹。
- datetime 转 UTC ISO 格式（北京时间减 8 小时）。
- 平台字段（手写固定值）写入。

### 10. 安全与配置
- 数据库密码、简道云 APIKey 全部移入 `.env`，`.env` 加入 `.gitignore`。
- `.env.example` 提供模板。
- `safe_ident` / `oracle_ident` 校验标识符防注入。
- `config.json` 数据源不含密码。

---

## 三、关键约束（实测确认）

| 项 | 情况 |
|---|---|
| 简道云 `batch_create` | ✅ 可用，返回 `success_ids` |
| 简道云 `update` | ✅ 可用，需 `data_id` |
| 简道云 `batch_update` | ⚠️ 只能改固定值 |
| 简道云 `delete` / `batch_delete` | ❌ 未授权（用户无删除权限） |
| Oracle 版本 | 10g（10.2.0.5.0），需 Instant Client 21 + thick 模式 |
| Oracle 表归属 | `BFEC`/`SHFEC`/`GZFEC` 三个 schema，登录用户 `clazz` 下无表 |
| Oracle 账号权限 | 仅 `CREATE SESSION` |

---

## 四、字段映射关系（当前已配置）

### 简道云表单字段（4 个）
| 字段 | 简道云 key |
|---|---|
| 平台 | `_widget_1788918827859`（手写固定值） |
| 项目编号 | `_widget_1788918827860` |
| 项目名称 | `_widget_1788918827861` |
| 平台数据创建时间 | `_widget_1788918827862` |

### 各数据源映射

| 源表 | 项目编号 | 项目名称 | 平台数据创建时间 |
|---|---|---|---|
| MySQL `coaching_class`（班级） | `class_number` | `class_name` | `class_ctime` |
| MySQL `groupbatch`（期次） | `classnumber` | `batchName` | `createDate` |
| Oracle `CLAZZ`（班级） | `CLASS_CODE` | `CLASS_NAME` | `BEGIN_YEAR`（年份→01-01） |

---

## 五、环境依赖

| 依赖 | 说明 |
|---|---|
| Python 3.14 | 运行环境 |
| fastapi / uvicorn | Web 框架 |
| pymysql | MySQL 驱动 |
| requests | 简道云 API |
| oracledb | Oracle 驱动 |
| Oracle Instant Client 21 | Oracle thick 模式（路径在 `config.json` 配置） |
| VS2013 运行库 | Instant Client 依赖（`msvcr120.dll`） |

---

## 六、Git 分支与提交

- 当前分支：`feature/表单数据的即时同步功能`
- 已完成提交（按时间）：
  - `edee1b6` 数据迁移工具：MySQL多数据源 → 简道云（预览确认、字段映射、时间筛选）
  - `54dd7ac` 密码脱敏：密码与 APIKey 移入 .env，新增 README
  - `7063957` 新增 Oracle 数据源支持开发计划文档
  - `fcda32c` 更新 Oracle 计划（实测探测结果）
  - `c0e3fa2` 实现 Oracle 数据源支持
  - `e609d24` 预览显示平台字段、时间筛选只保留起始时间
  - `baec92b` 新增表单信息即时同步功能任务文档
  - `6e7d29e` 补充：不做 CDC 而用轮询的原因

---

## 七、未完成 / 计划中

以下功能**尚未实现**，仅记录于计划文档，不要与已完成功能混淆：

- **表单信息即时同步**（增量同步、映射持久化、对账、轮询）— 见 `docs/sync-feature-plan.md`
- **Oracle 支持的后续完善** — 见 `docs/oracle-plan.md`
- **CDC 实时同步** — 已搁置（Oracle 10g 版本限制，详见 sync-feature-plan.md 补充章节）
