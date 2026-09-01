# 温湿度监控领域模型冻结稿

状态：第一版业务规则冻结（rule_version=1.0，decision_date=2026-08-28），已接入受保护的飞书写入适配器；默认仍关闭写入。

## 目标边界

领域层只负责确定性业务规则：标准、作业上下文、单次采样结果和报警状态转换。
它不依赖 Flask、SQLite、飞书、Modbus、HA，也不执行网络请求、延迟等待或通知。

应用层负责读取当前状态、调用领域层、持久化状态，并执行领域层返回的动作指令。
集成层负责 HA、Modbus、飞书等外部系统适配。

## 核心模型

### EnvironmentStandard

描述某监测点（可选）、区域、作业类型和时间范围内的环境合格条件。温度和湿度边界均为闭区间；
某一维度的上下限都为空时，表示该标准不约束该维度。标准必须带版本和来源。
另外使用 `enabled` 控制是否生效，使用 `priority` 解决同类标准的优先级。

同一区域可以有多个监测点且分别适用不同标准，因此 `device_id` 为空表示区域级默认标准，
有值时表示监测点专用标准。解析时监测点精确匹配优先于区域级标准。

时间有效区间采用左闭右开：`effective_from <= timestamp < effective_to`；
`effective_to` 可以与 `effective_from` 相等，表示空区间。这样相邻版本可以无缝衔接。

### OperationState

描述设备当前所处的业务上下文：`IDLE`、`OPERATING` 或 `NOT_APPLICABLE`，以及作业类型、
可选工单号和起止时间。第一版不支持 `PAUSED`。作业登记新旧顺序按飞书记录创建时间判断；
旧记录只能审计，不能覆盖已接受的当前状态。它只参与标准解析，不判断温湿度是否超限。

### MonitorResult

描述一个采样时刻相对于一个标准的结果：适用性、数据质量、温度状态、湿度状态、总体状态、
标准版本和原因码。
`evaluate_monitor_state()` 是纯函数：相同输入必须得到相同输出。

其中 `applicability` 为 `APPLICABLE`、`NOT_APPLICABLE`、`NO_STANDARD`；`data_quality` 为
`GOOD`、`OFFLINE`、`MISSING`、`ERROR`。`仅监测` 只能表示 `NOT_APPLICABLE`，不能当作
`NORMAL`；`待工艺标准` 表示 `NO_STANDARD`；离线、数据缺失、数据异常必须可以区分。
等于上下限时通过。总体状态只在适用且数据质量良好时产生 `NORMAL`/`VIOLATION`，否则为
`UNKNOWN`。

### AlarmState

描述报警生命周期，不等同于环境异常事件。当前冻结状态为：

```text
NORMAL -> PENDING -> ALARM -> RECOVERY -> NORMAL
```

第一版冻结默认恢复确认窗口为 1 分钟；`recovery_after=0` 仍可由测试或特殊策略显式配置为
`ALARM -> NORMAL` 直接恢复，需要连续正常确认时使用 `RECOVERY`。

## 状态转换

| 当前状态 | 监测结果 | 条件 | 下一状态 | 领域动作 |
| --- | --- | --- | --- | --- |
| NORMAL | NORMAL | - | NORMAL | 无 |
| NORMAL | VIOLATION | 首次出现 | PENDING | 创建验证任务 |
| PENDING | VIOLATION | 未满验证窗口 | PENDING | 无 |
| PENDING | VIOLATION | 已满验证窗口 | ALARM | 完成验证任务、创建事件 |
| PENDING | NORMAL | 验证窗口内恢复 | NORMAL | 取消验证任务 |
| PENDING | UNKNOWN | 无法确认 | PENDING | 无 |
| ALARM | VIOLATION | - | ALARM | 更新事件 |
| ALARM | NORMAL | 无恢复窗口 | NORMAL | 关闭事件 |
| ALARM | NORMAL | 有恢复窗口 | RECOVERY | 开始恢复确认 |
| RECOVERY | NORMAL | 未满恢复窗口 | RECOVERY | 无 |
| RECOVERY | NORMAL | 已满恢复窗口 | NORMAL | 关闭事件 |
| RECOVERY | VIOLATION | 再次超限 | ALARM | 更新事件 |
| 任意活动状态 | UNKNOWN | 无法确认 | 保持原状态 | 无 |

状态机只返回 `StateTransition` 和声明式 `AlarmAction`，由应用层执行实际任务、事件写入和通知。

## StandardResolver

标准解析接口固定为：

```python
resolve(
    *,
    area_id: str,
    operation_type: str | None,
    timestamp: datetime,
    device_id: str | None = None,
) -> EnvironmentStandard
```

解析顺序固定为：启用且时间有效、区域匹配、设备精确匹配优先、作业类型精确匹配优先、
`priority` 较高优先。
如果最高优先级仍有多个候选，必须抛出配置冲突；没有候选则抛出未找到异常。
`StaticStandardResolver` 用于测试和本地开发，`SQLiteStandardResolver` 从本地
`standard_versions` 缓存解析；飞书只作为后续同步来源，不参与实时规则判断。

## 调用约定

```python
standard = standard_resolver.resolve(
    area_id=device.area,
    operation_type=operation_state.operation_type,
    timestamp=sample.sample_time,
    device_id=device.device_id,
)
result = evaluate_monitor_state(
    device=device,
    sample=sample,
    standard=standard,
)
transition = alarm_state_machine.apply(
    result=result,
    current_state=current_alarm_state,
    now=now,
)
```

## 持久化任务和动作执行

状态机返回的验证任务通过 `automation_tasks` 持久化，至少包含任务类型、实体、执行时间、
状态、payload、`dedupe_key`、重试次数和错误信息。相同 `dedupe_key` 只允许产生一条任务，
程序重启后由 scheduler 重新 claim `PENDING` 任务。

所有领域动作统一交给 `ActionExecutor`：

```text
disabled -> SKIPPED
shadow   -> PLANNED，只写 automation_runs，不执行外部副作用
active   -> 调用注入的 handler，记录 SUCCEEDED 或 FAILED
```

领域层只返回动作指令；SQLite、飞书和通知 handler 均由应用层注入。`automation_runs` 可记录
Python 结果、报警转换、飞书观察值、匹配结果和差异类型，为 Shadow 对比提供数据基础。

任务消费使用 lease：任务被 claim 时写入 `claimed_at`、`lease_until` 和 `worker_id`；lease
过期的 `RUNNING` 任务可以被其他 worker 重新 claim。旧 worker 在 lease 过期后不能再完成或
失败回写任务。

Scheduler 只处理 `task_type`、payload 和 handler，不了解温度、报警或飞书：

```text
claim_due_tasks -> dispatch(task_type, task) -> mark_succeeded / mark_failed
```

## 环境标准表设计

飞书侧已新建独立的 `环境标准表`（`table_id=tbl4S6Q0VOYjK92t`），不能继续把
`设备温湿度记录` 主表的当前上下限当作完整标准版本库。目标字段如下：

| 字段 | 类型 | 规则 |
|---|---|---|
| `standard_id` | 文本 | 必填；与 `revision` 组成唯一键 |
| `revision` | 文本 | 必填；与 `standard_id` 组成唯一键 |
| `area` | 文本/单选 | 必填 |
| `operation_type` | 文本/单选 | 可空；空表示区域默认标准 |
| `temperature_min/max` | 数字 | 必须成对出现或成对为空，且 min ≤ max |
| `humidity_min/max` | 数字 | 必须成对出现或成对为空，且 min ≤ max |
| `effective_from` | 日期时间 | 必填 |
| `effective_to` | 日期时间 | 可空；有值时 effective_from ≤ effective_to |
| `priority` | 整数 | 同层级候选排序 |
| `enabled` | 布尔 | false 不参与解析 |
| `source_document` | 文本 | 必填 |
| `clause` | 文本 | 可空 |
| `备注` | 文本 | 可选管理备注，不参与 Resolver |
| `最后确认人` | 人员 | 可选管理字段，不参与 Resolver |

解析时精确 `operation_type` 优先于默认标准；同一层级仍有多个同优先级候选时必须报配置
冲突，不能随机选择。飞书录入规范要求 `standard_id + revision` 唯一，可靠约束由同步校验和
SQLite `PRIMARY KEY (standard_id, revision)` 保证。

示例数据：

```json
[
  {
    "standard_id": "ENV-PE-001",
    "revision": "Rev.B",
    "area": "PE仓库",
    "operation_type": null,
    "temperature_min": 10,
    "temperature_max": 45,
    "humidity_min": 30,
    "humidity_max": 70,
    "effective_from": "2026-01-01T00:00:00+08:00",
    "effective_to": null,
    "priority": 10,
    "enabled": true,
    "source_document": "NE-QMS-QP034",
    "clause": "5.2.3"
  },
  {
    "standard_id": "ENV-PE-OP-001",
    "revision": "Rev.A",
    "area": "PE仓库",
    "operation_type": "灌封",
    "temperature_min": 20,
    "temperature_max": 26,
    "humidity_min": 40,
    "humidity_max": 60,
    "effective_from": "2026-01-01T00:00:00+08:00",
    "effective_to": null,
    "priority": 1,
    "enabled": true,
    "source_document": "工艺文件-灌封",
    "clause": "4.1"
  }
]
```

`FeishuStandardAdapter` 只负责按显式字段映射读取和规范化。真实表的 table_id 和字段名已
冻结在 `integrations/feishu_standard_config.py`；`FeishuBitableRecordSource` 只读调用现有
Feishu HTTP 服务，领域层不感知飞书字段名。

## 标准同步

标准同步的输入是标准化后的完整快照，不是领域层直接读取飞书记录。`StandardSyncService`
先验证全部快照，再在一个事务内激活：

1. 检查字段完整性、版本重复、时间范围和 `min <= max`。
2. 检查启用标准在相同 `area + operation_type + priority` 下是否存在有效时间重叠。
3. 校验通过后，原子更新 SQLite `standard_versions`。
4. 校验失败、源不可用或激活失败时写入 `standard_sync_runs`，保留上一版有效缓存。

当前提供 `StaticStandardResolver` 和 `SQLiteStandardResolver`；`FeishuStandardAdapter` 作为
只读 source adapter 读取已建立的独立标准表，不让飞书字段名进入 domain 或比较器。

## 作业观察和 active event 规则

正式作业入口是 `环境受控作业登记`。飞书动作规范化为 `StartOperation`、`SwitchOperation`、
`EndOperation`；工单号选填。观察按 `source_created_at` 判断新旧，旧记录只进入审计，不覆盖
当前状态。第一版不支持 `PAUSED`；`N/A` 表示 `NOT_APPLICABLE`，不能等同 `IDLE`。

同一 `device_id` 同时最多允许一个未关闭 ENV 事件。Python/SQLite 使用事务和 active partial
unique index 共同保证，不能只依赖应用层 `if not exists`；任务重试、重复采样、并发 worker
和飞书重复事件都必须保持幂等。本轮不修改飞书已有事件。

## Shadow 比对

比较器只接受两个规范化对象：

```text
ExpectedAutomationState
ObservedAutomationState
```

`FeishuObservationAdapter` 负责把飞书原始字段转换成 `alarm_state`、`operation_state`、
`event_exists` 和可选事件数量/标准信息。Python expected 已变化、飞书仍为旧状态时，延迟
≤60 秒统一记录 `FEISHU_DELAY`；超过 60 秒才按 `STANDARD_MISMATCH`、
`OPERATION_STATE_MISMATCH`、`ALARM_STATE_MISMATCH`、`EVENT_MISSING`、
`EVENT_DUPLICATED` 或 `UNKNOWN` 分类。`EVENT_MISSING`/`EVENT_DUPLICATED` 优先于普通
报警状态差异。

## 第一版冻结决策

| 决策 | 内容 |
|---|---|
| `rule_version` | `1.0` |
| `decision_date` | `2026-08-28` |
| 标准来源 | 新建独立环境标准数据表 |
| 连续超限确认 | 5 分钟 |
| 恢复确认 | 1 分钟 |
| N/A | `NOT_APPLICABLE`，不等同 `IDLE` |
| PAUSED | 第一版不支持 |
| 作业入口 | `环境受控作业登记` |
| 工单号 | 选填 |
| 作业新旧 | 按记录创建时间；旧记录只审计、不覆盖 |
| active ENV 事件 | 同设备最多 1 个未关闭事件 |
| Shadow 延迟 | ≤60 秒为 `FEISHU_DELAY`，>60 秒才是实际 mismatch |

## 后续集成原则

1. 先用 `AUTOMATION_MODE=shadow` 持久化计算结果和转换结果，不执行飞书写入；只有显式开启 Active 写入开关后才执行异常事件写入。
2. 延迟动作必须落库，不能用 `sleep()` 代替；任务至少需要类型、实体、执行时间、状态和幂等键。
3. 每个规则组切换时只允许一个写入方创建事件或更新报警状态。
4. 飞书字段和公式可以作为展示或输入适配，但切换后不能继续与 Python 同时做业务判断。
