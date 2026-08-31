# temperature-monitor 项目全解

> 本文档按**真实调用链**组织，而不是按目录组织。所有结论都以代码为证，引用格式 `文件:行号`，可直接跳转核对。
> 写作基线：main 分支 `9b8d35e`（2026-08-23），Python 3.12，约 6600 行 Python，129 个测试。

---

## 目录

1. [项目一页纸](#1-项目一页纸)
2. [总体架构与三条数据链路](#2-总体架构与三条数据链路)
3. [目录地图](#3-目录地图)
4. [启动链路：app.py](#4-启动链路apppy)
5. [配置层：config.py](#5-配置层configpy)
6. [写链路一：HA 温度上报 /temperature](#6-写链路一ha-温度上报-temperature)
7. [写链路二：历史快照 /history/sample](#7-写链路二历史快照-historysample)
8. [写链路三：Modbus 后台采集](#8-写链路三modbus-后台采集)
9. [读链路：/api、/history、/dashboard](#9-读链路api-history-dashboard)
10. [飞书服务层：锁、缓存、重试、幂等](#10-飞书服务层锁缓存重试幂等)
11. [HTTP 基础设施：http_client 与 token](#11-http-基础设施http_client-与-token)
12. [数据库层：db.py](#12-数据库层dbpy)
13. [统一设备模型与事件状态机](#13-统一设备模型与事件状态机)
14. [前端看板：dashboard](#14-前端看板dashboard)
15. [部署与 CI/CD](#15-部署与-cicd)
16. [测试策略](#16-测试策略)
17. [关键设计决策 12 讲（面试叙事）](#17-关键设计决策-12-讲面试叙事)
18. [已知边界（诚实清单）](#18-已知边界诚实清单)
19. [面试问题地图](#19-面试问题地图)

---

## 1. 项目一页纸

**是什么**：一套部署在 Linux 服务器（Docker Compose）和 Home Assistant Add-on 两种环境下的温湿度监控系统。多个温湿度设备的数据最终汇入**飞书多维表格（Bitable）**作为业务事实源，同时在本地维护 **SQLite 镜像**用于查询、统计和看板。

**数据从哪来**（三个入口）：

| 入口 | 协议 | 触发方式 | 代码入口 |
|---|---|---|---|
| Home Assistant webhook | HTTP POST | 设备状态变化时 HA 主动推 | `routes/temperature.py:36` |
| 历史快照采样 | HTTP POST | HA 定时器每 10 分钟调一次 | `routes/history.py:18` |
| 工业设备直采 | Modbus TCP / RTU(RS485) | 后台线程每 5 秒轮询 | `services/collector.py:41` |

**数据到哪去**：飞书多维表格（实时表 + 每设备一张历史表）、SQLite 镜像（4 张表）、CSV 月度文件、本地看板（Chart.js）。

**技术栈**：Python 3.12 / Flask 3（工厂模式 + 5 个 Blueprint）/ waitress（生产 WSGI，4 线程）/ SQLite（WAL）/ requests / pymodbus 3.15（TCP+串口）/ pyserial / Docker + GitHub Actions（测试 → 构建 → 阿里云 ACR → SSH 部署 → 健康检查 → 自动回滚）。

**规模感**：单进程多线程架构；11 个 HA 设备 + 1 个 Modbus 设备；采样间隔 10 分钟（历史）/ 5 秒（Modbus）；数据量级为"每天每设备 144 条历史快照"——这是所有技术选型（SQLite、单进程、无消息队列）的前提。

---

## 2. 总体架构与三条数据链路

```text
┌─────────────────┐   POST /temperature    ┌──────────────────────────────┐
│ Home Assistant   │ ──────────────────────► │ routes/temperature.py        │
│ (webhook 推送)   │                         │  校验 → 飞书实时表更新        │
└─────────────────┘                         │  → SQLite镜像 → 统一模型      │
                                            └──────────┬───────────────────┘
┌─────────────────┐   POST /history/sample             │
│ HA 定时器        │ ──────────────────────► ┌──────────▼───────────────────┐
│ (每10分钟)       │   routes/history.py     │ services/feishu.py           │
└─────────────────┘   → services/history.py  │  token缓存/重试/按资源加锁    │
                      读实时表 → 写11张历史表 │  record_id 正/负缓存         │
                      → SQLite镜像 → 清理     └──────────┬───────────────────┘
                                                 HTTPS │ tenant_access_token
┌─────────────────┐  Modbus TCP:5020 / RTU          ▼
│ PLC / RS485变送器│ ◄────────────────────── ┌──────────────────┐
│ (FC03/FC04)     │   每5秒轮询              │ 飞书开放平台       │
└─────────────────┘  services/modbus_client  │ (Bitable 事实源)  │
       │             .py 后台线程            └──────────────────┘
       ▼
┌──────────────────────────────┐
│ services/devices.py           │   统一设备模型（隔离边界）
│  record_sample()              │──► db.device_samples（(device,source)身份）
│  ↘ services/events.py         │──► db.device_events（仅状态转移）
└──────────────────────────────┘

读侧：/api/devices /api/events /api/system/status /history/query
      /history/stats/* /dashboard  ──全部读 SQLite 镜像，不查飞书
```

三条写链路、一条读链路。**核心不变式：飞书是业务事实源，SQLite 只是镜像**——镜像的任何故障都只降级本地功能，绝不影响飞书写入主链路（这是全项目最重要的一条隔离原则，见 §12）。

---

## 3. 目录地图

```text
app.py                     # 唯一生产入口：create_app + run_server（§4）
config.py                  # 全部配置：HA options.json → 环境变量 → 默认值（§5）
requirements.txt           # Flask/waitress/requests/pymodbus<3.16/pyserial
routes/                    # 5 个 Blueprint（HTTP 层，无业务逻辑）
  temperature.py           #   POST /temperature + GET /health
  history.py               #   POST /history/sample
  analytics.py             #   GET /history/query /history/stats/*
  api.py                   #   GET /api/devices /api/events /api/system/status
  dashboard.py             #   GET /dashboard（服务端渲染 HTML）
services/                  # 业务层
  feishu.py                #   飞书 API 封装：锁/缓存/重试（§10）
  token.py                 #   tenant_access_token 缓存（§11）
  http_client.py           #   requests 会话 + 重试 + 指数退避（§11）
  history.py               #   历史采样编排：分桶/去重/清理（§7）
  db.py                    #   SQLite 镜像：4 张表 + 迁移（§12）
  devices.py               #   统一设备模型 record_sample（§13）
  events.py                #   纯函数状态转移评估（§13）
  collector.py             #   采集线程生命周期（§8）
  modbus_client.py         #   Modbus 端点/映射/轮询器（§8）
  storage.py               #   CSV 月度文件
  validator.py             #   数值校验 + 华氏转摄氏
tools/                     # 独立运行的运维工具，不进生产镜像主进程
  modbus_probe.py          #   单次读寄存器 / 显式 --scan 扫从站
  modbus_simulator.py      #   本地 Modbus TCP 模拟设备
tests/                     # 129 个测试；helpers.py 起真实 pymodbus TCP server
.github/workflows/         # tests.yml（PR+push）→ deploy.yml（workflow_run 门控）
```

---

## 4. 启动链路：app.py

完整执行顺序（`python app.py`，Dockerfile CMD）：

```text
模块顶层执行
├── import config          # config.py 顶层立刻执行：读 HA options + 环境变量（§5）
├── import routes/services # 只是装载定义
├── app = create_app()     # app.py:58 —— import 时也会执行，每进程一次
│   ├── configure_logging()      # app.py:18  RotatingFileHandler + 控制台；幂等（handlers 判重）
│   ├── db.init_db()             # app.py:47  建 4 张表 + 列迁移；失败只停用镜像（§12）
│   ├── Flask(__name__)
│   └── register_blueprint ×5    # app.py:50-54
└── if __name__ == "__main__":   # app.py:87  import 时为假，直接运行才为真
    └── run_server()             # app.py:61
        ├── collector.start_collectors()   # 起采集线程（进程内唯一一次，§8）
        ├── waitress.serve(app, threads=4) # 阻塞
        └── finally: stop_collectors()     # serve 返回/抛异常时收尾
```

**四个关键点**：

1. **`app = create_app()`（`app.py:58`）在 import 时就执行**。守卫只包住了 `run_server()`。所以 `import app` 并非零副作用——它会建目录、初始化日志、建表，但这些副作用全部幂等；被严格排除的只有"起线程、开网络服务"。
2. **`create_app()` 保持无副作用（不起线程）**，契约写在 `services/collector.py:5-11`。否则 CI 一跑测试就长出真实采集线程。
3. **三层防线防止线程重复启动**：`__main__` 守卫（结构层）→ `collector.py:34-49` 的 `_started` 标志 + `_start_lock`（幂等层，防 check-then-act 竞态）→ `.env.example` 部署约定（多 worker 时只在一个 worker 开 `MODBUS_ENABLED`，运维层）。
4. **已知边界**：容器里 python 是 PID 1 且未注册 SIGTERM 处理器，`docker stop` 时 `finally` 不会执行（内核对 PID 1 忽略未安装处理器的终止信号），宽限期后 SIGKILL 强杀。SQLite WAL 已提交事务不受影响，损失仅是"没主动 close Modbus 连接"。详见 §18。

**为什么 waitress 而不是 Flask 内置 server / gunicorn**：Flask 内置 `app.run()` 是单线程开发服务器，明确不能用于生产；gunicorn 在 HA Add-on（单容器单进程）环境里没有收益，反而引入多 worker 重复采集问题（每个 worker 都会执行模块级代码）。waitress 是纯 Python、生产级、多线程（`WAITRESS_THREADS=4`）、无 worker fork 语义——与"单进程采集"的架构约束精确匹配。

---

## 5. 配置层：config.py

**三级配置源，优先级从高到低**（`config.py:12-79`）：

```text
Home Assistant Add-on options.json (/data/options.json)
    ↓ os.environ.setdefault()  ← 注意：不覆盖已有环境变量
环境变量
    ↓ 默认值
代码内默认（fail-safe：Modbus 默认关闭）
```

关键设计：

- **`os.environ.setdefault()` 而非赋值**（`config.py:60`）：HA 的 options 只填空缺，显式设置的环境变量优先。这让 Add-on 和裸 Docker 部署共用同一套代码。
- **Add-on 环境自动切换数据目录**（`config.py:121-125`）：容器内 `/app/data` 升级即丢，HA Supervisor 的 `/data` 才持久。检测到 options.json 存在即默认切到 `/data`。
- **类型化读取器 fail-fast**：`_get_int/_get_float`（`config.py:89-106`）遇到非法值直接抛 `ValueError`，带"哪个变量、当前值是什么"的错误消息——配置错误在启动瞬间暴露，而不是运行到一半才发现。
- **两类校验时机**：结构校验（设备映射的 JSON 格式、去重，`config.py:169-256`）在 import 时执行；Modbus 寄存器映射故意**惰性解析**（`config.py:293` 只存原始字符串，`MODBUS_ENABLED=true` 且真正构造 poller 时才 parse，非法只禁用采集不影响启动，`collector.py:76-82`）。
- **安全默认**：`MODBUS_ENABLED=false`（未开启时行为与旧版完全一致）、`HISTORY_CLEANUP_ENABLED=false`（删除类功能默认硬关）、鉴权 key 默认空但读 API 未配 key 返回 503（见 §9）。

---

## 6. 写链路一：HA 温度上报 /temperature

`routes/temperature.py:36-131`，逐跳：

```text
POST /temperature  {device, temperature, humidity, status?}
│
├─ 1 鉴权（temperature.py:22-33）
│    TEMPERATURE_API_KEY 为空 → 放行（兼容旧部署）
│    非空 → 必须带 X-Temperature-Key（或 X-History-Key），hmac.compare_digest 比较
│    ※ compare_digest 而不是 ==：防时序侧信道逐字节猜测密钥
│
├─ 2 请求体校验（:42-48）
│    get_json(silent=True) → 非 dict 400；device 缺失 400；upper() 归一化
│    MAX_CONTENT_LENGTH=16KB（app.py:49）挡异常大 payload
│
├─ 3 身份映射（:53-55）
│    DEVICE_NAME_MAP: HA 设备名 → 飞书设备名（同一物理设备两种命名不拆身份）
│    DEVICE_RECORD_MAP: 手动指定 record_id（可选）
│    resolve_record_id(): 未手动配置则查飞书表自动识别（§10 详述）
│
├─ 4 分支（:56-74）
│    离线 → 只更新飞书"在线状态"字段，保留最后温湿度（不写 0 污染历史）
│    在线 → validator.normalize_temperature/humidity:
│           华氏→摄氏（SOURCE_TEMPERATURE_UNIT）、范围校验（-50~100°C / 0~100%）、
│           保留两位小数；非法值抛 ValueError → 400
│         → update_feishu_fields(record_id, {温度/湿度/在线状态/更新时间毫秒})
│
├─ 5 飞书失败处理（:79-81, :108-115）
│    网络异常/非 0 code → 502 返回给 HA（HA 会重试 webhook）
│
├─ 6 本地镜像（:87-94）
│    save_history(): SQLite temperature_reports 追加 + CSV 月度文件（storage.py:15）
│    ※ 无论飞书成败都记（审计用途），temperature_reports 是 append-only 事件日志
│
└─ 7 统一设备模型（:100-106）
     devices.record_sample(source="home_assistant", ...)
     内部吞掉所有异常 → 绝不影响原上报链路（隔离边界，§13）
```

**这条链路的知识密度在"顺序"**：先飞书后镜像——飞书是事实源，镜像失败可以容忍；反过来就会出现"本地有、云端没有"的假数据。

---

## 7. 写链路二：历史快照 /history/sample

HA 每 10 分钟调一次 `POST /history/sample`（`routes/history.py:18` → `services/history.py:278 sample_history`）。这一段是全项目**幂等设计最密集**的地方。

### 7.1 采样主流程（history.py:278-370）

```text
POST /history/sample（带 X-History-Key）
│
├─ 鉴权三层（history.py:20-45）
│    未配 HISTORY_API_KEY → 503；key 错 → 401；validate_history_config()
│    再校验设备表/时区/保留天数一致性 → 503
│
├─ 防并发重入（:280-284）
│    _sample_run_lock.acquire(blocking=False) 拿不到锁 → 202 already_running
│    ※ HA 的 HTTP 客户端超时重试可能造成两个采样并发；非阻塞获取 =
│      第二个请求立刻返回而不是排队 double 写
│
├─ 时间分桶（:104-112 floor_sample_time）
│    当前时间向下取整到 HISTORY_INTERVAL_MINUTES 的整数倍
│    例：10:47 → 10:40（以配置时区的当地时间为准）
│    ※ 同一个 10 分钟窗口内无论触发几次，目标"采集时间"都相同 —— 幂等的根基
│
├─ 读飞书实时总表（:290-291）
│    list_realtime_snapshots() 分页拉全部记录，_index_realtime_snapshots
│    校验：设备编号不允许重复、11 台设备一台不能少（:184-207）
│
└─ 逐设备写历史表（:297-322）
     ├─ 去重第一层：_latest_sample_cache 内存缓存每设备最后采集时间，
     │   latest >= sample_time → skip（:300-309）
     │   ※ 避免每 10 分钟都对 11 张表各发一次"查最新"请求
     ├─ 去重第二层：create_history_record 带 client_token（uuid）查询参数
     │   （feishu.py:295-305）—— 飞书侧幂等令牌，同 token 重试不会插两条
     ├─ 写成功 → db.save_history_snapshot() 落 SQLite 镜像
     │   （PK=(device, sample_time_ms)，INSERT OR REPLACE —— 第三层幂等）
     └─ 单设备失败只记 failures 字典，不中断其余设备（:320-322）
```

**返回码语义**（`:330-338`）：全部成功 200 / 部分失败 207 / 全部失败 502。207 是"多状态"的正确 HTTP 语义，HA 侧可据此决定是否重试。

### 7.2 数据清理（history.py:214-275 run_cleanup_if_due）

挂在采样流程尾部，但受**独立的功能开关**控制：

- `HISTORY_CLEANUP_ENABLED` 默认 false，且**HTTP 请求无法覆盖**——只有重启读配置才可能改变（防误开）。
- 时间门（`:222`）：只在 `sample_time.hour >= HISTORY_CLEANUP_HOUR`（默认凌晨 2 点）后的第一次采样执行。
- 每表每天只执行一次：`_cleanup_date_by_table[table_id] == today` → `already_checked`（`:241-243`）。
- 云端过滤（feishu.py:308-356）：用飞书 `records/search` 的 `isLess` 条件在服务端过滤，不把全表拉到本地。
- 分批删除：每批 ≤500 条（飞书 API 限制，feishu.py:363-364）。
- **每张表独立失败域**：一张表清理失败只记 `failures`，不影响其他表（`:263-265`）。

### 7.3 为什么需要三层幂等

HA 的定时器 webhook 语义是 at-least-once：网络超时会导致 HA 重试。三层各自挡一种情况：

| 层 | 挡什么 | 成本 |
|---|---|---|
| 分桶时间戳（floor） | 同窗口内的所有重复触发 | 无 |
| 内存 latest 缓存 | 避免重复查飞书最新记录 | 重启后失效（无害，查一次即重建） |
| client_token + SQLite PK | 进程重启后缓存丢失、网络重试穿透到飞书 | 无 |

---

## 8. 写链路三：Modbus 后台采集

P1A 落地的工业采集链路。自上而下：

### 8.1 线程生命周期（services/collector.py）

```text
run_server() → start_collectors()  (collector.py:41-99)
├─ _start_lock + _started 幂等守卫（:45-49）
├─ MODBUS_ENABLED=false → 记日志直接返回（:51-53）
├─ parse_modbus_endpoint + parse_register_map 构造校验
│    任何 ModbusConfigError/ImportError/OSError → 只禁用采集，服务照常起（:76-82）
└─ threading.Thread(target=poller.run_forever, daemon=True).start()（:85-90）
     ※ daemon=True：进程退出不被采集线程拖住
```

**契约**（collector.py docstring，面试可直接引用）：`start_collectors` 只从 `app.run_server` 调用一次；Flask app 创建保持无副作用；任何采集侧故障只禁用采集器并记 ERROR，Flask 继续服务。

### 8.2 端点与客户端工厂（modbus_client.py:93-209）

- `ModbusEndpoint` 是 frozen dataclass——端点描述一次构造后不可变，线程间共享无状态风险。
- `parse_modbus_endpoint()`（`:114-179`）做全量校验：transport 只能 tcp/rtu；rtu 必须有串口路径、波特率 1200~115200、校验 N/E/O、停止位 1/2、数据位 7/8；tcp 必须有 host、端口 1~65535；timeout ≥1s。全部中文报错带当前值。
- `build_modbus_client()`（`:182-209`）是**工厂函数**：按 transport 返回 `ModbusSerialClient`（RTU 帧格式是 pymodbus 串口客户端默认）或 `ModbusTcpClient`。两者鸭子类型接口相同（`connected/connect/read_holding_registers/read_input_registers/close`），所以**轮询循环与传输层完全解耦**。
- **惰性 import**（`:189,202`）：`from pymodbus.client import ...` 在工厂内部执行——没装 pymodbus/pyserial 时模块仍可导入（纯函数可测试），只有真正启用采集才要求依赖，缺失被 collector 捕获降级。

### 8.3 寄存器映射（modbus_client.py:59-79, 212-313）

默认布局（可用 `MODBUS_REGISTER_MAP` JSON 覆盖）：

```json
{
  "temperature": {"address": 0, "scale": 0.1, "encoding": "int16", "type": "holding"},
  "humidity":    {"address": 1, "scale": 0.1, "encoding": "uint16", "type": "holding"},
  "device_status": {"address": 2, "online_value": 1, "type": "holding"}
}
```

- 每个测量字段可独立选 holding（FC03）或 input（FC04）；`device_status` **可选**——没有它时"读成功即在线"（简易变送器没有状态字）。
- `parse_register_map` 严格校验：未知字段/未知配置项/address 范围 0~65534/encoding 只支持 int16 与 uint16/scale 必须有限。
- **稀疏地址分段读取**（`:284-299` + `_contiguous_runs :303-313`）：同类型地址排序后切成连续区间，每个区间一次读请求。原因：真实设备稀疏寄存器之间可能是保留/非法区，无条件读 min..max 会触发 Illegal Data Address 异常响应。区间数上限 16（`MAX_RUNS_PER_TYPE`），防病态映射产生过多请求。
- **FC03/FC04 地址空间隔离**（`_read_registers_by_type :398-420`）：holding 和 input 是两个独立地址空间，各自的原始值放在独立字典里——地址同为 0 的一个 holding 寄存器和一个 input 寄存器互不覆盖。
- `decode_register()`（`:316-320`）：int16 负数处理——`raw >= 0x8000` 时减 0x10000（补码解释），再乘 scale。例：0xFFFF × 0.1 = -0.1°C。

### 8.4 轮询循环（ModbusPoller，modbus_client.py:327-562）

```text
run_forever()（:492-525）
└── while not _stop.is_set():
      sample = poll_once()
      ├─ 成功 → record_sample(source="modbus", ...)  → 统一模型
      ├─ 失败 → record_sample(..., status="offline", only_on_status_change=True)
      │         ※ 也要推进离线状态转移，但已经在离线时不再写行
      │           （5 秒一轮 × 关机设备 ≠ 每轮一条 offline 记录）
      └── _stop.wait(poll_interval)   ← 用 Event.wait 而不是 time.sleep：
                                        stop() 能立即打断等待，秒级优雅停止

poll_once()（:422-490）
├─ 未连接则 connect()，失败 → ConnectionError（消息不含端点细节，防 API 泄漏）
├─ 按 holding/input 两类分别分段读取全部所需寄存器
│    _read_run()（:374-396）逐区间校验：
│    - response.isError() → ModbusReadError（从站返回异常码，如 Illegal Address）
│    - registers 数量不足 → ModbusShortResponseError
│      ※ 短响应必须在此拦截：裸索引 registers[i] 抛 IndexError，
│        而 IndexError 不在异常捕获元组里，会杀死整个采集线程
├─ decode + 合理性范围检查（温度 -50~100 / 湿度 0~100，超界按无值处理 + warning）
├─ device_status 判定在线（无该字段则读成功=在线）
└─ 任何失败：(RuntimeError, OSError, ValueError, ModbusException)
      → _safe_close_client()  ← 关键：pymodbus serial 的 connected 只检查
      │    内部 socket 是否存在；USB 拔出后不 close 就永远不会重新 connect()
      → _mark_failure(exc) → 返回 None（采集线程永不 crash）
```

**失败处理的三个层次**：

1. **协议层**（`_read_run`）：从站异常响应/短响应转成受控异常。
2. **连接层**（`poll_once`）：任何失败主动 close，下一轮重连——这就是"USB-RS485 拔插自愈"的实现：拔出 → OSError → close → 重插 → connect() 成功。**边界**：只保证串口路径不变时自愈；OS 重新枚举成新名字（COM3→COM4）需改配置重启（modbus_client.py:18-20 文档化）。
3. **观测层**（`_mark_failure :548-562`）：连续失败计数；错误类别变化或每 12 轮（`REPEATED_ERROR_LOG_INTERVAL`）才记一条完整日志——防止 5 秒一轮打爆日志；对外状态 API 只暴露异常类名（`type(exc).__name__`），完整消息（可能含串口路径）只进服务日志。

**RS485 半双工约束**（modbus_client.py:21-23）：一条总线同一时刻只能有一个主站请求。当前单端点配置天然满足；未来多从站必须是"一条总线一个线程循环多 unit"，不能每从站一线程。

---

## 9. 读链路：/api、/history、/dashboard

所有读接口**只查 SQLite 镜像，不碰飞书**——查询可用性与飞书 API 完全解耦。

### 9.1 鉴权策略矩阵

| 接口 | 未配 HISTORY_API_KEY | key 错误 |
|---|---|---|
| `/api/devices` `/api/events`（api.py:26-38） | **503**（api.py:27-31） | 401 |
| `/history/query` `/history/stats/*`（analytics.py:16-30） | 503 | 401 |
| `/dashboard`（dashboard.py:124-141） | 503 | 401（支持 `?key=` 或 Bearer） |
| `/health` `/api/system/status` | 无鉴权（只回健康摘要） | — |
| `/temperature` | 无鉴权（可选 TEMPERATURE_API_KEY） | 401 |

**"未配 key 返回 503 而不是放行"是推翻过一次的决策**：早期版本"未配置即开放"图省事，review 时认定违反最小权限——已有部署升级后绝不能静默获得无鉴权读能力，宁可 503 逼运维显式配置。503（服务不可用）而非 401（未认证）语义也更准确：不是"你没权限"，是"这套能力没启用"。

### 9.2 统一模型读 API（routes/api.py）

- `GET /api/devices`（`:65-74`）：每个 `(device, source)` 身份一行（ROW_NUMBER 窗口查询，db.py:578-618）。一台设备两个数据源 = 两行，**不做跨源融合**。
- `GET /api/devices/<id>`（`:77-124`）：设备有多源时不允许静默任选一条——返回 400 + `sources` 列表，强制调用方 `?source=` 显式选择身份（`:106-113`）。单源设备直接返回最新值 + 最近样本列表。
- `GET /api/events?device=&limit=`（`:127-141`）：事件日志，新在前。
- `GET /api/system/status`（`:144-158`）：健康聚合——SQLite 统计、采集器状态（transport/device_id/最后成功时间/连续失败数，**无端点无 IP**）、`device_count`（不同设备号数）与 `identity_count`（(device,source) 对数，与 /api/devices 的 count 对账）双计数。

### 9.3 分析接口（routes/analytics.py）

- `/history/query`：时间过滤（接受 epoch 秒/毫秒/ISO8601，`_parse_time_to_ms :42-64`）+ 分页，limit 上限 10000。
- `/history/stats/daily`：按本地日期分组的聚合（异常计数排除离线样本，防止关机设备被算成温度超标——db.py:388-395 的 SQL CASE WHEN）。
- `/history/stats/devices`：设备总览 + `estimated_offline_duration_sec`。字段名故意带 `estimated_`：离线时长 = 离线样本数 × 采样间隔，是估算不是精确值（漏采/停机都会偏差）——字段命名自证语义。

---

## 10. 飞书服务层：锁、缓存、重试、幂等

`services/feishu.py` 是全项目并发设计最讲究的模块。

### 10.1 per-resource lock（feishu.py:16-34）

```python
_resource_locks: dict[str, threading.RLock] = {}
def _resource_lock(key): ...   # key 例如 "record:recXXXX" / "table:tblYYYY"
```

**为什么不是一把全局锁**：串行化粒度是"资源"——不同设备写不同 record、不同历史表之间可以并行；一个慢请求的退避重试只阻塞同一资源。全局锁会让 11 台设备的历史写入被一台的超时串行拖慢。
**为什么不是无锁**：飞书侧 read-modify-write（查最新 → 判断 → 写入）在并发下会产生重复记录；对同一资源必须互斥。
**锁泄漏是接受的代价**：key 空间有限（record 数 + table 数，量级几十），不清理也不增长失控；为几十个 key 做 LRU 淘汰是过度工程。

### 10.2 重试矩阵（两层，职责分离）

**外层业务重试**（`_request_bitable_json :69-137`，持有资源锁）：

| 触发条件 | 动作 |
|---|---|
| code=99991663（token 失效）且未刷新过 | `clear_token()` → 立即重试（每请求周期最多一次） |
| HTTP 429 / ≥500 / 飞书瞬态码 {1254290,1254291,1254607,1255040} | 指数退避 `0.8 × 2^(n-1)` 秒后重试，最多 3 次 |
| 其他 | 直接返回结果由调用方决定 |

**内层传输重试**（`http_client.request_with_retry`，见 §11）：连接错误/超时/HTTP 层 429/5xx。

两层各自处理自己能理解的失败：传输层知道网络，业务层知道飞书语义码。退避公式相同（`backoff × 2^(attempt-1)`：0.8s → 1.6s → 3.2s），这是标准**指数退避**——线性重试在服务端过载时是火上浇油。

### 10.3 record_id 解析（feishu.py:148-228）

```text
DEVICE_RECORD_MAP 手动配置？ → 直接用
├─ 正缓存命中（TTL 3600s）→ 直接用
├─ 负缓存生效中（TTL 300s）→ 直接抛错（防止未知设备名反复触发全表扫描）
└─ 全表分页扫描（page_size=500）按"设备编号"字段匹配
     ├─ 恰好 1 条 → 写正缓存返回
     ├─ 多条匹配 → 抛错（设备编号重复是数据问题，必须人工处理）
     └─ 0 条 → 写负缓存 300 秒，抛错
```

**负缓存（negative cache）是这套设计的点睛**：缓存"找不到"这个结果。没有它，一个拼错的设备名每 10 分钟触发一次 11 表全扫；有了它，5 分钟内直接快速失败。正负缓存 TTL 故意不同：找到的结果 1 小时内可信，找不到的结果 5 分钟后就要重查（可能运维刚建好记录）。

### 10.4 幂等写（create_history_record，feishu.py:295-305）

POST 查询参数带 `client_token=<uuid>`——飞书侧幂等令牌，**同 token 的重复创建只生效一次**。uuid 在函数入口生成一次，`_request_bitable_json` 内部的所有重试复用同一个 token：重试穿透到飞书也不会插重复行。

---

## 11. HTTP 基础设施：http_client 与 token

**http_client.py（71 行）**：

- 每次请求新建 `requests.Session` 并显式 `Connection: close`（`:31,36`）——不用长连接池。这是刻意的简单取舍：请求频率低（每 10 分钟一批），连接复用收益微小，而 waitress 多线程共享连接池需要处理线程安全与失效连接，复杂度不划算。
- `trust_env=False` 默认关闭系统代理（`:17-20`）：生产服务器上的 `http_proxy` 环境变量会把飞书流量劫持到不存在的代理，这是容器部署的经典坑。

**token.py（65 行）**：`tenant_access_token` 进程内缓存。过期时间 = `now + expire - 300s`（`TOKEN_REFRESH_MARGIN_SECONDS`，`:60-63`）——提前 5 分钟刷新，避免"token 上一秒还有效、请求在路上就过期"的边界竞态。token 失效兜底：业务层收到 99991663 → `clear_token()` → 重取（§10.2）。

---

## 12. 数据库层：db.py

### 12.1 四张表，三种语义（db.py:42-103）

| 表 | 语义 | 主键/去重 |
|---|---|---|
| `temperature_reports` | append-only **审计日志**（每次 HA 上报一行，含飞书返回码） | 无（重复上报就是多行，by design） |
| `history_snapshots` | 飞书历史记录的**业务镜像** | `PK(device, sample_time_ms)` 幂等 |
| `device_samples` | **跨源统一样本表**（HA + Modbus + 未来 OPC UA 共用一个 schema） | `PK(device, source, sample_time_ms)` 幂等 |
| `device_events` | **状态转移事件**（只在变化时插入） | 自增 id |

docstring（`:1-27`）把每张表的语义写死在文件头——这是"文档即契约"的范例，比散在注释里强。

### 12.2 连接模型与并发（db.py:131-174）

```python
_lock = threading.RLock()          # 全模块共享一把可重入锁
_connection = sqlite3.connect(..., check_same_thread=False, timeout=5.0)
connection.execute("PRAGMA journal_mode=WAL")
connection.execute("PRAGMA busy_timeout=5000")
connection.execute("PRAGMA synchronous=NORMAL")
```

- **单连接 + RLock**：waitress 4 线程共享一个连接，所有读写持锁串行。RLock（可重入）是必须的：`devices.record_sample` 在 `with db._lock:` 里调用 `db.fetch_previous_device_sample`，后者内部再取同一把锁——普通 Lock 会当场死锁（devices.py:123-127 注释明说）。
- **WAL（Write-Ahead Logging）**：写不再阻塞读。SQLite 默认 journal 模式下写事务独占文件，waitress 的读请求会被采集线程的写卡住；WAL 把写前置到 -wal 文件，读写并发。代价：多一个 wal 文件、跨进程场景需要共享内存协调（本设计明确限定单进程，`:24-26`）。
- **`synchronous=NORMAL`**：WAL 模式下事务不 fsync 到磁盘即返回，性能大幅提升；掉电最多丢最后一个 checkpoint 之后的事务，但**已提交事务在进程被 SIGKILL 时依然完好**（这正是 §4 那个 PID 1 边界"损失有限"的根据）。
- **busy_timeout=5000**：锁被占时等 5 秒而不是立刻抛 `database is locked`。

### 12.3 迁移：为什么 CREATE TABLE IF NOT EXISTS 不够（db.py:105-129）

`IF NOT EXISTS` 只在**建表**时生效——表已存在就整句跳过，**不会给旧表补新列**。P1A 给 `device_events` 加 `source` 列时，老部署的表已经存在，缺列会导致所有 INSERT 失败。解法：

```python
_COLUMN_MIGRATIONS = (("device_events", "source", "ALTER TABLE ... ADD COLUMN source TEXT"), ...)

def _apply_column_migrations(connection):
    # 表存在？→ PRAGMA table_info 取现有列集合 → 缺哪列补哪列（幂等）
```

每连接初始化时执行，逐列 `PRAGMA table_info` 检查后 ALTER——跑多少次都幂等，不需要版本号管理。这是"加性列迁移"模式：只加列不改列类型不删列，任何旧版本库都能无痛升级。（也是 commit `9b8d35e` 的主题。）

### 12.4 隔离边界（db.py:165-174）

`_get_connection()` 把初始化的一切异常（权限、磁盘满、路径非法）转成 `_init_failed=True` + 返回 None——**镜像故障永远不能打断飞书写入主链路或 Flask 启动**。每个写函数同样捕获 `sqlite3.Error` 只计数（`_write_failures`，对 `/health` 可见）。上层路由把这个状态翻译成 503（api.py:42-49：镜像死了返回 503 而不是空列表——空列表会被调用方误读为"没有数据"）。

### 12.5 SQL 可移植性（db.py:420-461, 578-611）

"每设备最新一行"用 `ROW_NUMBER() OVER (PARTITION BY device ORDER BY sample_time_ms DESC)` 窗口函数而不是 SQLite 特有的 `GROUP BY + MAX()` 裸列技巧——前者是标准 SQL，PostgreSQL/MySQL 直接可用。为"如果哪天换主库"留了后路，成本为零。

---

## 13. 统一设备模型与事件状态机

### 13.1 record_sample（devices.py:62-168）

P0 的核心抽象：**任何数据源只要调 `record_sample(device, source, temperature, humidity, status)`，就进入同一张 `device_samples` 表、同一套状态机**。HA 路由（temperature.py:100）和 Modbus poller（modbus_client.py:504）是当前两个调用方；未来 OPC UA 只是第三个 source 字符串。

关键点：

- **身份是 `(device, source)` 二元组**（db.py:547-575 注释）：同一物理设备的 HA 上报和 Modbus 轮询各自维护基线，互不触发对方的状态转移——两个源的在线判定标准不同，混在一起会产生伪事件。
- **设备名归一化**：upper + strip（`:82`）；状态词归一化 `normalize_status()`：把 "在线/run/true/1/…" 和 "离线/unavailable/nan/stopped/fault/…" 映射到 `online/offline`，未知状态拒绝入库（`:38-46,95-101`）。
- **数据库是事实源，没有进程内设备注册表**（docstring `:5-7`）：读 API 直接查库，重启不丢状态，也没有缓存失效问题。
- **整个函数是隔离边界**：任何异常捕获后记日志返回 `[]`（`:166-168`），调用方（HA 请求路径）绝不受影响。
- **临界区**（`:127-157`）：`with db._lock:` 包住"读上一条 → 写本条 → 评估转移 → 写事件"——RLock 可重入，序列保证读基线和写样本之间没有别的写入者插入（否则可能漏事件）。

### 13.2 事件状态机（events.py）

`evaluate_transitions(previous, current)` 是**纯函数**（无 IO、无全局状态，直接单测）：

```text
状态维度1：status    online ⇄ offline         → status_change 事件
状态维度2：温度带    NORMAL ⇄ TEMPERATURE_HIGH  → temperature_alert 事件
                                   （阈值 EVENT_TEMPERATURE_HIGH_C，可关闭）
规则：
- previous is None（首条样本）→ 建立基线，不发事件
- 稳态轮询（状态没变）→ 永远不插入行（device_events 只有转移）
- 温度带只在两边都可判定时比较（None 温度不参与）
```

事件经 `db.save_device_event(source=...)` 落库，携带 old_state/new_state/value/message。`only_on_status_change=True` 参数（devices.py:70-79）服务于失败路径：设备关机后每 5 秒的轮询失败不会刷屏 device_samples——已经在离线就跳过插入。

---

## 14. 前端看板：dashboard

`routes/dashboard.py:174-276`，服务端渲染的单页 HTML：

- **Chart.js 本地化**（`/static/chart.umd.min.js`，dashboard.py:24）：不引 CDN——生产环境可能无外网，看板不能因为图表库加载失败而白屏。有降级路径：`Chart` 未定义时显示"请使用 API 接口"的提示（`:70-74`），表格数据不受影响。
- **XSS 防护双保险**：所有进入 HTML 的字符串过 `html.escape()`（`:144-171`，设备名含 `<script>` 也只是文字）；嵌入 `<script>` 的 JSON 把 `<` 替换成 `\u003c`（`:265-268`），防止数据值以 `</script>` 闭合标签逃逸出脚本上下文。
- **图表数据对齐**：温度/湿度两个数据集统一到同一个排序设备轴，缺失值填 None（`:231-237`）——设备 A 只有温度没有湿度时，不能让湿度序列的标签整体错位。
- 认证用 `?key=`（服务端渲染页面浏览器无法带自定义 header）或 Bearer（程序访问），注释明确"URL 需保密"（`:128-135`）。

---

## 15. 部署与 CI/CD

### 15.1 Dockerfile 要点

- `python:3.12-slim` 基础镜像；**非 root 运行**：uid/gid 固定 1000（宿主对 data/logs 目录授权有确定目标）。
- **追加 dialout 组**（Debian gid 20）：Modbus RTU 需要 `/dev/ttyUSB*` 的组读权限；宿主设备组 gid 不同时用 compose `group_add` 对齐——这是真机验收时的关键接线知识。
- `EXPOSE 5000`；`CMD ["python","app.py"]`（exec 形式 → python 是 PID 1，见 §18 的信号边界）。
- `ARG BUILD_VERSION` 写进 LABEL：镜像自带版本可追溯。

### 15.2 CI → CD 两段式（.github/workflows/）

```text
push/PR ──► tests.yml：ruff check + unittest（Python 3.12）
              │
              │ 仅 main 分支 push 且测试成功
              ▼ 由 workflow_run 触发（不是同一个 workflow 里连续 job）
           deploy.yml（concurrency: production-deploy，取消进行中的旧部署）
              │
              ├─ build job：checkout head_sha → 镜像 tag = commit SHA
              │    docker build → push 阿里云 ACR（SHA tag + latest tag）
              │
              └─ deploy job：SSH 到生产服务器
                   ├─ 校验 compose.yaml/.env 存在；data/logs 属主修正到 1000
                   ├─ git reset --hard origin/main 同步 compose 文件
                   │    （严禁 git clean -fdx：.env/data/logs 必须存活）
                   ├─ 备份旧 IMAGE_TAG（.env 里的）→ rollback() 函数就绪
                   ├─ sed 只改 .env 的 IMAGE_TAG 行（其余业务配置不动）
                   ├─ docker compose pull
                   ├─ docker compose up -d --wait --wait-timeout 120
                   └─ curl --retry 10 --retry-delay 3 http://127.0.0.1:$PORT/health
                        失败 → rollback()：把 .env 改回旧 tag → up -d → exit 1
```

**每个机制回答一个面试问题**：

- **为什么用 `workflow_run` 而不是 push 直接部署**：PR 只跑测试永不部署；部署的触发条件是"**测试成功的那个 commit**"（`head_sha`），测试与部署的代码完全一致，不存在"测试 A、部署 B"的窗口。
- **为什么镜像 tag 用 commit SHA 而不是 latest**：SHA 是不可变标识——回滚 = 把 .env 的 IMAGE_TAG 改回旧 SHA；latest 是会漂移的指针，无法回滚到"上一个确切版本"。运行中的容器 `docker inspect` 也能对出正在跑哪个 commit。
- **为什么 `up -d` 而不是 `down -v`**：`-v` 删卷。data/（SQLite）和 logs/ 是 bind mount，`up -d` 重建容器但数据不动——这就是"Docker 升级镜像不丢数据"的机制：**镜像（只读模板）与容器（运行实例）与数据（volume/bind mount）三者分离**。
- **回滚是应用层实现而非平台能力**：rollback() 是 shell 函数，改 tag → pull → up → 健康检查链路复用，总耗时分钟级，不需要 K8s 级基础设施。
- **健康门禁**：`/health` 返回 SQLite 统计（temperature.py:133-135），部署脚本 curl 重试 10 次——新容器活着且数据库可写才算成功。

### 15.3 环境双形态

同一镜像服务两种部署：生产服务器（Compose + .env + bind mount）和 HA Add-on（config.py 检测 `/data/options.json` 自动切换数据目录 + 读 options 配置，§5）。Add-on 的 `uart: true` 声明让 Supervisor 暴露串口设备。

---

## 16. 测试策略

- **129 个测试，CI 强制**（tests.yml：`ruff check` + `unittest discover`）。每个服务模块一个测试文件（test_db/test_feishu/test_history_service/test_modbus_client/test_collector/test_devices_model/test_config…）。
- **hermetic（密闭）原则**：测试不触网、不写仓库目录。临时目录承载 SQLite；commit `9b8d35e` 专门修复过测试隔离（幂等迁移测试曾可能污染真实库）。
- **集成测试打真协议**：`tests/helpers.py` 用 pymodbus 起一个**真实的 ModbusTcpServer**（独立线程 + 临时端口 + 独立 asyncio loop），集成测试走完整线上协议而不是 mock 客户端方法。SimDevice 单块布局同答 FC03/FC04；四块布局用于证明同号 holding/input 地址不串（helpers.py docstring）。
- **纯函数优先可测**：`evaluate_transitions`、`parse_register_map`、`floor_sample_time`、`decode_register` 都是无 IO 纯函数，测试直接断言输入输出——这是"把逻辑从 IO 里剥出来"分层设计的直接收益。
- 注入点而非 mock 框架：`ModbusPoller` 接收 `record_sample` 回调参数（modbus_client.py:74），测试传 `_noop_record_sample`。

---

## 17. 关键设计决策 12 讲（面试叙事）

每讲按 **问题 → 选择 → 替代方案 → 代价** 展开。这是面试讲项目的骨架素材。

**① 飞书为事实源 + SQLite 为镜像，而不是直接用 SQLite 做主库。**
问题：业务方在飞书表格里看数据、加公式、做看板。选择：飞书是 system of record，本地镜像 best-effort。替代：把 SQLite 当主库同步到飞书（双向同步的冲突解决复杂度爆炸）。代价：镜像有秒级滞后；镜像挂了读功能 503。

**② 为什么 SQLite 而不是 MySQL/PostgreSQL。**
数据量级：11 设备 × 144 条/天 ≈ 每天 1600 行，年 60 万行——单机嵌入式数据库绰绰有余。部署形态：Add-on 单容器，外置数据库需要第二个服务、网络、凭据，违背"Add-on 自包含"。WAL 解决了读阻塞写。明确边界写进 db.py docstring：单进程设计，多进程部署需要重新设计协调。

**③ 单进程多线程，而不是多进程/微服务。**
采集线程与 Web 服务共进程：线程间共享一个 SQLite 连接即可同步，进程间就要引入 IPC。waitress 4 线程足够这个负载。代价：GIL 下 CPU 并行受限（本应用是 IO 密集，无影响）、多 worker 部署需要部署层约定（§4 三层防线）。

**④ per-resource lock 取代全局锁。**
演进：最初一把全局锁串行化所有飞书请求 → 11 台设备的历史写入被最慢一台拖住。现在锁粒度 = record/table key（feishu.py:16-34）。替代方案：无锁（read-modify-write 竞态产生重复记录）、分布式锁（单进程用不上 Redis，过度工程）。

**⑤ 两层重试 + 指数退避。**
传输层（http_client）管网络错误/超时；业务层（feishu）管飞书语义码（429/5xx/瞬态码/token 失效）。指数退避 0.8→1.6→3.2s。为什么不无限重试：at-least-once 调用方（HA）有自己的重试，本层重试 3 次后把 502 抛回去是正确的责任边界。

**⑥ 幂等的三层实现（历史采样）。**
时间分桶（同窗口同目标时间戳）+ 内存 latest 缓存（省查询）+ client_token/复合主键（穿透兜底）。不同层挡不同的重复路径（§7.3）。

**⑦ 状态机身份是 (device, source)。**
两个数据源对同一物理设备的在线判定标准不同（HA 看 status 字段，Modbus 看能否读到寄存器），共享基线会互相触发伪事件。代价：读 API 必须处理"一台设备多行"，解法是多源时强制 `?source=`（400 + sources 列表）而不是静默选一条。

**⑧ 事件只存转移，不存快照。**
稳态轮询 5 秒一条 × 11 设备 = 每天 19 万行垃圾。device_events 只在状态变化时插入，查询"当前状态"走 device_samples 最新行。这是监控系统的标准做法（事件 vs 状态分离）。

**⑨ 隔离边界模式（三个模块共用同一思想）。**
db.py（初始化失败只停用镜像）、devices.record_sample（异常全吞）、collector（配置错误只禁用采集）——都是"副系统故障只能降级自己，不能拖垮主链路"。反面：如果 SQLite 镜像异常 500 了 /temperature，飞书写入也会失败——数据丢失，不可接受。

**⑩ 端点信息不进状态 API。**
`/api/system/status` 只有 transport/device_id/时间戳/计数字；串口路径、IP、端口只进服务日志（modbus_client.py:107-111,427-428,549-551 三处刻意处理）。原因：状态 API 是无鉴权的健康接口，端点细节是内网侦察情报。

**⑪ 惰性依赖 + 惰性解析。**
pymodbus/pyserial 是可选依赖：未启用 Modbus 的部署不需要装；import 延迟到工厂函数内；寄存器映射解析延迟到真正启用时，非法值只禁用采集（collector.py:76-82）。效果：核心服务（飞书链路）的可用性与工业采集依赖完全解耦。

**⑫ CI/CD 用 commit SHA 镜像 + 应用层回滚。**
SHA 不可变可回滚；workflow_run 保证测试与部署同一 commit；rollback 是 shell 函数（改 .env tag → up → health gate），分钟级恢复，零基础设施成本（§15.2）。

---

## 18. 已知边界（诚实清单）

面试讲项目时，**主动说出边界比掩盖边界加分**。当前明确接受的限制：

1. **PID 1 与 SIGTERM**：容器内 python 是 PID 1、无 SIGTERM 处理器，`docker stop` 时 `finally` 里的 `stop_collectors()` 不执行，宽限期后 SIGKILL。损失有限（WAL 已提交事务安全、daemon 线程随进程死），修复方案（signal handler 或 `--init`）已评估，按"冻结主功能"原则暂不改动。
2. **RTU 串口重枚举**：USB-RS485 拔插在路径不变时自愈；COM 号变了需改配置重启（modbus_client.py:18-20 文档化）。
3. **多进程部署不支持**：采集线程"只起一次"是进程内保证；多 worker 必须只在一个 worker 开 MODBUS_ENABLED（部署约定，config.py:272-273 注释）。
4. **内存缓存进程内**：record_id 缓存、latest 采样缓存、清理日历都是进程内存——重启丢失后自动重建，代价只是重启后第一批请求多几次查询。
5. **离线时长是估算**：离线样本数 × 间隔；漏采/停机导致偏差，API 字段名 `estimated_` 自证。
6. **单 Modbus 端点**：一条 RTU 总线一个线程、一个 unit_id；多从站是下一步（一条总线一个线程循环多 unit 的设计已预留在 docstring 约束里）。
7. **CSV 与 SQLite 双写**：temperature_reports 走 CSV + SQLite 两份（storage.py），CSV 是给非技术同事用 Excel 看的历史习惯，属于兼容性保留。

---

## 19. 面试问题地图

总提示词里的问题清单 → 本文对应章节，练习时先自答再对照：

| 问题 | 章节 |
|---|---|
| 为什么选 Flask？请求生命周期？ | §4（waitress 选型）、§6（请求逐跳） |
| Blueprint 作用？register_blueprint 做了什么？ | §3、§4（create_app） |
| 为什么 SQLite 不用 MySQL？WAL 是什么？事务解决什么？ | §12.2、§17② |
| 什么是幂等？为什么需要迁移？IF NOT EXISTS 为何不够？ | §7.3、§12.3 |
| 为什么需要 Lock？race condition？global→per-resource lock？ | §10.1、§12.2、§17④ |
| TTL cache？negative cache 为什么存在？ | §10.3 |
| HTTP 429 怎么处理？指数退避是什么？ | §10.2、§11 |
| Modbus RTU 和 TCP 区别？RS485 是什么？为什么工业喜欢 RS485？ | §8.2（同一 poller 双传输）、§8.4（半双工约束） |
| FC03 和 FC04 区别？40001 是什么？CRC 怎么工作？ | §8.3（地址空间隔离）；40001=保持寄存器偏移 0 的习惯地址（4xxxx 段 + 1 偏移）；CRC 由 pymodbus 帧层处理 |
| 设备断线怎么检测？USB-RS485 拔插怎么办？ | §8.4 失败三层次、§18.2 |
| 状态机怎么设计才不刷屏？ | §13.1（only_on_status_change）、§17⑧ |
| Docker volume 为什么升级不丢数据？image vs container？ | §15.2 |
| GitHub Actions 如何完成 CI/CD？SHA 镜像？自动回滚？ | §15.2 |
| 11 个设备扩展到 1000 个怎么办？ | §18.3/4/6（真实答案：分批改造——多从站总线、分进程采集、SQLite 换主库、飞书换消息队列，每一项都标明触发条件，而不是现在就上 K8s） |

---

*生成于 2026-08-23，基于 main@9b8d35e。本文档是学习材料，未提交到仓库（push main 即触发生产部署）。*
