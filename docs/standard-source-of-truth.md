# 环境标准唯一事实源

## 结论

生产监控判断只接受飞书环境标准表经过适配、严格校验并成功提交的
validated standard。`bootstrap.py` 只提供设备编号和区域等静态身份信息；它不提供
`control_type`、温湿度上下限或 `enabled` 的业务默认值。

## 解析链

```text
Feishu standard table (read-only)
        |
        v
FeishuStandardAdapter  -- field/type normalization
        |
        v
StandardSyncService    -- complete-snapshot validation
        |                         \
        | valid                    \ invalid/fetch failure
        v                           v
SQLite standard_versions       standard_sync_runs(FAILED)
  (immutable history)             (no pointer change)
        |
        v
validated standard snapshot
        |
        +--> active_snapshot_id
        +--> last_known_good_snapshot_id
        |
        v
SQLiteStandardResolver
  1. current active effective validated standard
  2. last-known-good validated standard
  3. StandardNotFoundError
        |
        v
MonitorEngine -> MonitorResult(UNKNOWN, NO_STANDARD) when unavailable
```

`standard_versions` 保存 `(standard_id, revision)` 的不可变业务内容；其中 `standard_id`
是稳定的逻辑标准 key，revision chain 使用同一个 `standard_id`。相同版本再次同步必须
内容一致，不能用新的上下限、控制类型或启用状态覆盖历史行。新的 revision 可以在同一
完整快照中与旧 revision 共存，并按较晚的 `effective_from` supersede 旧 revision；不应
为同一逻辑标准另造 `ENV-E2E-*` 之类的 `standard_id`。同步只在完整快照
验证成功后移动 active/last-known-good 指针，并且相同快照重复同步是幂等的。
旧版 `standard_versions` 历史行不会在启动迁移时自动提升为快照或指针；首次部署必须
等待一次严格 Feishu sync 成功后才建立可信 active/LKG snapshot。

严格校验包括：设备编号非空、版本非空且可追溯、`control_type` 为支持的 enum、
`temp_min < temp_max`、`humidity_min < humidity_max`、上下限为有限数值，以及
`enabled` 为布尔值。同一 `standard_id` 的 revision 若 selector 不一致且有效期重叠，
或多个 revision 的 `effective_from` 相同，仍然失败；不同 logical standard 在相同
selector/priority 下重叠也仍然失败。任何失败都保留当前有效标准并写入失败同步审计。

## 监控审计

每次 `MonitorResult` 都携带 `standard_id`、`standard_revision` 和
`standard_source`。动作审计和 Shadow 对比审计同时在 `automation_runs` 独立列及 JSON
上下文保存这些值，因此可以从一次判断追溯到飞书来源和具体版本。

## Standards readiness 门禁

运行时状态同时暴露 `active_snapshot_id`、`last_known_good_snapshot_id`、
`validated_standard_count`、`expected_standard_count`、`latest_sync_status`、
`last_sync_attempt_at`、`last_successful_sync_at`、`standard_source` 和
`standards_ready`。只有 active/LKG 都存在、active 为 `VALIDATED`、已验证设备数量
覆盖配置设备、至少有一次成功的严格 Feishu sync 且 active source 为 Feishu 时，
`standards_ready` 才为 true。

Active/Canary 的 `ActionExecutor` 在该门禁为 false 时只记录 `PLANNED`，不调用任何
生产写 handler；采样、本地持久化和 Shadow/诊断仍继续。监控没有可用标准时返回
`UNKNOWN`/`NO_STANDARD`，不会被当作正常或超限，也不会产生错误飞书动作。

`/api/thresholds` 只读当前 active validated Feishu standard，并返回
`authoritative_source=feishu`。历史本地 `device_thresholds` 表仅作为兼容性缓存保留，
PUT 写入口返回冲突，不得改变生产运行标准。

## 同步触发

当前运行时通过 durable `SYNC_STANDARD` 周期任务兜底。`ShadowRuntime.trigger_standard_sync()`
提供立即触发边界；后续接入飞书 webhook/event 时，事件处理器只需要调用该入口，
无需改变解析和激活逻辑。

## 部署门禁

部署 workflow 在拉取新镜像和启动迁移前，使用 SQLite online backup API 创建
`data/backups/temperature_monitor_<UTC>.db`，兼容 WAL，并对备份执行 `PRAGMA quick_check`。
备份、quick check 或容器内备份目录创建任一步失败，都会阻止后续部署。启动后的健康检查
还必须确认 `/api/system/status` 中 `runtime.available=true`；首次部署时允许
`standards_ready=false`，但必须等一次严格 Feishu sync 成功后才允许 Active 写回。
失败同步只保留 `last-known-good`，保留的备份用于人工回滚数据库；标准历史和 snapshot
记录不做覆盖式迁移。

## 故障语义

飞书读取失败、字段非法、快照冲突或本地快照激活失败，都不会清空或替换当前生产
标准。若从未成功获取过有效标准，解析器返回 `StandardNotFoundError`，监控结果为
`NO_STANDARD/UNKNOWN`，不会产生基于代码默认 `control_type` 的业务判断。
