# Temperature Monitor

[![Tests](https://github.com/neilchen-dev/temperature-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/neilchen-dev/temperature-monitor/actions/workflows/tests.yml)

面向生产现场环境监控场景的轻量级数字化项目：通过 **Home Assistant + Python/Flask + REST API + 飞书多维表格**，将温湿度传感器数据、设备在线状态、现场作业登记、环境点检与异常记录串成一套可追溯的监测流程。

> 本仓库中的设备编号、区域名称、业务表单和展示数据均已脱敏或使用 Demo 数据。生产环境中的客户、人员、内部区域与平台凭据不包含在公开仓库中。

## 项目亮点

- **IoT 数据接入**：Home Assistant 读取 Xiaomi Home 温湿度传感器状态，变化时自动上报。
- **后端服务**：Flask 提供 `POST /temperature` 接口，负责设备校验、单位转换与在线状态处理。
- **业务系统集成**：通过飞书开放 API 写入多维表格，实现现场数据实时更新与留痕。
- **状态判定**：支持由 Home Assistant 明确上报的在线/离线状态；离线上报不会覆盖最后一次有效温湿度。
- **定时历史快照**：Home Assistant 每十分钟调用一次 `POST /history/sample`，后端将实时总表的 11 台设备完整快照分别写入历史表，并按采集时间去重。
- **现场闭环**：多维表格 Demo 中包含监测记录、异常事件、作业登记、区域/点检等业务对象，并通过 Dashboard 汇总关键状态。
- **工程化运行**：Token 缓存与刷新、429/5xx/超时重试、CSV 历史记录、轮转日志、Waitress 与 Docker 部署。

## 业务 Demo

公开展示版使用 `DEV-xx`、`Area-x`、`Storage-x` 等匿名标识重建现场环境监控场景，主要包含：

1. **实时监测台账**：设备编号、温湿度、更新时间、在线状态及区域映射；
2. **环境受控作业登记**：现场人员提交当前工艺、区域、监测点与备注，监测数据由系统侧自动记录；
3. **仓库环境点检**：现场确认监测系统、报警与仓储状态，无需重复手工抄录温湿度；
4. **环境监控 Dashboard**：汇总超限点位、监测点总数、在线点位及各点位温湿度与控制区间。

这部分用于展示 **“业务需求 → 数据模型 → 自动采集 → 状态判定 → 表单/点检 → Dashboard”** 的完整数字化链路，而非公开生产环境数据。

## 预览

| 监测数据表 | 环境监控面板 |
| --- | --- |
| ![Demo monitoring records](docs/images/bitable-records.png) | ![Demo dashboard](docs/images/dashboard.png) |

| 现场作业登记 | 仓库环境点检 |
| --- | --- |
| ![Field work log](docs/images/field-work-log.png) | ![Warehouse inspection](docs/images/field-work-log-detail.png) |

## Docker Compose 部署（Home Assistant Container）

部署者只需准备 Docker Desktop（Windows/macOS）或 Docker Engine（Linux）和自己的飞书应用凭据。项目不包含任何真实凭据。

1. 克隆仓库后，将 [`.env.example`](.env.example) 复制为 `.env`；填写 `APP_ID`、`APP_SECRET`、`APP_TOKEN`、`TABLE_ID` 和随机生成的 `HISTORY_API_KEY`。程序默认按飞书表的“设备编号”字段自动识别 `record_id`。
2. Windows 用户双击或在 PowerShell 中运行：

   ```powershell
   .\deploy.ps1
   ```

   首次运行会自动创建 `.env` 模板；填写后再次运行即可启动。

3. Linux/macOS 或任意终端运行：

   ```bash
   docker compose pull
   docker compose up -d --remove-orphans --wait --wait-timeout 120
   ```

部署完成后访问 `http://localhost:5000/health`，返回 `{"status":"ok",...}` 即表示服务已启动。Compose 默认使用版本化镜像 `1.2.0`；以后升级时先将 `IMAGE_TAG` 改为目标版本，再重复“拉取 + 启动”命令。生产环境不要只依赖 `latest`。

Home Assistant Container 不使用 Add-on 内部主机名。`rest_command` 中的 `<temperature-monitor-host>` 按 HA 容器网络模式填写：

- HA 使用 `network_mode: host`：填 `127.0.0.1`。
- HA 使用普通 bridge 网络：填 Docker 主机的局域网 IP，例如 `192.168.1.10`；不能填 `localhost`，因为它只代表 HA 容器自身。
- HA 与本服务加入同一个自定义 Docker 网络：填容器名 `temperature-monitor`。

服务端口已由 Compose 映射为宿主机的 `${PORT:-5000}`。部署 HA 配置前，先从 HA 容器所在网络确认 `http://<temperature-monitor-host>:5000/health` 可访问。

## 可选：Home Assistant Add-on

仅适用于 **Home Assistant OS / Supervised**；Home Assistant Container 用户可忽略本节。已配置为使用阿里云容器镜像：

```text
crpi-7apex3hoo0i4alz2.cn-hongkong.personal.cr.aliyuncs.com/noef-temperature/temperature-monitor:1.2.0
```

1. 在 Home Assistant 中打开 **设置 → Add-ons → Add-on Store → 右上角菜单 → Repositories**。
2. 添加仓库：`https://github.com/neilchen-dev/temperature-monitor`。
3. 在 Add-on Store 选择 **Temperature Monitor**，点击 **Install**。
4. 在 Add-on 的 **Configuration** 页面填写飞书凭据和至少 32 字节的 `history_api_key`；默认使用 `device_id_field`（`设备编号`）自动识别每台设备的 `record_id`，点击 **Save** 后 **Start**。

如设备字段名称不是“设备编号”，将 `device_id_field` 改为你的字段名。如果 Home Assistant 上报的设备名与飞书表中的设备编号不同，使用 `device_name_map` 做名称映射，例如：

```json
{"sensor.warehouse_temp":"DEV-01","sensor.warehouse_humidity":"DEV-02"}
```

`device_record_map` 仍可选填，用于固定覆盖个别设备的自动识别结果：

```json
{"DEV-01":"recxxxxxxxxxxxx","DEV-02":"recyyyyyyyyyyyy"}
```

Add-on 启动后，Home Assistant 自动化应通过 Add-on 的**内部主机名**访问 `http://<addon-hostname>:5000/temperature`；健康检查为 `/health`。自定义 GitHub 仓库安装的 Add-on 内部主机名由 Home Assistant 按仓库生成，并非可靠的固定 `local-temperature-monitor`。请在下方 REST 命令中替换 `<addon-hostname>` 为当前安装实例分配的内部主机名。HA Container（无 Supervisor）不支持 Add-on，请使用上方 Docker Compose 部署方式。

## 系统架构

```mermaid
flowchart TD
    sensor["小米温湿度计 / Demo DEV-xx"] --> integration["Xiaomi Home 集成"]
    integration --> entity["Home Assistant 温度/湿度实体"]
    entity --> automation["状态变化触发 Automation"]
    entity --> historyAutomation["每10分钟历史采样 Automation"]
    automation --> rest["rest_command.python_test"]
    historyAutomation --> historyRest["rest_command.temperature_history_sample"]
    rest -->|"POST /temperature"| api["Temperature Monitor / Flask"]
    historyRest -->|"POST /history/sample"| api
    api --> validate["设备校验、单位转换、在线状态处理"]
    validate --> feishu["飞书多维表格 / Dashboard"]
    validate --> csv["CSV 月度历史"]
    api --> log["轮转日志"]
```

1. Home Assistant 在温度、湿度或在线状态改变时调用 `POST /temperature`。
2. 服务校验设备与数值，并依据 `SOURCE_TEMPERATURE_UNIT` 将温度统一为摄氏度。
3. 最新状态写入飞书多维表格，同时保留月度 CSV 和日志；离线事件只更新状态，不覆盖最后一次有效读数。
4. 每逢整十分钟，后端读取实时总表并向 TH-01 至 TH-11 历史表各写一条同时间点快照；重复请求不会重复写入。

## 界面预览

- [`docs/images/`](docs/images/)：监测记录、现场登记表单与仪表盘预览截图。

## 仓库结构

```text
homeassistant/       Home Assistant REST 命令与自动化示例
hassio/              Home Assistant Add-on 清单与说明
routes/              HTTP 路由与响应处理
services/            校验、飞书通信、令牌、重试与本地存储
docs/images/         README 预览截图
app.py               Flask 应用、日志和 Waitress 启动入口
config.py            环境变量、设备映射与运行目录配置
Dockerfile           容器化运行配置
```

## 本地运行

建议使用 Python 3.12：

```powershell
git clone https://github.com/neilchen-dev/temperature-monitor.git
cd temperature-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:APP_ID="your_feishu_app_id"
$env:APP_SECRET="your_feishu_app_secret"
$env:APP_TOKEN="your_bitable_app_token"
$env:TABLE_ID="your_table_id"
python app.py
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:5000/health
```

## Docker Compose 说明

```bash
cp .env.example .env
# 编辑 .env 后执行
docker compose pull
docker compose up -d --remove-orphans --wait --wait-timeout 120
```

Compose 默认直接拉取已发布的 ACR `1.2.0` 镜像，无需在本地构建。更新容器建议执行 `docker compose pull` 后再执行 `docker compose up -d --remove-orphans --wait --wait-timeout 120`。CSV 历史与日志分别持久化到本地 `data/` 和 `logs/` 目录。停止服务请执行 `docker compose down`。凭据只能保存在 `.env` 或密钥管理服务中，切勿提交该文件。

如需回滚，在 `.env` 中设置上一稳定版本（例如 `IMAGE_TAG=1.0.3`），然后重新执行拉取和启动命令。`HISTORY_CLEANUP_ENABLED` 只从部署配置读取；第一阶段必须为 `false`，修改后需要重建容器才会生效。

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `APP_ID` | 是 | — | 飞书应用 ID |
| `APP_SECRET` | 是 | — | 飞书应用密钥 |
| `APP_TOKEN` | 是 | — | 飞书多维表格 App Token |
| `TABLE_ID` | 是 | — | 飞书数据表 ID |
| `HISTORY_API_KEY` | 使用历史采样时是 | — | `POST /history/sample` 的共享密钥，至少 32 字节 |
| `HISTORY_TABLE_MAP` | 使用历史采样时是 | 空 | JSON 格式的 TH-01～TH-11 到历史表 ID 的完整映射 |
| `HISTORY_INTERVAL_MINUTES` | 否 | `10` | 历史采样去重时间桶分钟数；需与 HA 自动化一致 |
| `HISTORY_TIMEZONE` | 否 | `Asia/Shanghai` | 历史采样与清理截止日使用的时区 |
| `HISTORY_CLEANUP_ENABLED` | 否 | `false` | 是否真正删除过期记录；第一阶段必须保持 `false` |
| `HISTORY_RETENTION_DAYS` | 否 | `90` | 历史记录保留天数 |
| `HISTORY_CLEANUP_HOUR` | 否 | `2` | 每日首次执行清理检查的本地小时 |
| `DEVICE_RECORD_MAP` | 否 | 空 | JSON 格式的设备名到飞书 record ID 手动覆盖，例如 `{"DEV-01":"recxxx"}` |
| `DEVICE_ID_FIELD` | 否 | `设备编号` | 自动识别 record ID 时，在飞书表中匹配设备名的字段 |
| `DEVICE_NAME_MAP` | 否 | 空 | JSON 格式的 Home Assistant 上报名到飞书设备编号映射，例如 `{"sensor.warehouse_temp":"DEV-01"}` |
| `SOURCE_TEMPERATURE_UNIT` | 否 | `C` | 上报温度单位：`F` 或 `C` |
| `HOST` | 否 | `0.0.0.0` | HTTP 监听地址 |
| `PORT` | 否 | `5000` | HTTP 监听端口 |
| `USE_SYSTEM_PROXY` | 否 | `false` | 飞书请求是否读取系统代理 |
| `REQUEST_TIMEOUT_SECONDS` | 否 | `10` | 单次 HTTP 请求超时秒数 |
| `REQUEST_RETRY_TIMES` | 否 | `3` | HTTP 请求最大尝试次数 |
| `WAITRESS_THREADS` | 否 | `4` | Waitress 工作线程数 |
| `DATA_DIR` | 否 | `/app/data`（Add-on 环境为 `/data`） | CSV 与 SQLite 数据目录 |
| `LOG_DIR` | 否 | `/app/logs` | 日志目录 |
| `SQLITE_ENABLED` | 否 | `true` | 是否启用 SQLite 本地镜像（温度上报与历史快照的本地副本，不影响飞书主链路） |
| `SQLITE_DB_PATH` | 否 | `${DATA_DIR}/temperature_monitor.db` | SQLite 数据库文件路径 |

## SQLite 本地镜像与查询 API

服务在写入飞书的同时，将数据镜像到本地 SQLite（WAL 模式，默认 `data/temperature_monitor.db`）。设计原则：

- **飞书多维表格仍是唯一事实源**，SQLite 只是本地副本；初始化或写入失败只记日志并累加失败计数，绝不影响飞书链路、去重与清理逻辑；
- `SQLITE_ENABLED=false` 可整体关闭并快速回退；
- `history_snapshots` 以 `(设备, 采集时间)` 为复合主键，天然幂等；`temperature_reports` 是原始事件日志，允许 HA 重试产生重复行；
- 空温湿度为 NULL：设备离线（`online_status=离线`）与在线但数值不可解析两种情况通过 `online_status` 区分；
- 当前并发假设为**单进程单实例**（Waitress 多线程由内部锁保护）；数据库文件与 `-wal`/`-shm` 均落在同一 `data/` 持久卷内。

### 只读查询与统计接口

三个接口均需 `X-History-Key` 请求头（与历史采样共享密钥），镜像未启用时返回 503。

```bash
# 查询快照明细：支持 device / start / end（epoch 秒、毫秒或 ISO 8601）/ limit
curl -H "X-History-Key: $KEY" \
  "http://127.0.0.1:5000/history/query?device=TH-01&start=2026-08-18T00:00:00%2B08:00&limit=500"

# 每日统计：每设备每天的平均/最小/最大温湿度、离线次数、超限次数（离线样本不计入超限）
curl -H "X-History-Key: $KEY" \
  "http://127.0.0.1:5000/history/stats/daily?device=TH-01&days=7"

# 设备总览：最后快照值、快照数、上报次数、离线时长估算（离线样本数 × 采样间隔）
curl -H "X-History-Key: $KEY" "http://127.0.0.1:5000/history/stats/devices"
```

`GET /health` 的响应中包含 `sqlite` 对象（`enabled`、`write_failures`、两张表的行数），行数也可作为镜像是否落后于飞书的粗略指标。

### 可视化看板

浏览器访问 `http://127.0.0.1:5000/dashboard?key=<HISTORY_API_KEY>&days=7`（`days` 可选，1～90），页面由服务端渲染，包含：

- **超限与离线趋势**：按天汇总的温度/湿度超限次数与离线次数；
- **各设备平均温湿度**：窗口期内每设备平均温度（°C）与平均湿度（%RH）双轴对比；
- **设备总览表**：最后快照值、在线状态、快照数、上报次数、最后快照/上报时间、估算离线时长。

注意：`key` 通过 URL query string 传递，**会出现在浏览器历史、书签以及反向代理/网关的 access log 中**。请勿分享带 key 的链接，生产环境建议将 key 放在只有你访问的受控设备上打开，或为 dashboard 配置独立密钥。离线时长为估算值（离线样本数 × 采样间隔），漏采或停机会导致偏差。图表依赖 CDN 的 Chart.js，加载失败时图表区域显示占位提示，总览表不受影响。

## Home Assistant 配置

参考 [`homeassistant/rest_command.yaml`](homeassistant/rest_command.yaml)、[`homeassistant/automation_th01.example.yaml`](homeassistant/automation_th01.example.yaml) 和 [`homeassistant/automation_history_sample.yaml`](homeassistant/automation_history_sample.yaml)。将 `homeassistant/secrets.example.yaml` 中的密钥项复制到 HA 的 `secrets.yaml`，并确保它与服务端 `HISTORY_API_KEY` 完全一致。

```yaml
rest_command:
  python_test:
    # HA 使用 host 网络时填 127.0.0.1；bridge 网络填 Docker 主机局域网 IP；
    # 两个容器共享自定义网络时填 temperature-monitor。
    url: "http://<temperature-monitor-host>:5000/temperature"
    method: POST
    headers:
      Content-Type: application/json
    payload: >
      {
        "device": "{{ device }}",
        "temperature": "{{ temperature }}",
        "humidity": "{{ humidity }}"
      }

  temperature_history_sample:
    url: "http://<temperature-monitor-host>:5000/history/sample"
    method: POST
    headers:
      Content-Type: application/json
      X-History-Key: !secret temperature_monitor_history_api_key
    payload: "{}"
    timeout: 120
```

请求体示例：

```json
{
  "device": "DEV-01",
  "temperature": 24.6,
  "humidity": 52.0,
  "status": "online"
}
```

若未传 `status`，后端会按在线处理。默认 `SOURCE_TEMPERATURE_UNIT=C`；如果上游发送华氏温度，请将它设置为 `F`。

历史采样接口读取实时总表中的区域、温湿度、在线状态、温湿度判定、工艺、综合判定、作业状态和警报状态，再分别写入 11 张历史表。同一十分钟时间桶重复调用会被跳过。第一阶段 `HISTORY_CLEANUP_ENABLED=false` 时会在任何过期记录筛选或删除 API 调用之前直接短路；观察稳定 24～48 小时并人工确认后，才能修改配置并重启服务启用清理。
