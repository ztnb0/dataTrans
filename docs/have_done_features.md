# 已完成功能记录（have_done_features）

> 本文档记录 dataTrans 工具**已经完成并验证**的功能，与"进行中/计划中"的功能区分开。
> 计划中的功能见 `docs/sync-feature-plan.md`、`docs/oracle-plan.md`；即时同步（轮巡）方案见 `docs/sync-lunXun.md`。

---

## 一、工具概况

dataTrans 是一个 Web 小工具：后台数据库（MySQL / Oracle）→ 简道云表单 的数据传输 / 即时同步工具。

- 后端：Python + FastAPI（`app.py`）
- 前端：单页 HTML（`static/index.html`）
- 运行：`python -m uvicorn app:app --host 0.0.0.0 --port 8088`

---

## 二、已完成功能清单

### 1. 多数据源管理
- 数据源信息在 `config.json` 配置，密码在 `.env`（已加入 `.gitignore`，不提交）。
- 前端左上角「数据源」搜索下拉，可切换多个数据源；支持「+ 添加数据源」新增、「数据源管理」增删改。
- 支持 `mysql` 和 `oracle` 两种类型（按 `type` 字段分派）。
- 当前配置 4 个数据源：`dpt-182`（MySQL 测试库）、`dpt-aliyun`（大平台后台系统，MySQL 线上只读）、`oracle-clazz`（教务系统，Oracle）、`user-src-1789010399`（飞霞系统，MySQL 用户添加）。

### 2. 三级结构浏览（可搜索）
- 数据源 → 数据库（Oracle 为 schema）→ 表 → 字段，逐级加载。
- 「数据库」「表」均为**可搜索下拉框**（输入关键词实时过滤）。
- 字段列表展示字段名、类型、注释。

### 3. 字段映射 + 自动勾选
- 勾选源字段后，自动与简道云字段配对，右侧可下拉调整对应字段。
- **智能默认映射**：按字段名/类型自动猜（`*number`/`*code`→项目编号、`*name`→项目名称、时间字段→平台数据创建时间）。
- **按已保存映射自动勾选**：选完数据源 + 库 + 表后，若 `mappings.json` 里已有该表的映射，自动勾选字段、填好目标字段/转换规则/平台/数据类型，省得手动找。

### 4. 传输前预览 + 确认
- 点「开始传输」先进入预览（dry-run，不写库）。
- 预览界面显示：源表总数据条数、筛选后条数、本次将传输条数。
- 预览表格展示**映射后的数据**（表头为简道云字段名）。
- **逐行勾选排除**：默认全选，取消勾选 = 不传输；提供全选/全不选、分页。
- 确认无误后点「确认传输」才真正写入简道云。

### 5. 时间筛选
- 手动传输：勾选时间字段后，映射区下方出现「起始时间」选择器（年月日三级，Oracle 数字年份只显示「年」）。
- 轮巡同步：后台统一按 `SYNC_TIME_FROM='2026'` 过滤，只同步 2026 年及之后的数据（对账/新增/修改检测均生效）。

### 6. 数据预览
- 「预览源数据」：查看源表前 5 条。
- 「查看表单已有数据」：表格形式预览简道云表单已有数据。

### 7. 映射持久化（`mappings.json`）
- 前端「保存映射」把「源表字段 → 简道云字段」映射（含 `source_fields`/`target_fields`/`transforms`/`platform_value`/`data_type_value`）写入 `mappings.json`。
- 按 id 覆盖或追加；当前 7 套映射（见第四节）。
- 轮巡同步只处理 `enabled=true` 的映射。

### 8. 平台/数据类型自动填充（`auto_fill_rules.json`）
- 前端「规则管理」维护各数据源的「平台 / 数据类型」自动填充规则（`source_name` + `match` 库/schema/表 → value）。
- 选数据源 + 库/表时自动填「平台」「数据类型」输入框；无规则则清空（避免残留上一个源的值）。
- 保存映射时这两个值写进 `mappings.json`，同步/传输时写入简道云的「平台」「数据类型」字段。

### 9. 轮巡即时同步（后台自动）
- 后台常驻线程每 5 分钟一轮，只在 08:00~20:00 运行（夜间休眠到次日 08:00）。
- **前端两个按钮控制**：左栏「预览源数据」旁「**单源同步**」「**全部同步**」，点开启时**先立即同步一次**再进入自动轮巡，按钮文字在「同步/暂停」间切换；默认关闭，重启后关闭。
- **首次对账**：拉简道云已有数据（按「源表主键 + 数据类型」匹配），已有的接管（记 `data_id` + hash），缺失的补新增。
- **增量检测**：新增=主键递增 `主键 > max_id`；修改=轻量全量 hash 对比（`mtime` 无权限，全部走 hash）。
- **查重覆盖**：写入简道云前按「主键 + 数据类型」查重，已存在则 `update` 覆盖，不存在才 `batch_create`，避免重复。
- 同步状态存 `sync_state.json`（账本：`max_id`/`rows[主键].data_id/hash`/`last_sync_time`），日志写 `sync.log`。

### 10. MySQL 数据源支持
- `pymysql` 连接，`SHOW DATABASES` / `SHOW TABLES` / `SHOW FULL COLUMNS` 等方言。
- `information_schema` 取主键。
- 支持 `LIMIT` 分页、`%s` 占位符、反引号标识符。
- 字符集 `gbk`（中文正常）。

### 11. Oracle 数据源支持
- `oracledb`（thick 模式）+ Oracle Instant Client 21。
- 元数据方言：`all_tables` / `all_tab_columns` / `all_col_comments` / `all_constraints`。
- 支持多 schema（`BFEC`/`SHFEC`/`GZFEC` 的 `CLAZZ`），`ROWNUM` 分页、`:1` 占位符、双引号标识符。
- 字符集 `ZHS16GBK`；`BEGIN_YEAR`（NUMBER 年份）自动按年份处理。

### 12. 简道云数据写入
- 批量新增 `batch_create`（每批 ≤100 条，自动分批 + 限速）。
- 单条更新 `update`（需 `data_id`）。
- 字段值用 `{"value": ...}` 包裹；datetime 转字符串格式。
- 每条记录固定带：id（源表主键）、数据类型、平台 + 业务字段（含转换规则）。

### 13. 安全与配置
- 数据库密码、简道云 APIKey 全部移入 `.env`，`.env` 加入 `.gitignore`。
- `.env.example` 提供模板。
- `safe_ident` / `oracle_ident` 校验标识符防注入。
- `config.json` 数据源不含密码；`sync_state.json`、`sync.log` 等运行时产物也加入 `.gitignore`。

---

## 三、关键约束（实测确认）

| 项 | 情况 |
|---|---|
| 简道云 `batch_create` | ✅ 可用，返回 `success_ids`（顺序对应） |
| 简道云 `update` | ✅ 可用，需 `data_id` |
| 简道云 `batch_update` | ⚠️ 只能把多条改成同一固定值 |
| 简道云 `delete` / `batch_delete` | ❌ 未授权（源表删除无法同步） |
| 简道云 list 分页 | ✅ 用 `limit` + `skip` 分页拉取（实测有效） |
| Oracle 版本 | 10g（10.2.0.5.0），需 Instant Client 21 + thick 模式 |
| Oracle 表归属 | `BFEC`/`SHFEC`/`GZFEC` 三个 schema，登录用户 `clazz` 下无表 |
| Oracle 账号权限 | 仅 `CREATE SESSION`（无时间戳字段，走 hash 对比） |
| 阿里云只读库 `dptep` | 字段级 SELECT 授权：映射字段能查，但 `SELECT *` 及 `groupbatch.mtime` 无权限（故全表 hash 对比，不用 mtime） |

---

## 四、字段映射关系（当前已配置）

### 简道云表单字段（6 个）

| 字段 | 简道云 key | 说明 |
|---|---|---|
| id | `_widget_1789030761638` | 存源表主键（定位匹配键之一） |
| 数据类型 | `_widget_1789030761637` | 固定值，每张表唯一（定位匹配键之一） |
| 平台 | `_widget_1788918827859` | 固定值 |
| 项目编号 | `_widget_1788918827860` | 业务字段 |
| 项目名称 | `_widget_1788918827861` | 业务字段 |
| 平台数据创建时间 | `_widget_1788918827862` | 业务字段（也是时间筛选字段） |

### 各数据源映射（7 套）

| 数据源 | 表 | 主键 | 平台 / 数据类型 | 源字段 → 简道云字段 |
|---|---|---|---|---|
| 飞霞系统 | db_plan_positon | PlanPositonID | 飞霞系统 / 飞霞 | PlanPositonID→id、ExamPlanName→项目编号(batch_number)、ExamAreaName→项目名称(city_name)、ExamDate→时间 |
| 大平台后台系统 | coaching_class | id | 大平台后台系统 / 大平台班级 | id→id、class_name→项目名称、class_ctime→时间、class_number→项目编号 |
| 大平台后台系统 | groupbatch | id | 大平台后台系统 / 大平台期次 | id→id、batchName→项目名称、createDate→时间、classnumber→项目编号 |
| 教务系统 | CLAZZ（BFEC） | CLASS_ID | 教务系统 / 教务北京 | CLASS_ID→id、CLASS_CODE→项目编号、CLASS_NAME→项目名称、BEGIN_YEAR→时间 |
| 教务系统 | CLAZZ（SHFEC） | CLASS_ID | 教务系统 / 教务上海 | 同上 |
| 教务系统 | CLAZZ（GZFEC） | CLASS_ID | 教务系统 / 教务广州 | 同上 |
| 182测试数据库 | coaching_class | id | 测试平台 / 测试班级 | id→id、class_name→项目名称、class_ctime→时间、class_number→项目编号 |

---

## 五、数据文件

| 文件 | 性质 | 谁写入 | 功能 |
|---|---|---|---|
| `mappings.json` | 配置 | 前端「保存映射」 | 字段映射关系（同步/传输的依据） |
| `auto_fill_rules.json` | 配置 | 前端「规则管理」 | 平台/数据类型自动填充规则 |
| `sync_state.json` | 状态（运行时） | 后台轮巡自动维护 | 同步账本：`max_id` / `rows[主键].data_id/hash` / `last_sync_time` |
| `sync.log` | 日志 | 后台轮巡 | 同步结果与报错记录 |

详见 `docs/数据文件说明.md`。

---

## 六、环境依赖

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

## 七、Git 分支与提交

- 当前分支：`feature/大平台后台-即时同步`
- 已完成提交（按时间）：
  - `edee1b6` 数据迁移工具：MySQL多数据源 → 简道云（预览确认、字段映射、时间筛选）
  - `54dd7ac` 密码脱敏：密码与 APIKey 移入 .env，新增 README
  - `7063957` 新增 Oracle 数据源支持开发计划文档
  - `fcda32c` 更新 Oracle 计划（实测探测结果）
  - `c0e3fa2` 实现 Oracle 数据源支持
  - `e609d24` 预览显示平台字段、时间筛选只保留起始时间
  - `baec92b` 新增表单信息即时同步功能任务文档
  - `6e7d29e` 补充：不做 CDC 而用轮询的原因
  - `12f668a` 即时同步回调接口（mappings + notify）+ 前端数据类型字段
  - `5970edf` 合并 feature/自定义添加数据源
  - `8bdd097` 接口回调即时同步方案实现（mappings + notify-batch + 数据源管理 + 自动填充规则），后续暂停改轮巡

> 注：轮巡即时同步方案（单源/全部同步按钮、对账、hash 对比、查重覆盖、时间筛选等）的代码与文档已完成，尚未提交。

---

## 八、未完成 / 计划中 / 已暂停

以下功能**尚未实现或已暂停**，不要与已完成功能混淆：

- **接口回调同步**（`POST /api/sync/notify`、`/api/sync/notify-batch`）— 已暂停（代码保留），改用轮巡，见 `docs/sync-lunXun.md`；
- **Oracle 支持的后续完善** — 见 `docs/oracle-plan.md`；
- **CDC 实时同步** — 已搁置（Oracle 10g 版本限制，详见 sync-feature-plan.md 补充章节）。
