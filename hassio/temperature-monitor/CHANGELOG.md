# Changelog

## Unreleased

IIoT 数据采集（Modbus TCP / RTU）：

- 新增可选 Modbus 采集：`MODBUS_ENABLED` 默认关闭；支持 TCP 与 RTU（USB-RS485，`MODBUS_TRANSPORT=rtu` + `MODBUS_SERIAL_PORT`）两种传输，输出到与 HA 上报共用的统一设备模型。Add-on 声明 `uart: true` 自动映射宿主串口，镜像内用户已加入 dialout 组。
- 统一设备模型与事件：新增 `device_samples`（跨源统一样本）与 `device_events`（状态转移事件，仅在变化瞬间记录）表；状态机身份为 (设备, 数据源)。`/api/devices`、`/api/devices/<id>?source=`、`/api/events`、`/api/system/status` 四个新接口，鉴权与既有查询接口一致（未配置 `HISTORY_API_KEY` 返回 503）；SQLite 不可用时显式 503。
- 寄存器映射 `MODBUS_REGISTER_MAP`：字段级 FC03/FC04 类型、`device_status` 可选（省略时读取成功即在线）、稀疏地址分段读取；`address` 为零基 PDU 地址（40001/30001 均填 0）。
- 工业现场健壮性：短响应/异常响应安全失败不退出采集线程；通信故障主动关闭传输并自动重连（串口路径不变时含 USB 重插）；`/api/system/status` 只暴露稳定错误类别，不含串口路径/IP。
- 新增工具：`tools/modbus_simulator.py`（本地模拟 PLC）与 `tools/modbus_probe.py`（单次读取探针；unit-id 扫描仅显式 `--scan` 启用）。
- 新增可选温度阈值事件 `EVENT_TEMPERATURE_HIGH_C`；新增 `MODBUS_TIMEOUT_SECONDS`。
- 依赖：新增 `pymodbus>=3.15,<3.16` 与 `pyserial>=3.5,<4`。

安全加固：

- `POST /temperature` 支持可选共享密钥 `TEMPERATURE_API_KEY`：设置后请求必须携带 `X-Temperature-Key`（或复用 `X-History-Key`）头；留空保持无鉴权以兼容旧 HA 配置。Add-on 新增 `temperature_api_key` 选项。
- 容器改为非 root 用户（uid/gid 1000）运行；自动部署脚本会修正宿主 `data/`、`logs/` 目录属主，手动部署需确保目录可被 uid 1000 写入。
- 新增请求体大小上限 `MAX_CONTENT_LENGTH`（默认 16KB），超限返回 413。
- Compose 为容器 stdout 配置 `json-file` 日志轮转（10MB × 3 个文件）。
- Dashboard 鉴权额外支持 `Authorization: Bearer <HISTORY_API_KEY>` 请求头，便于程序化访问且避免密钥进入 URL。

性能与可靠性：

- 飞书 API 调用的串行化粒度从全局锁改为按资源（record/table）加锁：不同设备写不同记录、不同历史表之间可并行，慢请求的重试退避不再阻塞无关设备。
- 自动识别的 record_id 缓存增加 1 小时 TTL，飞书表记录重建后无需重启服务即可恢复。
- 未找到的设备名增加 5 分钟负缓存，避免未知设备反复触发全表分页扫描。

可配置性：

- 新增 `HISTORY_DEVICES` 环境变量 / Add-on 选项：历史采样设备列表默认 TH-01～TH-11，可用逗号分隔列表覆盖；`HISTORY_TABLE_MAP` 必须与之一致。
- 环境变量整数/浮点解析失败时输出包含变量名和当前值的友好错误；空字符串回退默认值。

其他：

- Chart.js 4.4.3 改为由服务本地 `/static/` 目录提供，Dashboard 不再依赖外部 CDN。
- CI（Tests workflow）新增 `ruff` 错误级 lint 检查；仓库根新增 `ruff.toml` 固定规则集。

## 1.2.1

- 修复 SQLite 初始化遇到文件系统异常（如权限不足）时可能阻断服务启动的问题；现在任何初始化异常都只停用本地镜像。
- Home Assistant Add-on 环境下 SQLite/CSV 默认写入 Supervisor 持久目录 `/data`，避免升级或重建容器时本地数据丢失。
- 修复 Dashboard 温湿度数据集在部分设备缺数据时的标签错位问题，统一设备轴并以空值占位。
- Dashboard 设备总览表输出增加 HTML 转义。
- 查询接口 `limit` / `days` 非法参数回退默认值，不再返回 500。
- 修复 README 环境变量表与章节标题间的 Markdown 换行问题。

## 1.2.0

- 新增 SQLite 本地数据服务层：best-effort 镜像温度上报（append-only 事件日志）与历史快照（复合主键幂等），WAL 模式；飞书仍是唯一事实源，`SQLITE_ENABLED=false` 可整体关闭。
- 新增只读查询与统计 API：`GET /history/query`、`/history/stats/daily`、`/history/stats/devices`（`X-History-Key` 鉴权，镜像未启用返回 503）。
- 新增可视化看板 `GET /dashboard`：超限/离线趋势、设备平均温湿度、设备总览表；Chart.js CDN 不可达时优雅降级。
- `GET /health` 暴露 SQLite 镜像状态（enabled / write_failures / 表行数）。
- 修复 `SQLITE_ENABLED=false` 时仍会创建数据库文件的问题。
- Add-on 新增 `sqlite_enabled` 选项。

## 1.1.3

- 修复飞书公式字段（富文本数组结构）解析：`温度判定`、`湿度判定`、`当前判定状态` 等公式字段现按显示文本（如 `正常`、`仅监测`）写入历史表，形成静态快照。
- 历史快照不再随实时表公式规则变化回写，保证历史记录不可变。

## 1.1.1

- 修复 `history_cleanup_enabled: false` 时仍查询 11 张历史表的问题；关闭后完全跳过筛选与删除 API。
- 修复飞书日期筛选的 `InvalidFilter`，按官方格式传入 `ExactDate` 和毫秒时间戳。

## 1.1.0

- 新增带 `X-History-Key` 鉴权的十分钟历史采样接口。
- 新增 TH-01 至 TH-11 分表写入、时间桶去重和重启后去重恢复。
- 新增离线快照空温湿度、逐设备部分失败结果及飞书限流/冲突重试。
- 增加 90 天清理保护；关闭时完全跳过筛选与删除 API。
- 补充 Home Assistant 十分钟自动化、共享密钥配置和版本化容器部署。
