# 系统维护与温湿度传感器接入手册

更新日期：2026-10-23
适用对象：Temperature Monitor 维护人、Home Assistant 管理员、现场仪表维护人
用途：交接系统日常维护，并指导新增温湿度监测点从现场设备接入到平台验证。

> 本手册描述当前仓库实现。电气接线、安装位置、校准周期、环境限值、现场处置要求由设备说明书和获授权的工艺/质量文件确定；软件配置不能替代这些依据。现场交接另见 [docs/handover.md](handover.md)，温湿度巡检和标准维护另见[操作手册](temperature-humidity-control-manual.md)。

## 1. 一页看懂全链路

~~~mermaid
flowchart LR
    sensor["温湿度传感器"]
    ha["Home Assistant 实体与自动化"]
    modbus["Modbus TCP / RTU 采集线程"]
    api["Flask API /temperature"]
    norm["校验、单位归一、设备身份"]
    sqlite[("SQLite 持久样本、presence、事件、运行审计")]
    scheduler["持久调度器与重试"]
    feishu[("飞书设备实时表、历史表、标准/作业表")]
    hmi["/console · /dashboard · 查询 API"]
    runtime["Shadow / Active 监控运行时"]

    sensor --> ha --> api --> norm --> sqlite
    norm --> scheduler --> feishu
    ha -->|"每分钟心跳"| api
    ha -->|"/history/sample 每10分钟"| api
    sensor -->|"TCP 或 RS485"| modbus --> sqlite
    sqlite --> hmi
    feishu -->|"标准、作业、观察"| runtime
    sqlite --> runtime
~~~

### 两种采集路径的差别

| 路径 | 适用情况 | 当前数据落点 |
| --- | --- | --- |
| Home Assistant → HTTP | HA 已能读取设备；需要沿用现有实体、心跳与实时飞书投影 | 有效样本先写 SQLite；持久调度器再投影到飞书实时设备表。单独的历史自动化每 10 分钟触发历史快照。 |
| Modbus TCP / RTU | 设备/网关提供 Modbus 寄存器，服务主机能访问 TCP 地址或 RS485 串口 | 写入统一设备样本、在线状态和事件，供 SQLite 查询、控制台和运行时使用。当前 Modbus 采集器不会自动更新飞书设备实时表，也不会自行写每设备飞书历史表。 |

需要飞书实时表和现有 HA 工作流时，优先使用 HA 路径。若必须直采 Modbus 且需要飞书投影/历史表，请先设计并实现对应投影链路，不能把本地设备 API 出现样本当成飞书已写入。

当前配置只装配一个 Modbus 采集端点、一个 Unit ID 和一个设备编号。它可以代表 PLC 暴露的一组寄存器；若要由一个服务实例同时轮询多个 Modbus 从站/独立设备，需要扩展采集器或采用受控的多个实例。不要把同一 RS485 总线配置成多个并发轮询器。

## 2. 系统边界和关键文件

| 内容 | 位置 | 维护说明 |
| --- | --- | --- |
| 服务配置 | [.env.example](../.env.example)、部署机 .env | 示例可提交；生产凭据只保存在部署环境。环境变量变更通常要重启容器。 |
| 应用入口与 HTTP 服务 | [app.py](../app.py)、[routes/](../routes/) | 生产入口启动 Flask/Waitress、运行时和采集线程。 |
| HA 示例 | [homeassistant/](../homeassistant/) | REST 命令、TH-01 实时上报、heartbeat、历史采样示例。复制后替换设备 ID 和实体 ID。 |
| Modbus 采集 | [services/modbus_client.py](../services/modbus_client.py)、[services/collector.py](../services/collector.py) | 寄存器解析和轮询线程。采集故障会记录状态/日志，不应阻塞 HTTP 服务。 |
| 设备统一模型与 SQLite | [services/devices.py](../services/devices.py)、[services/db.py](../services/db.py)、[repositories/](../repositories/) | 设备样本以“设备 + 来源”区分；heartbeat 单独更新 presence，不伪造新测量。 |
| 飞书字段契约 | [docs/feishu-mapping.md](feishu-mapping.md) | 查表、字段映射、作业、事件和点检写入约定。 |
| 生效标准来源 | [docs/standard-source-of-truth.md](standard-source-of-truth.md) | 运行标准由飞书标准表提供；本地设备配置不是标准来源。 |
| Docker 部署 | [compose.yaml](../compose.yaml)、[compose.deploy.yaml](../compose.deploy.yaml) | Compose 把宿主机 data/、logs/ 挂进容器；容器以 uid/gid 1000 运行。 |
| HA Add-on | [hassio/temperature-monitor/](../hassio/temperature-monitor/) | Add-on 选项存在 Supervisor 配置中；串口权限由 manifest 的 uart: true 暴露。 |
| 运维工具 | [tools/](../tools/) | Modbus 探针/模拟器、SQLite 在线备份及备份保留预览。 |

持久数据位置取决于运行方式：SQLITE_ENABLED=true 时，Compose 默认数据库在宿主机 data/temperature_monitor.db；HA Add-on 环境默认使用 /data。最终以 SQLITE_DB_PATH 和实际挂载为准。不要只备份容器可写层。

### 2.1 部署运行档案（每次交接填写）

本手册是通用流程。完成现场交接时，把下表复制到受控的内部运维记录并填实值；不要把 secret 明文放进仓库。此档案未填写完之前，接手人仍需要原维护人协助确认生产环境。

| 项目 | 本部署实际值 |
| --- | --- |
| 运行方式 | Docker Compose / HA OS Add-on / 其他：待填 |
| 服务主机和维护入口 | 主机名、SSH/HA 管理入口及授权组：待填 |
| Compose 项目目录或 Add-on 实例名 | 待填 |
| HA 到服务的 Base URL | HA 网络类型、内部主机名/IP、端口：待填 |
| 外部访问/防火墙 | 仅允许哪些网络访问 5000 端口：待填 |
| 持久数据库路径 | SQLITE_DB_PATH 实际值及宿主挂载：待填 |
| 飞书凭据存放位置 | Vault/Secret Manager 路径或管理员，不记录 secret 值：待填 |
| 飞书表资源登记 | 实时表、设备表、标准表、事件表、历史表的内部目录链接：待填 |
| 当前版本和回滚版本 | commit SHA / 镜像标签：待填 |
| 自动化运行模式 | AUTOMATION_MODE、SHADOW_DEVICE_IDS、ACTIVE_DEVICE_IDS：待填 |
| SQLite 备份 | 频率、外部存储位置、最近备份、quick_check 结果：待填 |
| 维护窗口与升级联系人 | 待填 |

责任分工建议明确到人或团队：

| 责任 | 主责 | 需要通知的角色 |
| --- | --- | --- |
| 传感器供电、布点、校准、RS485/TCP 连通 | 仪表/现场维护 | 区域责任人 |
| HA entity、自动化、secret 和网络路径 | Home Assistant 管理员 | 服务维护人 |
| 容器、SQLite、发布、备份恢复 | 应用/平台维护人 | 值班负责人 |
| 飞书表字段、记录绑定和应用权限 | 飞书空间管理员 | 服务维护人 |
| 控制标准、阈值和异常闭环 | 工艺/质量标准负责人 | 区域责任人 |

### 2.2 单台设备登记卡（每个监测点一行）

| 字段 | 记录 |
| --- | --- |
| 设备编号 / 区域 / 责任人 | 待填 |
| 型号 / 序列号 / 安装位置 / 校准到期 | 待填 |
| 采集路径 / 数据源身份 | HA、Modbus 或其他；source 值：待填 |
| HA 温度实体 / 湿度实体 | 待填 |
| Modbus 主机:端口 / 串口 / Unit ID | 待填 |
| 寄存器类型、PDU 地址、编码、scale、单位 | 待填；依据手册版本：待填 |
| 上报/轮询周期 / freshness 预期 | 待填 |
| 实时飞书设备行 / 历史表映射 | 待填或不适用 |
| 标准编号、版本、生效期 | 待填或不适用 |
| 最近端到端验证时间 / 结果 | 待填 |

表中真实资源 ID 和网络地址只写在公司受控记录库；公开仓库留空或写示例值。

## 3. 新增传感器：从现场到可观测

### 3.1 接入前先定身份和责任

给每个监测点确定唯一稳定的设备编号，例如 TH-12，并登记：

- 设备编号、安装区域、传感器型号/序列号、安装时间和位置；
- 来源路径（HA 或 Modbus）、网络地址或串口、Unit ID；
- 温度与湿度单位、寄存器表/实体 ID、数据刷新周期；
- 责任人、备用联系人、校准/比对方法；
- 是否需要飞书实时行、历史表、运行标准、告警或通知。

同一设备编号不要随意更名。设备查询身份实际是“设备 + 来源”；如果有意让同一个编号同时接入 HA 和 Modbus，会得到两条独立来源记录，不会自动融合。只有确有需要时才这样配置，查询设备详情时必须指定 source。

### 3.2 安装并确认传感器读数

1. 按设备手册确认供电、电压、端子定义、总线终端/偏置和防护等级；本项目不提供任何型号的接线图。
2. 核实安装位置有代表性，避开热源、出风口、门口和局部冷凝等干扰；按现场要求完成校准或与合格参考仪表比对。
3. 记录现场仪表显示值、时间、参考仪表、差值和复核人。平台只接受有限数值；温度合理范围为 -50 至 100 °C，湿度为 0 至 100 %RH。
4. 确认服务主机与 HA/传感器之间的网络或串口可达，再配置软件。

### 3.3 先准备飞书设备行（HA 路径必需）

HA 的 /temperature 会在飞书实时设备表中查找目标设备记录，并把温湿度投影到该行。接入前：

1. 在部署使用的 TABLE_ID 对应设备实时表中创建一条设备记录。
2. 设备编号字段（默认字段名 设备编号，可由 DEVICE_ID_FIELD 覆盖）填写与上报完全一致的编号，例如 TH-12。
3. 确保该设备编号在该表中只出现一次。自动查找遇到重复记录会拒绝投影；没有唯一记录时也无法投影。
4. 如 HA 上报的是不同名称，例如 sensor.warehouse_temperature，配置 DEVICE_NAME_MAP / Add-on device_name_map 把该名称映射到标准设备编号。更简单的做法是让自动化直接上报 TH-12。
5. 若要写入飞书历史快照，在每设备历史表建表并登记 table ID。把设备加入 HISTORY_DEVICES，并在 HISTORY_TABLE_MAP 为参与采样的每台设备提供一一对应的表 ID。覆盖默认设备列表时，两项必须完整且一致；不需要历史时不要把设备放进采样列表。

Modbus 直采不会使用上述实时表投影。无需为了本地 Modbus 查询创建实时表记录；只有另行开发投影时才需要相应的 Feishu 记录绑定。

设备实时行与环境标准行是两张不同的表：实时行保存设备身份和当前状态，标准行保存经批准的控制要求。创建实时行或看到新传感器有数据，均不代表已经创建了标准。

### 3.4 路径 A：通过 Home Assistant 上报

#### A1. 确认 HA 实体

在 HA 开发者工具中确认温度、湿度实体都有有效数值，状态不会长期停在 unknown / unavailable。记录实体 ID，例如：

- sensor.th12_temperature
- sensor.th12_humidity

检查温度源单位。HA 上报单位由服务的 SOURCE_TEMPERATURE_UNIT 控制：C 表示摄氏，F 表示华氏并由服务转换成摄氏。一个服务实例使用同一个来源单位设置；混合单位设备先在 HA 建立统一单位模板实体再上报。

#### A2. 配置 REST 命令和密钥

复制 [homeassistant/rest_command.yaml](../homeassistant/rest_command.yaml) 中的 python_test、python_heartbeat，以及需要历史采样时的 temperature_history_sample。改名成不冲突的命令名，并替换服务地址：

- HA 与服务使用 host 网络时，通常用 127.0.0.1:5000；
- 普通 Docker bridge 网络时，使用服务主机局域网 IP 和发布端口；
- 容器共享自定义网络时，使用服务容器名；
- HA OS Add-on 使用该 Add-on 实例的内部主机名；自定义仓库安装时主机名可能随安装实例变化。

Compose 生产部署和 Home Assistant Add-on 默认要求设置至少 32 字节的 TEMPERATURE_API_KEY；在 HA `secrets.yaml` 保存同一个随机密钥，实时上报和 heartbeat 都发送 `X-Temperature-Key`。历史采样单独使用 HISTORY_API_KEY / X-History-Key。密钥不要写在公开仓库或 URL 中。部署脚本会在重启前检查密钥是否存在且长度达标。

REST 命令示意：

~~~yaml
rest_command:
  temperature_monitor_report:
    url: "http://<temperature-monitor-host>:5000/temperature"
    method: POST
    headers:
      Content-Type: application/json
      X-Temperature-Key: !secret temperature_monitor_temperature_api_key
    payload: >
      {
        "device": "TH-12",
        "temperature": "{{ states('sensor.th12_temperature') }}",
        "humidity": "{{ states('sensor.th12_humidity') }}"
      }
~~~

把模板放在现有 rest_command 配置里，避免重复定义顶层键。HA 使用 packages 或 include 的方式依项目现有配置组织；改动后检查配置并重载 REST 命令/自动化或重启 HA。

#### A3. 为新设备增加两类自动化

测量自动化在温度或湿度变化时调用 /temperature，代表一条真实测量。复制 [homeassistant/automation_th01.example.yaml](../homeassistant/automation_th01.example.yaml)，将 TH-01 和实体 ID 替换为新设备。不要把实体状态 unknown 或 unavailable 当成温湿度上报。

另复制 [homeassistant/automation_th01_heartbeat.example.yaml](../homeassistant/automation_th01_heartbeat.example.yaml)，每分钟调用 /temperature/heartbeat，并在必需实体不可用时传 availability=unavailable。Heartbeat 负责在线状态/新鲜度，不新增测量历史；它不能替代测量自动化。可复用示例中的模板逻辑，替换两个实体和设备编号。

需要飞书历史记录时，再部署 [homeassistant/automation_history_sample.yaml](../homeassistant/automation_history_sample.yaml) 中每 10 分钟一次的调用，并确认设备历史表映射完整。该调用是全量历史采样任务，不需要每个设备单独建一个定时器。

#### A4. 验证 HA → 服务 → 飞书

1. 先在测试环境或批准的维护窗口通过新设备的真实 HA 状态触发一条上报。
2. HTTP 接口返回 200 且内容为 success 或 accepted 代表请求已被服务接受；accepted 通常表示 SQLite 已保存、飞书投影排队或等待重试，不代表飞书行已更新。
3. 用受保护接口确认本地样本：

   ~~~sh
   curl -fsS -H "X-History-Key: <HISTORY_API_KEY>" \
     "http://<temperature-monitor-host>:5000/api/devices/TH-12?source=home_assistant"
   ~~~

4. 对照飞书实时设备行的温度、湿度、在线状态和更新时间。若有历史采样，检查对应设备的历史表是否出现当前采样桶。
5. 在 HA 中确认 heartbeat 更新时间会推进、availability 与实体状态相符，同时温度值没有变化时测量时间不会被 heartbeat 刷新。
6. 打开 /console 查看设备卡片/来源/最后样本；用 /api/events?device=TH-12 检查在线/离线状态迁移。

### 3.5 路径 B：Modbus TCP / RTU 直采

#### B1. 从设备手册取得协议参数

开始配置前必须取得设备/网关的寄存器手册，确认：

- 传输为 TCP 还是 RTU；TCP 地址和端口，或串口设备路径；
- Unit ID（Modbus 从站地址）；
- 温度、湿度和可选状态字分别使用 FC03 holding 还是 FC04 input；
- 每个寄存器的地址、16 位有符号/无符号解释、比例因子、状态在线值；
- 厂商手册使用的 40001 / 30001 编号与软件需要的零基 PDU 地址之间的换算；
- 温度最终必须输出为摄氏度。SOURCE_TEMPERATURE_UNIT 只影响 HA HTTP 输入，Modbus 解码不做华氏转摄氏；Modbus 映射只有 scale，没有带偏移的单位换算。

不能根据其他型号或本手册猜寄存器地址。当前映射只支持单个 16 位寄存器和 int16 / uint16；需要 float32、字节序组合或 32 位量程时，需要扩展解码器。

#### B2. 先用探针验证，不先启动常驻采集

在能访问设备的环境安装项目依赖后，用仓库的探针做单次读取。默认只试指定 Unit ID；扫描会逐个访问总线地址，只有现场允许时才显式启用：

~~~sh
# TCP，显式写设备端口
python tools/modbus_probe.py --tcp 192.0.2.20:502 --unit 1

# RTU：Linux 示例；Windows 可用 COM3
python tools/modbus_probe.py --rtu /dev/ttyUSB0 --unit 1 \
  --baudrate 9600 --parity N --stopbits 1 --bytesize 8

# 需要时明确指定寄存器映射
python tools/modbus_probe.py --tcp 192.0.2.20:502 --unit 1 \
  --map '{"temperature":{"address":0,"scale":0.1,"encoding":"int16","type":"holding"},"humidity":{"address":1,"scale":0.1,"encoding":"uint16","type":"holding"}}'
~~~

探针输出的温度/湿度应与设备屏幕或参考仪表大致相符；如地址/比例不对，先修正探针映射再部署。探针不能替代校准。

默认寄存器布局为：holding 地址 0 = signed temperature × 0.1 °C，地址 1 = unsigned humidity × 0.1 %RH，地址 2 = 状态（值 1 表示在线）。地址 2 的状态字段可以省略；省略时读寄存器成功即视为在线。手册中的 40001/30001 通常代表对应寄存器区的第一个地址，软件应填 PDU 地址 0；不得把 40001 直接填入 address。

#### B3. 配置 TCP 或 RS485/RTU

在部署 .env 配置。JSON 映射建议写单行，address 使用零基地址：

~~~dotenv
MODBUS_ENABLED=true
MODBUS_TRANSPORT=tcp
MODBUS_HOST=192.0.2.20
MODBUS_PORT=502
MODBUS_UNIT_ID=1
MODBUS_DEVICE_ID=TH-12
MODBUS_POLL_INTERVAL_SECONDS=5
MODBUS_TIMEOUT_SECONDS=5
MODBUS_REGISTER_MAP={"temperature":{"address":0,"scale":0.1,"encoding":"int16","type":"holding"},"humidity":{"address":1,"scale":0.1,"encoding":"uint16","type":"holding"}}
~~~

RTU 把传输改成 rtu，按设备手册设置端口、波特率、奇偶校验、停止位和数据位：

~~~dotenv
MODBUS_ENABLED=true
MODBUS_TRANSPORT=rtu
MODBUS_SERIAL_PORT=/dev/ttyUSB0
MODBUS_BAUDRATE=9600
MODBUS_PARITY=N
MODBUS_STOPBITS=1
MODBUS_BYTESIZE=8
MODBUS_UNIT_ID=1
MODBUS_DEVICE_ID=TH-12
~~~

RTU 物理层注意事项：

- RS485 A/B 极性名称在厂商间不总是一致，按设备手册和现场标签确认；屏蔽、参考地、终端/偏置按设备网络设计执行。
- 一条半双工总线只允许一个主站轮询。核对总线上是否已有 HA、网关或 PLC 主站。
- Linux Compose 部署要把实际串口映射进容器，并确保 uid 1000 用户有串口组权限；compose.yaml 提供了注释示例，宿主机组 GID 需现场查询。HA Add-on manifest 已声明 uart: true。
- USB 设备重新枚举成新的 /dev/ttyUSB* 名称时，更新 MODBUS_SERIAL_PORT 并重启服务。
- 配置 MODBUS_ENABLED=true 后重启服务生效。寄存器/端点错误只会禁用 Modbus 采集并记录日志，Flask 仍可能正常响应健康检查。

#### B4. 验证 Modbus → SQLite → 控制台

1. 检查探针单次读取值和 Unit ID。
2. 重启后查看服务日志中的“Modbus 采集线程启动”及轮询结果。
3. 调用以下只读接口，使用详情 API 密钥：

   ~~~sh
   curl -fsS -H "X-History-Key: <HISTORY_API_KEY>" \
     "http://<temperature-monitor-host>:5000/api/devices/TH-12?source=modbus"
   curl -fsS -H "X-History-Key: <HISTORY_API_KEY>" \
     "http://<temperature-monitor-host>:5000/api/system/status"
   ~~~

4. 连续观察多个轮询周期，确认 last_sample_time 持续更新、数值合理、失败计数回落到 0；断开恢复应出现来源为 modbus 的在线/离线迁移事件。
5. 在 /console 查看设备来源和最后样本。确认飞书实时设备行不会因当前直采功能自动变化；如业务要求该效果，需补建写入投影链路。

Modbus 数值在超出 -50..100 °C 或 0..100 %RH 时会按无值处理并写日志。地址错误、读超时、串口断开会使本轮失败；连续失败会在统一设备模型形成离线状态迁移，不会让 Flask 进程退出。

### 3.6 监控标准和自动化是独立交付项

传感器接入成功只证明数据采集链路工作，不代表该监测点已获得业务标准或正式报警能力。

- 受控监测点必须由标准负责人提供已批准、当前有效的标准编号/版本、区域、控制类型、适用工艺和温湿度上下限；缺来源或限值时按待标准处理，不从历史曲线推算，也不填虚构阈值。
- 温湿度监测的飞书入口是 [NoEE 温湿度监测台账](https://qjvasb3rm2.feishu.cn/wiki/PKKJwvD8ei7zL3k3Dbkcj2S8nhc?table=tblc6uCFLGPZLcR6&view=vewo6x1Acl) 同一 Base。设备身份/当前状态维护在《设备温湿度记录》，运行标准维护在《环境标准表》；不要把“当前适用上下限”展示列当成标准来源。
- 标准表字段及生效门禁见下方操作步骤和 [标准事实源说明](standard-source-of-truth.md)。只有完整快照校验成功并被运行时激活，飞书表里的记录才算进入系统。
- 新设备若加入 Shadow，配置区域/身份上下文并加入 SHADOW_DEVICE_IDS；只有完成标准、观察源、运行状态和差异核验后才进入下一阶段。
- Active 写回还需要独立变更审批、白名单、写入总开关和 cutover acknowledgement。普通传感器接入不要顺手启用 Active；默认模式 disabled 不影响基础采集。
- EVENT_TEMPERATURE_HIGH_C 是可选全局温度事件阈值，不等同于每设备的正式工艺标准。

#### 新建温湿度标准：从台账到运行时的逐步操作

**适用人：**已获标准表编辑权限的标准维护人员；限值与适用范围由标准批准人确认。现场接线/采集维护人员可以提交申请，但不能自行决定限值或启用标准。

1. **先确定监测点身份。** 打开上方台账中的《设备温湿度记录》，确认这确实是新监测点而非现有设备更换传感器。新点使用唯一、稳定的设备编号（例如 `TH-12`），并填正式区域、控制类型和责任人；设备编号要与 HA 上报映射或 Modbus `device_id` 完全一致。该表的当前上下限仅用于展示，不在此处建标准。
2. **拿到批准依据。** 标准负责人提供批准文件编号/版本/条款、批准人、目标区域/设备/作业类型、控制类型、温度与湿度上下限及计划生效时间。若文件没有某一维度的正式上下限，暂停标准同步配置并交由系统维护人评估数据模型/校验是否需调整；不要留空、借用别的设备限值或编造范围。
3. **打开正确的表。** 在同一个《NoEE 温湿度监测台账》Base 中切换到《环境标准表》，点击表格底部的“+ 新增记录”入口（飞书版本不同，按钮位置或文案可能略有差异），创建一行。不要改《设备温湿度记录》的“当前适用温度/湿度上下限”，也不要用设备表替代标准版本台账。
4. **判定是新标准还是修订。** 若是既有逻辑标准换版，沿用同一“标准编号”，填写新的“版本”，保留旧行；若是经批准的独立控制要求，才使用标准负责人分配的新编号。当前 `ENV-LEGACY-TH-*` 是迁移标识，不能直接当作已批准文件编号。不要为了绕过冲突随意改编号、优先级或旧行内容。
5. **按字段契约完整填写。** 当前运行时代码要求每一行的标准运行字段完整，包括停用行；各字段的必填/可空规则见下表，备注、最后确认人是管理追溯字段。`适用设备` 必须是设备表中的精确设备编号。`适用作业类型` 可以为空，表示该设备的通用标准。控制类型只能选 `全天控制`、`作业期间控制`、`仅监测`，且须与标准批准范围一致。温度、湿度四个上下限均须填写有限数字，且各自下限严格小于上限；不要把 `%RH` 写成 0–1 的小数比例。

   填行前可先从批准文件抄出这张录入卡，再逐字段录入；尖括号内容是待填提示，不能原样保存进表格：

   | 字段 | 本次批准值 |
   | --- | --- |
   | 标准编号 / 版本 | `<标准负责人分配的编号>` / `<新版本>` |
   | 适用区域 / 适用设备 | `<设备表区域原文>` / `TH-12` |
   | 适用作业类型 / 控制类型 | `<正式工艺名或留空>` / `<审批确认的控制类型>` |
   | 温度下限 / 上限 | `<来源文件数值> °C` / `<来源文件数值> °C` |
   | 湿度下限 / 上限 | `<来源文件数值> %RH` / `<来源文件数值> %RH` |
   | 生效 / 失效时间 | `<含时区的批准时点>` / `<含时区的结束时点或留空>` |
   | 优先级 / 是否启用 | `<批准的整数优先级>` / `按批准状态填写；正式记录首次进入同步表时即确定` |
   | 来源文件 / 条款 | `<正式文件编号及版本>` / `<具体条款>` |

   Base 表若没有上述字段、字段类型不匹配，或选项不能接受正式值，先停止编辑并请台账管理员与系统维护人核实字段契约；不要自行改成近似字段名。

   | 《环境标准表》字段 | 新行怎么填 |
   | --- | --- |
   | 标准编号、版本 | 按第 4 步确定；修订创建新版本，不覆盖旧版本 |
   | 适用区域 | 与《设备温湿度记录》的区域正式名称逐字一致 |
   | 适用设备 | 必填，填写设备表中的唯一编号，例如 `TH-12`；本部署的同步校验不接受空值 |
   | 适用作业类型 | 工艺专用标准填正式工艺名；通用标准留空 |
   | 控制类型 | 必填：`全天控制` / `作业期间控制` / `仅监测` |
   | 温度下限（°C）、温度上限（°C） | 两项必填；从批准文件填写，下限小于上限 |
   | 湿度下限（%RH）、湿度上限（%RH） | 两项必填；从批准文件填写，下限小于上限 |
   | 生效时间、失效时间 | 生效时间必填；填写有时区含义的切换时点。失效时间可空；有值时它是不再适用的边界时刻 |
   | 优先级、是否启用 | 优先级填写整数；待审批草稿留在审批资料中，不进入同步表。双人复核完成后，正式记录首次进入同步表时填写批准状态；未来生效记录使用批准的未来生效时间 |
   | 来源文件、条款 | 来源文件必填并可追溯；条款按批准文件填写 |
   | 备注、最后确认人 | 用于管理追溯，不代替标准编号、来源文件或审批记录 |

6. **检查覆盖和冲突。** 按设备编号、区域、作业类型及有效期筛查《环境标准表》。确认该设备在切换时刻有且只有一个适用结果；检查同一标准的版本没有相同生效时刻，其他启用标准没有相同匹配层级/优先级的重叠冲突。标准优先级不能用来掩盖错误范围或重复记录。
7. **复核后提交不可变版本。** 复核人对照批准文件核对编号/版本、适用设备、区域、控制类型、单位、四个限值、生效期、来源和启用状态。审批完成后再把最终记录加入同步表，首次同步时即填写批准的启用状态；未来生效的记录以正式启用状态录入，并设置批准的未来 `effective_from`。已同步的 `(standard_id, revision)` 包括启用状态在内均不可原地修改；需要改变业务内容时必须建立新版本。不要先把正式记录以 `是否启用=false` 同步，再改成 `true`，这种改动会被同步器拒绝。
8. **等待同步并查系统状态。** 运行时通过周期任务读取整张《环境标准表》，不是保存飞书单行就即时生效。由系统维护人使用带 `X-History-Key` 的 `GET /api/system/status` 检查 `latest_sync_status=SUCCEEDED`、`standards_ready` 与最近成功同步时间；若运行配置没有启用 Shadow/Active，状态接口可能显示运行时不可用，此时由维护人查看标准同步日志/审计结果，不能用基础 `GET /health` 代替标准同步证明。
9. **核验目标设备实际采用的版本。** 系统维护人可用只读 `GET /api/thresholds` 查看 `authoritative_source=feishu`、`standards_ready` 和已激活 `items`，按 `device_id` 核对新标准编号、版本、区域、控制类型和四个限值；随后在《设备温湿度记录》或监控台确认目标设备显示的当前适用标准及上下限，并确认同区域其他设备没有被误改。接口只读，不要尝试通过本地 threshold PUT 改标准。
   HA 路径还应核对《设备温湿度记录》的温湿度和更新时间确实由该设备上报；当前 Modbus 直采路径只写本地服务/SQLite/控制台，不会自动投影到飞书设备实时表。若现场要求 Modbus 也更新该行，必须另行交付并验证 Feishu 写入投影，不能把本地 API 有样本当成已回写台账。
10. **闭环留痕。** 在标准变更记录中保存变更单号、批准依据、旧/新编号版本、设备和范围、计划生效时间、飞书创建/复核人、同步状态与时间、现场读数核对人，以及通知对象。

**失败处理：** 如果标准表任一行的必填字段非法、有效期或候选标准冲突，整份快照同步失败，当前有效快照保持不变。修正表格后等下一次同步，再重复第 8–10 步；不要把“飞书已保存”写成“系统已生效”。在首次成功严格同步前，监控不得被视为拥有正式标准。已同步标准的停用/撤销没有原地修改通道，须先由系统维护人设计新版本与完整快照迁移，不要直接改旧行的启用状态或限值。

**字段核对提示：** 代码配置还映射了标准字段《控制类型》，但现有字段清单文档没有记录它的飞书 field ID。首次按此流程操作前，台账管理员需在《环境标准表》确认字段确实存在且选项值与上面枚举一致；若缺少字段或字段名不同，先由系统维护人同步更正 Base 字段或代码映射，并完成一次成功同步，再启用新标准。

## 4. 服务巡检与常见故障

### 4.1 日常巡检建议

以下是建议检查频率，现场可按 SLA 调整：

| 周期 | 检查项 |
| --- | --- |
| 每班/每日 | /health、设备最后样本时间、可用状态、HA heartbeat、飞书待投影/失败数、未闭环事件。 |
| 每周 | SQLite 可写性、磁盘空间、日志轮转、备份是否可读及 quick_check 结果。 |
| 每月或发布前 | 传感器与参考仪表比对、设备/标准责任人复核、备份恢复演练、密钥和依赖更新窗口。 |

健康地址是 GET /health；Active 告警链路严格就绪地址是 GET /readyz。只有 Active/Shadow 依赖的就绪异常时再排 Runtime/标准同步；基础采集模式 disabled 下，不能用 /readyz 代替样本检查。

Compose 常用巡检命令：

~~~sh
docker compose ps
docker compose logs --tail=200 temperature-monitor
curl -i http://127.0.0.1:5000/health
curl -i http://127.0.0.1:5000/readyz
df -h
du -sh data logs
~~~

受保护诊断的建议顺序：

1. 先验证服务本身和部署容器；HTTP 200 只能说明服务可响应。
2. 用 GET /api/devices/TH-12?source=home_assistant 或 source=modbus 检查来源、状态、温度、湿度和 last_sample_time。多来源设备必须指定 source。
3. 用带 X-History-Key 的 GET /api/system/status 查看 collectors、device_model 和 feishu_projection。Modbus 看 enabled/running、last_success、last_error_summary、consecutive_failures；HA 看设备模型健康/断流和飞书投影 pending/failed 诊断。
4. 用 GET /api/events?device=TH-12 核对设备在线/离线迁移；heartbeat 本身不应制造 measurement 样本。
5. 需要本地历史快照时，用 GET /history/query?device=TH-12&limit=20 核对最新采样时间。飞书历史表还需单独检查表内记录。

示例：

~~~sh
curl -i http://127.0.0.1:5000/health
curl -fsS -H "X-History-Key: <HISTORY_API_KEY>" \
  "http://127.0.0.1:5000/api/devices/TH-12?source=home_assistant"
curl -fsS -H "X-History-Key: <HISTORY_API_KEY>" \
  "http://127.0.0.1:5000/api/system/status"
curl -fsS -H "X-History-Key: <HISTORY_API_KEY>" \
  "http://127.0.0.1:5000/api/events?device=TH-12&limit=20"
curl -fsS -H "X-History-Key: <HISTORY_API_KEY>" \
  "http://127.0.0.1:5000/history/query?device=TH-12&limit=20"
~~~

验收建议：HA 至少核对一条真实测量和下一分钟 heartbeat；Modbus 至少连续读取 3 个轮询周期。需要历史表时，等待下一次整 10 分钟采样完成再查。把平台值与设备屏幕/参考仪表对比，允差采用设备规格或批准的校准要求，不由本手册另定。
/api/devices、/api/events、/history/query 和控制台数据请求需有效 HISTORY_API_KEY。密钥通过 X-History-Key 请求头或 Bearer token 传入，不能放在 URL。查询详情优先用受保护 API，匿名 /api/system/status 只用于健康聚合，不作为访问控制替代。

### 4.2 故障速查

| 现象 | 优先检查 |
| --- | --- |
| HA 测量无效或 HTTP 400 | 实体是否为数字；有无把 unknown/unavailable 当数值；HA 与 SOURCE_TEMPERATURE_UNIT 是否一致；范围是否超限；设备编号是否为空。 |
| HA 返回 HTTP 401 | TEMPERATURE_API_KEY 与 HA secret 是否一致；Header 名为 X-Temperature-Key。 |
| 本地 API 有样本但飞书实时行不更新 | 返回体若是 accepted，检查 /api/system/status 中投影队列/错误和服务日志；核对飞书应用权限、APP_TOKEN、TABLE_ID、设备编号唯一性及 DEVICE_ID_FIELD。 |
| 找不到设备或飞书提示重复 | 检查实时表中设备编号是否有且仅有一条匹配；确认 HA 上报 ID，必要时检查 DEVICE_NAME_MAP / DEVICE_RECORD_MAP。 |
| 历史表没有记录 | HISTORY_API_KEY、HA 定时自动化、HISTORY_DEVICES 与 HISTORY_TABLE_MAP 是否完整一致；核对每设备历史表 ID 和实时表读取是否成功。 |
| Modbus 探针连不上 | TCP 检查路由、防火墙、IP/端口；RTU 检查串口节点、权限、线序和总线是否已有主站；再核对 Unit ID、波特率、校验位。 |
| Modbus 能连接但温湿度不对 | 核对 FC03/FC04 类型、零基地址、int16/uint16、scale、单位及探针实际输出；当前解码器不支持 float32 和偏移换算。 |
| /health 200 但设备离线 | 服务进程健康不代表传感器链路健康；查受保护的设备详情、最后样本时间、Modbus 连续失败数和 HA entity/automation。 |
| /readyz 503 | 该接口严格检查告警运行时就绪。查 Runtime 是否启动、标准同步是否成功、任务是否阻塞及 readiness 缓存；再判断是否影响现场数据采集。 |
| Docker 数据库/日志不可写 | 核对 data/、logs/ bind mount 是否存在，以及 uid/gid 1000 的宿主机权限。 |
| 容器活着但查询接口 503 | 核对 SQLITE_ENABLED、数据库初始化错误与 SQLITE_DB_PATH；API 会在本地镜像不可用时返回 503。 |
| `/temperature` 或 heartbeat 返回 503 | 检查 `TEMPERATURE_AUTH_REQUIRED` 是否开启但 `TEMPERATURE_API_KEY` 为空；生产部署至少 32 字节，并与 HA `X-Temperature-Key` 对齐。 |

### 4.3 备份与恢复

Compose 部署将数据库保存在宿主机 data/temperature_monitor.db。使用 SQLite online backup API 一致性备份（包含 WAL 中已提交内容），并执行 quick_check：

~~~sh
mkdir -p data/backups
python tools/sqlite_backup.py \
  --source data/temperature_monitor.db \
  --backup data/backups/temperature_monitor_manual_20261023T010000Z.db
~~~

将备份复制到独立存储。确认输出 quick_check=ok 后记录备份时间、版本和存放位置。HA Add-on 要替换 source 为实际 SQLITE_DB_PATH（默认 /data/temperature_monitor.db），且备份文件需写入持久 /data 或导出至外部存储。

GitHub 自动部署当前不会创建 SQLite 备份；有 schema 迁移或高风险改动的发布，必须先完成上述备份并确认可用，再允许发布。部署脚本的镜像回滚不会恢复数据库。普通代码发布也应遵守现场备份计划，并确保最近的可恢复副本在独立存储。

备份保留工具先预览再评估；只有确认候选文件可删除后才使用 --apply：

~~~sh
python tools/backup_retention.py --directory data/backups
# 人工核对输出后，才考虑：
python tools/backup_retention.py --directory data/backups --apply
~~~

该工具只处理符合其自动备份文件命名的数据库；手工快照建议使用带 manual 的名称并单独按保留制度管理。不要删除唯一恢复点。

恢复前停止写入服务，先把当前数据库另存一份，再将确认可用的备份恢复到配置的 SQLite 路径并启动服务；不要在服务运行时直接覆盖主库，也不要只复制主库而忽略正在使用的 WAL 状态。启动后确认 /health、受保护查询和备份 quick_check。恢复意味着回到快照时间，快照之后的数据可能需要从飞书/上游补回。

标准 Compose 恢复示例（在项目目录执行；按实际 SQLITE_DB_PATH 修改 DB_PATH，并把 BACKUP 指向已验收的快照）：

~~~sh
set -euo pipefail
DB_PATH=data/temperature_monitor.db
BACKUP=data/backups/temperature_monitor_manual_YYYYMMDDTHHMMSSZ.db

docker compose stop temperature-monitor
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DB_DIR=$(dirname "$DB_PATH")
RECOVERY_DIR="$DB_DIR/recovery-$STAMP"
mkdir -p "$RECOVERY_DIR"
cp "$DB_PATH" "$RECOVERY_DIR/temperature_monitor.db"

# 停服务后先保存旧 WAL sidecar，避免它被错误应用到恢复快照。
for suffix in -wal -shm -journal; do
  if [ -e "$DB_PATH$suffix" ]; then
    mv "$DB_PATH$suffix" "$RECOVERY_DIR/temperature_monitor.db$suffix"
  fi
done

cp "$BACKUP" "$DB_PATH"
python3 - "$DB_PATH" <<'PY'
from pathlib import Path
import sqlite3
import sys

database = Path(sys.argv[1]).resolve()
connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
try:
    result = connection.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"quick_check failed: {result}")
    print(result)
finally:
    connection.close()
PY

docker compose up -d --wait --wait-timeout 600
curl --fail http://127.0.0.1:5000/health
~~~

只有 quick_check 输出 ok 才继续核对 API 和业务数据。HA Add-on 请先在 Supervisor 停止 Add-on，再按实际 /data 挂载执行同样的主库/sidecar 保存与恢复流程；恢复后再启动 Add-on。

## 5. 发布、升级和回滚

### Docker Compose 生产机

仓库 CI 对 PR 和 push 执行 Python 3.12、Ruff、pytest 与前端异步检查。主分支测试成功后，GitHub Actions 按提交 SHA 构建、推送并部署；部署流程检查镜像、/health 和运行时状态，失败时尝试回滚旧镜像。服务器 .env、data/、logs/ 会保留。

当前部署 workflow 明确关闭了部署时 SQLite 备份/全库扫描。因此数据库 schema 变更或高风险发布前应手动备份；自动镜像回滚不会自动回滚已经执行的数据迁移。

常规更新不执行 docker compose down -v、git clean -fdx 或删除 data/、logs/。发布和回滚使用经确认的精确 SHA 镜像，启动后复查样本接入、SQLite 和飞书投影。

手动部署按已验证的 commit SHA 固定镜像版本。在项目目录执行，替换仓库地址和 SHA：

~~~sh
export DEPLOY_IMAGE="your-registry/your-namespace/temperature-monitor:REPLACE_WITH_COMMIT_SHA"
docker compose -f compose.yaml -f compose.deploy.yaml pull
docker compose -f compose.yaml -f compose.deploy.yaml up -d --remove-orphans --wait --wait-timeout 600
docker inspect temperature-monitor --format '{{.Config.Image}}'
curl --fail http://127.0.0.1:5000/health
~~~

若 AUTOMATION_MODE 为 active 或 shadow，再查 /readyz 和运行时诊断。当前 workflow 会把 readiness 未就绪作为警告并保留仍存活的新镜像；不要把 /readyz 单独 503 当作容器损坏。\n手动回滚时，使用已记录并确认可用的旧 SHA 镜像，保持当前 .env、data/ 和 logs/：

~~~sh
export DEPLOY_IMAGE="your-registry/your-namespace/temperature-monitor:REPLACE_WITH_KNOWN_GOOD_SHA"
docker compose -f compose.yaml -f compose.deploy.yaml pull
docker compose -f compose.yaml -f compose.deploy.yaml up -d --remove-orphans --wait --wait-timeout 600
curl --fail http://127.0.0.1:5000/health
~~~

部署 workflow 会在 pull/up/存活或 Runtime 检查失败时尝试恢复旧镜像；它不会还原数据库快照。

### Home Assistant Add-on

Add-on 版本定义在 [hassio/temperature-monitor/config.yaml](../hassio/temperature-monitor/config.yaml)。发布前更新版本号，提交后由成功的 Tests workflow 触发 GHCR 发布。安装端从项目仓库升级 Add-on，并核对 options、/health 与新设备数据。Home Assistant Add-on 与 Compose 使用同一应用代码，但数据目录、密钥注入和硬件串口映射不同。

### 变更记录至少包含

- 变更目的、设备编号和现场影响；
- 修改的配置键、标准/寄存器手册版本、发布 commit SHA；
- 上线前数据库备份位置和 quick_check 结果；
- 上线后健康/就绪、样本时间、数值比对、飞书行/历史表检查；
- 回滚版本、执行人、时间和观察结果。

## 6. 安全、数据与职责边界

- .env、HA secrets.yaml、飞书 App Secret、API key、真实 APP_TOKEN / table ID 不提交到代码库；对外发送日志前移除凭据、个人信息和内网信息。
- Compose 生产部署和 Add-on 必须配置至少 32 字节的 TEMPERATURE_API_KEY，并在 HA secrets.yaml 为测量和 heartbeat 发送 X-Temperature-Key；HISTORY_API_KEY 单独保护历史采样和本地查询。代码默认保留旧式无鉴权兼容，但生产 Compose/Add-on 默认强制鉴权。
- SQLITE_ENABLED=true 为默认，本地数据库支持查询、事件和持久运行状态；SQLite 是本地镜像/运行存储，生产环境标准的权威源仍是飞书 validated standard。
- 传感器测量、在线状态、温度事件、正式标准报警、飞书通知和事件闭环是不同层次。单看“温度在区间内”或服务健康 200，不代表质量放行或异常已经闭环。
- 设备编号、飞书设备行、标准记录、HA 自动化、历史表映射、现场责任人应由各自系统负责人一起验收。

## 7. 新设备交接验收清单

- [ ] 设备编号唯一、区域/责任人/序列号已登记。
- [ ] 传感器安装、供电、网络/总线及参考仪表比对完成。
- [ ] 已明确 HA 或 Modbus 路径；Modbus 参数和寄存器定义来自设备手册。
- [ ] 单位、采样周期、离线处理和密钥 Header 已核对。
- [ ] HA 实时上报与每分钟 heartbeat 均已验证；或 Modbus 探针与多个真实轮询周期已验证。
- [ ] SQLite 设备详情显示正确来源、采样时间和合理数值。
- [ ] 若适用，飞书实时行、历史表和唯一设备编号均已核对。
- [ ] 若涉及受控标准，批准版本、生效期、区域/工艺匹配及责任人已核实。
- [ ] Shadow/Active 变更与普通设备接入分开审批，没有因接入自动打开 Active。
- [ ] 维护联系人、备份位置、发布 SHA、回滚版本和异常升级路径已移交。
- [ ] HA 实测量/heartbeat、Modbus 连续 3 个轮询周期、历史采样桶及仪表比对结果已记录；允差来自规格/批准要求。
- [ ] 部署运行档案、设备登记卡、secret 存放位置、备份文件和最近可回滚 SHA 均已填写。

### 数据保留策略状态

当前仓库未配置 `temperature_reports`、`device_samples`、`device_events` 等原始测量与设备事件表的周期性自动清理。不得把备份清理策略当作业务数据保留策略，也不要直接执行全表删除或大范围 VACUUM。`tools/data_retention_preview.py` 只用于只读统计候选数据量，不会删除记录。正式保留期限、例外和归档位置需由质量/业务负责人确认后，再由系统维护人设计分批清理、审计水位及恢复方案；决定尚未批准前保留现状并纳入交接风险项。

交接人：________  接手人：________  复核人：________  时间：________

## 8. 配套资料

- [README 快速入门、部署与 API](../README.md)
- [开发和验证约定](development.md)
- [HA freshness contract](ha-freshness.md)
- [飞书字段映射](feishu-mapping.md)
- [环境标准事实源](standard-source-of-truth.md)
- [领域模型](domain-model.md)
- [现场温湿度控制工作交接](handover.md)
- [Add-on 使用说明](../hassio/temperature-monitor/README.md)
