# dataTrans 数据迁移工具

后台 MySQL 数据库 → 简道云表单的数据传输工具。支持多数据源、字段映射、时间筛选、预览确认、逐行勾选排除，以及基于回调的即时同步。

## 生产环境（公司内网服务器）

已部署在公司内网 Linux 服务器上，内网浏览器直接访问：

```
http://192.168.6.138:8088
```

- 服务器：Ubuntu 24.04（ARM64），应用目录 `/home/spark1/dataTrans`。
- 由 systemd 服务 `datatrans` 托管，开机自启、崩溃自动重启。
- 4 个数据源（含 Oracle 教务系统）与简道云均已配好，无需重新配置。

```bash
# 常用运维命令（在服务器上执行）
sudo systemctl status datatrans     # 查看状态
sudo systemctl restart datatrans    # 重启（改 config.json/.env/mappings.json 后）
sudo journalctl -u datatrans -f     # 实时看日志
```

> 完整的部署记录、使用说明、注意事项见 `docs/部署上线文档.md`。

## 快速开始（本地开发）

```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8088
```

本机浏览器打开 http://localhost:8088

## 局域网访问（让其他主机访问）

1. **监听所有网卡**（关键）：启动时必须用 `--host 0.0.0.0`（不要用 `127.0.0.1`，否则只有本机能访问）。
2. **放行防火墙端口**：以**管理员身份**打开 PowerShell 执行：
   
   ```powershell
   New-NetFirewallRule -DisplayName "dataTrans 8088" -Direction Inbound -Protocol TCP -LocalPort 8088 -Action Allow
   ```
3. **查本机局域网 IP**：
   
   ```powershell
   ipconfig
   ```
   找到 `IPv4 地址`（形如 `192.168.x.x`，WLAN/以太网那个，忽略 vEthernet 虚拟网卡）。
4. **其他主机访问**：`http://<你的IP>:8088`

> 注意：IP 是 DHCP 动态分配的，会变。想让地址长期稳定，建议在系统网络设置里改成**静态 IP**，或向网管申请固定 IP。

## 添加 / 修改数据源

> 现在也可以在前端左上角点「＋ 添加数据源」按钮新增（密码写入 `.env`，保存后立即生效，无需重启）。以下为手工编辑方式。

1. 编辑 `config.json`，在 `sources` 数组里增改数据源（**不要写 password**）：
   ```json
   {
     "sources": [
       {
         "id": "dpt-182",
         "name": "182测试数据库",
         "host": "192.168.2.182",
         "port": 3306,
         "user": "dpt_dev",
         "charset": "gbk"
       }
     ]
   }
   ```
   - `id`：唯一标识，不能重复
   - `name`：前端数据源下拉里显示的名字
   - `charset`：数据库编码，常用 `gbk` 或 `utf8mb4`

2. 在 `.env` 里配置对应密码（`.env` 已加入 `.gitignore`，不会提交）：
   ```
   DB_DPT_182_PASSWORD=你的密码
   ```
   命名规则：`DB_` + 数据源 id（`-` 换成 `_`，大写）+ `_PASSWORD`。
   例：`id=dpt-aliyun` → `DB_DPT_ALIYUN_PASSWORD`。

   首次使用可参考 `.env.example` 模板。

3. **重启后端服务**（改配置后必须重启才生效，光刷新前端无效）。

## 重启服务

- **生产服务器**：`sudo systemctl restart datatrans`
- **本地开发**：先在命令行 Ctrl+C 停掉正在运行的服务，再重新执行：

```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8088
```

## 目录结构

```
app.py          # 后端（FastAPI + MySQL + 简道云 API）
config.json     # 数据源列表（不含密码）
mappings.json   # 即时同步的字段映射配置
.env            # 数据库密码（本地，不提交）
.env.example    # 密码配置模板
static/         # 前端页面
docs/           # 设计文档
```

## API 接口

### 元数据（前端三级浏览用）

| 方法 / 路径 | 说明 |
|---|---|
| `GET /api/sources` | 数据源列表 |
| `GET /api/sources/{id}` | 单个数据源完整信息（不含密码） |
| `POST /api/sources` | 添加数据源 |
| `PUT /api/sources/{id}` | 修改数据源（密码留空则保留原密码） |
| `DELETE /api/sources/{id}` | 删除数据源（含内置，同时清 `.env` 密码） |
| `POST /api/sources/test` | 测试数据源连接 |
| `GET /api/databases?source=...` | 数据库（Oracle 为 schema）列表 |
| `GET /api/tables?source=...&database=...` | 表列表 |
| `GET /api/columns?source=...&database=...&table=...` | 字段列表 + 主键 |
| `GET /api/preview?source=...&database=...&table=...&limit=5` | 预览源表前 N 条 |

### 简道云

| 方法 / 路径 | 说明 |
|---|---|
| `GET /api/jdy/fields` | 简道云表单字段（name=key、label=中文名） |
| `GET /api/jdy/preview?limit=20` | 预览简道云表单已有数据 |

### 手动传输

| 方法 / 路径 | 说明 |
|---|---|
| `POST /api/transfer/preview` | 传输前预览（dry-run，不写库） |
| `POST /api/transfer` | 真正写入简道云（`batch_create`） |

请求体（`TransferRequest`）：

```json
{
  "source": "dpt-aliyun",
  "database": "dptep",
  "table": "coaching_class",
  "source_fields": ["class_number", "class_name", "class_ctime"],
  "target_fields": ["_widget_1788918827860", "_widget_1788918827861", "_widget_1788918827862"],
  "platform_value": "教务系统",
  "time_field": "class_ctime",
  "date_from": "",
  "date_to": "",
  "limit": 0,
  "excluded_ids": null
}
```

### 即时同步（回调）

对方业务系统在写库后主动调用本工具接口，实现数据即时同步到简道云。详见 `docs/功能：创建即时同步接口.md`。

| 方法 / 路径 | 说明 |
|---|---|
| `GET /api/mappings` | 读取所有映射 |
| `POST /api/mappings` | 保存/更新一套映射 |
| `POST /api/sync/notify` | 核心回调接口（回查 + 新增/修改同步） |

#### `POST /api/sync/notify`

请求体：

```json
{
  "table": "coaching_class",
  "pk": "123",
  "database": ""
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `table` | string | 源表名（须已在 `mappings.json` 配置） |
| `pk` | string | 该行主键值 |
| `database` | string（可选） | 数据库/schema；MySQL 可省略，Oracle 必填 |

响应：

| 场景 | 响应体 |
|---|---|
| 新增成功 | `{"ok": true, "action": "create", "pk": "123"}` |
| 修改成功 | `{"ok": true, "action": "update", "pk": "123"}` |
| 行不存在 | `{"ok": true, "skipped": "row_not_found"}` |
| 参数/映射错误 | `{"detail": "..."}` |

#### `mappings.json` 结构

```json
[
  {
    "id": "coaching-class",
    "source": "dpt-aliyun",
    "database": "dptep",
    "table": "coaching_class",
    "platform_value": "大平台后台系统",
    "data_type_value": "大平台后台班级表单类型---大平台班级",
    "source_fields": ["class_number", "class_name", "class_ctime"],
    "target_fields": ["_widget_1788918827860", "_widget_1788918827861", "_widget_1788918827862"],
    "enabled": true
  }
]
```

- `source_fields` / `target_fields`：源字段 → 简道云字段 key 一一对应；
- `platform_value` / `data_type_value`：写入简道云的固定值（平台、数据类型）；
- `data_type_value` 参与匹配，必须每张表唯一且非空（如"班级"/"期次"）；
- 源表主键 id 自动写入简道云「id」字段（`_widget_1789030761638`），与 `data_type_value` 组成唯一匹配键。
