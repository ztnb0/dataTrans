# dataTrans 数据迁移工具

后台 MySQL 数据库 → 简道云表单的数据传输工具。支持多数据源、字段映射、时间筛选、预览确认、逐行勾选排除。

## 快速开始

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8088
```

浏览器打开 http://127.0.0.1:8088

## 添加 / 修改数据源

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

先在命令行 Ctrl+C 停掉正在运行的服务，再重新执行：

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8088
```

## 目录结构

```
app.py          # 后端（FastAPI + MySQL + 简道云 API）
config.json     # 数据源列表（不含密码）
.env            # 数据库密码（本地，不提交）
.env.example    # 密码配置模板
static/         # 前端页面
```
