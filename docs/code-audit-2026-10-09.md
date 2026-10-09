# 全仓库代码审计（2026-10-09）

## 范围与结论

审计对象为当前工作区，包含上一轮尚未提交的优化。扫描了全部 119 个第一方 Python 文件，其中业务代码与工具 63 个、约 27,655 行，测试文件 56 个；同时检查了控制台 JavaScript、Docker、Compose、Home Assistant 配置、部署脚本与 GitHub Actions。

方法为全仓库语法和风险模式扫描、关键路径人工追踪、临时 SQLite 数据库故障注入、HTTP 响应模拟及 JavaScript 异步响应模拟。没有访问真实飞书，没有写入生产数据库，没有部署或发布镜像。第三方 `static/chart.umd.min.js` 未逐行审计，也没有进行依赖漏洞数据库扫描。此审计不等于每种配置和每行代码均得到形式化验证。

发现 **7 项 P1 和 6 项 P2**。优先处理派发确认、跨仓储事务和样本顺序，再处理 Token 与历史幂等；镜像构建边界也应在下一次发布前修正。现有测试通过，仍无法覆盖下面复现的失败模式。

## 修复进展（同日续修）

下面的发现和复现输出保留审计时的历史状态；原行号对应审计时工作区。A01–A13 已修复并完成回归验证。

| 编号 | 已落实的修复 |
| --- | --- |
| A01 | Docker 构建排除 `.env.*`、虚拟环境、临时目录和私钥文件，保留 `.env.example`。 |
| A02 | 必需监听器显式确认成功；异常、返回 False 或没有业务消费者时保留待派发水位，可失败观测钩子可声明 `required=False`。 |
| A03 | 最新样本、告警状态、任务和事件意图在共享 SQLite 工作单元中提交；外部请求在事务之外执行。 |
| A04 | 镜像写入使用事务边界，失败整体回滚；仓储重试对非锁异常也回滚，外层事务中的失败交由外层回滚。 |
| A05 | 应用层拒绝旧观察驱动告警；最新样本仓储在写事务内比较真实时间，兼容配置时区中的无时区时间。 |
| A06 | 单次有界调用发现无效 Token 也立即清理缓存，下次任务可获取新 Token。 |
| A07 | 历史幂等标识由表、设备、采样桶生成；外部失败后重新读取远程水位；复用首次持久化的快照内容。 |
| A08 | HTTP 心跳原子持久化 presence 和待派发队列，由调度器恢复投递，确认后删除；重启或消费者失败均保留队列。 |
| A09 | Bitable 业务层统一控制调用次数，每次 HTTP 请求仅一次尝试；网络、业务错误和 Token 重试共用预算。 |
| A10 | 无鉴权状态接口仅返回健康和计数摘要；设备诊断详情需有效密钥，错误密钥返回鉴权错误。 |
| A11 | 窗口平均值按各指标有效样本数量加权，缺失值不进入分母。 |
| A12 | 趋势请求绑定选择与请求序号，旧响应不覆盖当前设备；回退请求纳入同一错误处理链。 |
| A13 | 停止超时保留采集线程引用并禁止重复启动；停止信号到达后不再发布当次轮询结果。 |

续修验证：`python -m pytest -q`：**656 passed**；Ruff、`pip check`、`git diff --check` 通过；`node tests/console_async.test.cjs` 通过。测试使用临时 SQLite 和模拟外部响应，本次全量运行没有警告（此前 Python 3.14 transport 析构警告为间歇性）。

额外修正：请求数值拒绝布尔值。新增 `tests/test_audit_fixes.py` 的 16 项故障注入回归，以及 `tests/console_async.test.cjs` 的趋势异步行为验证。

尚未实施下文的可选长期优化：读取过滤、本地数据保留、调度进度探针及模块拆分。保留期限等业务策略需要另行确定。本次没有调用真实飞书或发布部署；飞书幂等有效期与真实生产链路仍需上线验证。

## P1：优先修复

### A01 镜像构建上下文可能带入本地密钥与虚拟环境

位置：[.dockerignore](/Users/neilchen/Projects/temperature-monitor/.dockerignore:8)、[Dockerfile](/Users/neilchen/Projects/temperature-monitor/Dockerfile:9)。

`COPY . .` 复制全部未被 Docker 忽略的内容，而 `.dockerignore` 只排除了准确名称 `.env`，没有排除 `.env.local`、`.env.production`、`.venv/`、`.codex-tmp/` 和证书私钥等。Git 忽略规则不会自动变成 Docker 忽略规则。只要从存在这些文件的本地目录构建，它们就可能进入镜像；本仓库当前存在 `.venv/`，还有本轮临时检查目录。

这是构建规则缺口，**没有证据表明已发布镜像包含真实凭据**。CI 的干净检出也不能消除本地构建风险。

建议：排除 `.env.*` 并保留 `.env.example`，排除虚拟环境、临时目录和私钥；最好按明确的运行时文件清单 COPY。回归应通过构建上下文或镜像文件清单确认这些文件不存在。

### A02 运行时监听器失败后仍确认派发成功，丢失自动补处理机会

位置：[devices.py](/Users/neilchen/Projects/temperature-monitor/services/devices.py:311)、[projection.py](/Users/neilchen/Projects/temperature-monitor/services/projection.py:652)。

`_notify_sample_listeners()` 捕获并吞掉监听器异常；调用方没有成功/失败返回值。`dispatch_projected_sample()` 随后无条件推进 `last_dispatched_sample_time_ms`。当运行时因数据库写入失败而没有完成处理时，原始样本虽然保留，派发水位却声明处理已完成；恢复扫描不会再发现这次遗漏。

复现：注入抛出 `RuntimeError` 的监听器，派发返回 `True`，派发水位仍推进到该样本时间。

建议：区分必须成功的业务消费者和可失败的观测钩子；必须获得业务处理提交确认才能推进水位。业务消费者失败应保留可重放身份和错误状态，重放过程再通过业务幂等控制副作用。无监听器或监听器尚未启动时也需要明确确认策略。

### A03 告警状态、任务和事件没有统一的事务提交边界

位置：[monitor_service.py](/Users/neilchen/Projects/temperature-monitor/application/monitor_service.py:237)、[monitor_service.py](/Users/neilchen/Projects/temperature-monitor/application/monitor_service.py:286)。

样本处理先在 `_project_local_actions()` 中创建或取消任务、创建或恢复事件，各仓储分别提交，然后才保存告警状态。任何中间异常或进程中断都会留下相互矛盾的持久化状态。共享锁只约束并发，不提供跨提交原子性。

复现：先产生 PENDING 告警和验证任务，再输入正常样本；注入告警状态 `save()` 失败后，数据库中仍是 `PENDING`，关联任务已成为 `CANCELLED`。这破坏状态与任务的一致性，并影响重启恢复或后续调度。

建议：引入应用级工作单元，在一个 SQLite 事务内保存本地状态、任务、事件和外部动作意图；仓储支持由调用者控制提交。网络请求必须在本地事务提交后执行。测试覆盖各本地写入边界的故障注入。

### A04 部分 SQLite 写入失败未回滚，后续无关写入会提交残留数据

位置：[db.py](/Users/neilchen/Projects/temperature-monitor/services/db.py:922)、[db.py](/Users/neilchen/Projects/temperature-monitor/services/db.py:962)、[sqlite.py](/Users/neilchen/Projects/temperature-monitor/repositories/sqlite.py:77)。

`save_device_sample()` 先 INSERT 样本，再更新 presence。发生 SQLite 错误后返回 `False`，却没有回滚。连接被多个调用复用，后续任意 `commit()` 可能提交上一次失败留下的局部数据。重试辅助函数也只在部分可重试锁错误上回滚，对其他错误及耗尽重试的错误缺少统一清理。

复现：presence 更新注入 `OperationalError`，保存返回 `False`，`connection.in_transaction=True`；随后保存另一个设备的温度审计，前一条失败样本被一起提交，presence 仍缺失。

建议：明确每个操作对事务的所有权；操作自有事务在任何异常上回滚，调用者自有事务使用 savepoint，不可把外层事务一起回滚。错误处理与回滚都应处在同一锁范围。统一检查所有镜像写入及仓储重试出口。

### A05 旧样本可覆盖最新运行时样本，并继续驱动当前告警

位置：[runtime_state.py](/Users/neilchen/Projects/temperature-monitor/repositories/runtime_state.py:480)、[monitor_service.py](/Users/neilchen/Projects/temperature-monitor/application/monitor_service.py:178)。

`SQLiteLatestSampleRepository.save()` 对冲突无条件覆盖时间、数值和在线状态，应用服务也没有先拒绝过时观察。较新的心跳可能先进入运行时，随后延迟投影的旧测量再进入；并发监听器竞争运行时锁也可能改变处理顺序。

复现：先保存 12:00 的 24°C，再保存 11:50 的 40°C，最新样本变成 11:50、40°C。应用服务会继续按当前 `now` 处理这个旧值，存在误触发、误恢复及推迟验证的问题。

建议：在应用入口统一校验观察顺序，并原子推进已接受观察水位；分别维护测量时间和心跳时间，明确同时间、多来源的排序策略。只给 latest 表加条件 UPDATE 不足以阻止旧样本驱动状态机。

### A06 单次尝试模式收到 Token 失效响应后不清缓存

位置：[feishu.py](/Users/neilchen/Projects/temperature-monitor/services/feishu.py:145)。

Token 失效分支把“清空缓存”和“本次请求能否继续重试”放在同一条件中。`max_attempts=1` 时该分支不执行，下一次调度退避后还会取得同一个未到本地过期时间的失效 Token，直到重试耗尽或缓存过期。延迟投影的有界请求正使用这一模式。

复现：返回业务码 `99991663`、`max_attempts=1`，`clear_token()` 调用次数为 0。

建议：识别失效响应后立即清缓存；是否立即获取新 Token 和重试另行判断。保留单次任务请求预算，下一次任务获取新 Token。通知客户端已经独立清缓存，可统一策略。

### A07 历史写入在跨调用重试时更换幂等键，可能重复新增记录

位置：[feishu.py](/Users/neilchen/Projects/temperature-monitor/services/feishu.py:764)、[history.py](/Users/neilchen/Projects/temperature-monitor/services/history.py:300)。

历史记录每次调用使用新的 UUID。一次调用内部重试复用 URL，但下一次 `/history/sample` 调用重新产生 UUID。若飞书已写入、响应丢失，本地缓存未推进；缓存里设备已经存在且值为 `None`，下一次也不会重新查询最新时间，直接用新 Token 再次 POST。

复现：模拟第一次远端插入后响应丢失，第二次同桶采样成功；两次 POST 的 Token 不同，最新时间只查了一次。状态依次为 502、200，远端可能保存两份相同业务记录。

建议：以目标表、设备和采样时间桶构造稳定幂等键，并明确结果不确定时的查询确认与重试策略。不同时间桶不能复用一个键；必要时持久化历史交付状态。上一轮本地优先落盘修复了本地缺口，尚未解决这一远端重复问题。

## P2：后续修复

### A08 心跳 HTTP 请求仍同步等待运行时和外部 I/O

位置：[temperature.py](/Users/neilchen/Projects/temperature-monitor/routes/temperature.py:438)、[shadow_runner.py](/Users/neilchen/Projects/temperature-monitor/runtime/shadow_runner.py:333)、[shadow_runner.py](/Users/neilchen/Projects/temperature-monitor/runtime/shadow_runner.py:463)。

温度投影采用调度器派发，但心跳直接调用 `devices.dispatch_sample()`，等待与调度器共享的 `_execution_lock`。锁内包括同步任务及告警写回的网络工作。网络慢时，心跳已经本地保存，HTTP 请求仍占着 Waitress 线程；足够多的心跳可挤占服务容量。

复现：实际运行时监听器等待被持有的 execution lock；presence 已落库，HTTP 请求仍未完成；释放锁后才返回 200。

建议：持久化待处理心跳或维护可恢复的观察水位，由单一运行时消费者处理。确认语义以本地接收为界，结合 A02 的成功确认，避免异步化后丢观察。

### A09 飞书有两层重试，默认一次操作最多进行 9 次 HTTP 请求

位置：[feishu.py](/Users/neilchen/Projects/temperature-monitor/services/feishu.py:127)、[http_client.py](/Users/neilchen/Projects/temperature-monitor/services/http_client.py:41)。

业务层最多尝试 3 次，每次又调用最多尝试 3 次的传输层。HTTP 500 被两层都视为可重试，默认实际发出 9 次请求；每页读取都会重复，且很多事件、通知和记录查找使用默认读取预算，仍处在单个运行时线程内。

复现：连续 HTTP 500，模拟 Session 的 `request()` 调用次数为 9。

建议：由单一层拥有重试预算，设置操作整体截止时间，统一处理 Token、限流和业务冲突；持久化任务交由调度器退避。对分页设置截止时间、页数或重复 page_token 检查。后续再考虑线程独有连接池，避免每次创建 Session 并强制 `Connection: close`。

### A10 无鉴权的系统状态接口返回具体设备数值

位置：[api.py](/Users/neilchen/Projects/temperature-monitor/routes/api.py:587)、[devices.py](/Users/neilchen/Projects/temperature-monitor/services/devices.py:188)。

模块契约称公开系统状态只包含健康汇总，但实际返回 `device_model.device_states`，包含设备标识、温湿度、来源和可用状态；还返回详细运行时、投影错误等信息。这使 `/api/devices` 的数据访问保护可被绕过一部分。

复现：配置 `HISTORY_API_KEY`、不发送任何鉴权头，`GET /api/system/status` 返回 200，含测试设备的 `device`、`temperature`、`humidity`。

建议：公开端点仅保留固定的健康计数和布尔状态，详细诊断要求现有 API 密钥。错误信息也应经过筛选。保留 `/health` 轻量探针用途，不需要隐藏其基本可用性结果。

### A11 看板窗口平均值使用“日均值的平均”，采样不均时失真

位置：[dashboard.py](/Users/neilchen/Projects/temperature-monitor/routes/dashboard.py:335)、[dashboard.py](/Users/neilchen/Projects/temperature-monitor/routes/dashboard.py:350)。

一天 1 条与另一天 9 条在窗口平均中具有相同权重。停机、补采、离线或部分缺值时，会明显偏离整个窗口中有效样本的平均值。

复现：第一天 1 条 10°C，第二天 9 条 30°C；页面平均为 20°C，有效样本加权平均为 28°C。

建议：窗口查询直接计算平均；或为温度、湿度分别提供有效样本 COUNT 和 SUM，再计算各维度窗口平均。不能直接按总 `sample_count` 加权，因为空温度、空湿度及离线样本的数量可能不同。若产品希望显示日均值平均，应明确改名。

### A12 趋势请求乱序会把 A 的数据画在 B 的标题下

位置：[console.html](/Users/neilchen/Projects/temperature-monitor/static/console.html:948)、[console.html](/Users/neilchen/Projects/temperature-monitor/static/console.html:990)。

请求发出时捕获设备，但 `renderTrend()` 使用当前全局选择。用户从 A 切到 B，B 响应先返回、A 响应后返回时，A 数据覆盖 B 图表，标题和阈值仍取 B。切换时间窗口也有同样问题。

复现：JavaScript 模拟先返回 B 再返回 A，绘制记录依次为 `{label:B, actual:B}` 和 `{label:B, actual:A}`。

此外，回退样本请求的 Promise 没有返回到外层链，也没有自己的 catch；回退请求网络失败或超时可能出现未处理拒绝。

建议：每次趋势加载分配请求序号或取消上一次请求；绘制前验证设备、来源、窗口和鉴权上下文仍匹配。把回退 Promise 纳入同一返回链。可进一步按时间桶下采样 Modbus 数据，而非固定最多 1000 条。

### A13 采集器停止超时后仍清空线程引用，允许旧线程污染新生命周期

位置：[collector.py](/Users/neilchen/Projects/temperature-monitor/services/collector.py:119)、[modbus_client.py](/Users/neilchen/Projects/temperature-monitor/services/modbus_client.py:503)。

`stop_collectors()` 只 join 5 秒，不检查线程是否仍存活，随后清空引用并复位 `_started`。Modbus 调用可能超出该等待时间；等待期间的 `poll_once()` 返回后也没有检查停止标志，仍会调用 `record_sample()`。

已确认控制流；未连接真实串口做故障时长测试。网络慢或超时配置较长时，调用方可能关闭 DB 或切换配置，而旧线程继续写入；下一次 start 还可能再创建一个线程。测试输出曾出现跨测试采集线程日志，但不以该日志单独认定具体因果。

建议：join 超时保留线程和 poller 引用，标记停止中并阻止重启；在 poll 返回后检查 stop 标志。可按传输类型设计安全中断与最大关闭时间。已有运行时停止逻辑保留超时线程资源，可复用其生命周期设计。

## 其他优化建议（不计入上述缺陷数）

1. **远端读取按记录或业务键过滤。** `FeishuBitableRecordSource.read_records()` 只提供全表读取，事件绑定、责任人解析、人工关闭与通知会反复分页读取同一张表。事件量增长后网络请求数和运行时锁占用一起增长。增加按 record_id、设备与业务时间查询的接口；缓存应有明确版本与失效边界。
2. **本地保留策略。** 自动清理覆盖了 automation runs/tasks 和可选飞书历史，但 `temperature_reports`、`device_samples`、`device_events`、CSV 及部分审计表没有统一保留策略。制定业务保留期限后分批清理；在重放、派发和审计水位之前的数据不可直接删除。避免全表 DELETE 或频繁 VACUUM 阻塞采集。
3. **部署与回滚一致性。** 部署 workflow 构建已测试 SHA，却把服务器目录 reset 到 `origin/main`，应固定到同一 SHA；回滚读取了 `OLD_IMAGE` 但主要恢复 `.env` 的 `OLD_TAG`，应优先记录并使用不可变的实际旧镜像引用。注意当前工作区 `compose.yaml` 的固定本地镜像改动是既有用户改动，本轮未修改或将其认定为代码缺陷。
4. **健康探针观察进度。** `thread.is_alive()` 能识别线程死亡，但识别不了活着却停滞的调度器；readiness 缓存也没有最大陈旧期限。增加最后完成 tick、阶段耗时、缓存年龄和适配器请求时长，使卡住的调度器可见，避免把存活与业务就绪混为一谈。
5. **输入与配置验证。** `parse_number(True)` 当前接受为 1、`False` 为 0，与领域层拒绝布尔数值的规则不一致。设备和状态字段也存在自动字符串化。启动时集中校验超时、线程数、有限浮点数、时区及重试范围；请求参数按明确类型拒绝异常输入。
6. **工程拆分与环境一致性。** `monitor_service.py`、`feishu_writers.py` 和 `db.py` 很大，应围绕事务工作单元、事件写回、通知及观测存储逐步拆分，保留现有接口和回归行为。CI 使用 Python 3.12，而本地虚拟环境为 3.14；固定依赖解析结果并增加一致的测试环境。当前没有因本地版本而宣称第三方漏洞。

## 验证与复现

全仓库 AST 解析：119 个 Python 文件全部通过。

离线故障注入脚本位于当前工作区的忽略目录，不属于生产代码：

- [audit-repros.py](/Users/neilchen/Projects/temperature-monitor/.codex-tmp/audit-repros.py)：Token、样本顺序、告警半提交、事务残留、历史幂等、派发确认。
- [audit-http.py](/Users/neilchen/Projects/temperature-monitor/.codex-tmp/audit-http.py)：公开状态字段、平均值、心跳阻塞。
- [audit-trend.cjs](/Users/neilchen/Projects/temperature-monitor/.codex-tmp/audit-trend.cjs)：趋势请求乱序。

复现输出：

```text
TOKEN_INVALID: 99991663 cache_clears=0
LATEST_SAMPLE_REGRESSED: True value=40.0
PARTIAL_ALARM_TRANSITION: PENDING task=CANCELLED
FAILED_WRITE_LATER_COMMITTED: returned=False transaction_open=True rows_after_other_write=1
AMBIGUOUS_HISTORY_RETRY: statuses=(502,200) posts=2 distinct_tokens=2 latest_lookups=1
FAILED_LISTENER_ACKNOWLEDGED: True watermark=1791547200000
PUBLIC_STATUS_NO_AUTH: 200 fields=[device,temperature,humidity]
DASHBOARD_UNWEIGHTED_AVERAGE: [20.0] expected=28
HEARTBEAT_PERSISTED_BUT_HTTP_BLOCKED: True True
HEARTBEAT_FINISHED_AFTER_LOCK_RELEASE: 200
TREND_OUT_OF_ORDER_RESPONSES: [{label:B,actual:B},{label:B,actual:A}]
NESTED_RETRY_HTTP_CALLS: 9
BOOLEAN_MEASUREMENTS_ACCEPTED: 1.0 0.0
```

全量测试：`640 passed, 1 warning`（2.75 秒）。警告来自 Python 3.14 下 Modbus 测试的 asyncio transport 析构清理；没有测试失败。`ruff check .`、`pip check` 和 `git diff --check` 全部通过。A01、A13 依据构建规则与明确控制流确认，其余 11 项有离线复现证据。

首次审计仅新增报告和本地复现脚本；续修已修改业务代码，见上方修复进展。

## 发布续修

CD 使用 `compose.deploy.yaml` 显式指定本次 SHA 镜像，不覆盖本地 Compose 默认镜像；服务器同步到测试通过的提交 SHA。回滚优先为旧镜像 ID 创建本地固定标签，并验证实际运行镜像。趋势异步回归加入 CI。采集器集成测试按先停止客户端、再关闭模拟服务器的顺序清理，避免关闭服务端后轮询阻塞造成跨测试线程残留。

## 建议修复顺序

1. 在下一次构建发布前修正 A01。
2. 联合设计 A02、A03、A04、A05 的事务、确认与顺序边界，补故障注入和乱序测试；避免分别修补后仍留下跨层缺口。
3. 修复 A06、A07，以确定的缓存失效和稳定业务身份恢复外部服务。
4. 处理 A08、A09 的运行时占用与整体耗时预算，以及 A10 的公开诊断数据边界。
5. 修复 A11、A12、A13，再推进读取过滤、保留策略与结构拆分。
