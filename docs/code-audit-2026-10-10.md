# 继续代码审计（2026-10-10）

本轮针对 HTTP 输入与写回权限、巡检幂等和运行时健康探针继续审计。未修改业务代码，未访问真实飞书或生产数据库。这是定向审计，不代表全仓库不存在其他问题。

## 续修进展

B01–B03 已在后续修复落实，下面保留原始发现作为审计证据。

- B01：巡检 HTTP 入口从飞书设备表核对唯一设备及其区域；父记录必须存在且属于相同仓库区域。查询失败、设备缺失或多义均拒绝写入。支持飞书富文本字段；巡检是区域级记录，父记录归属按区域校验。
- B02：实际状态时间（优先 state_recorded_at，否则 inspected_at）统一用于业务键查询、字段写入与默认 client token。HTTP writer 也启用只读 source，以查询已存在的业务快照。
- B03：缓存最大有效期为 30 秒，过期后返回 not-ready 并暴露缓存年龄；后台刷新获取 execution lock 最多等待 5 秒，超时返回未就绪，避免刷新线程无限等锁。此次未新增完整调度阶段进度探针。
- 新增 `tests/test_audit_followup_fixes.py`，覆盖跨区域/父记录拒绝、身份缺失/歧义、读取失败、富文本、时间覆盖幂等、缓存过期与锁超时。业务修改只在本地，未部署或访问真实飞书。
- 续修验证：683 项 Python 测试通过（1 个既有类型的 Python 3.14 asyncio transport 析构警告）；Ruff、JavaScript 异步回归和 `git diff --check` 通过。

## B01 / P1：巡检范围校验与实际写入目标脱节

位置：`routes/api.py:427`、`routes/api.py:439`、`integrations/feishu_writers.py:1832`。

`POST /api/inspections` 只验证请求中的 `device_id` 属于 ACTIVE_DEVICE_IDS，却没有把该设备传给巡检 writer，也没有查询设备所属区域。实际写入的 `area` 和 `parent_record_id` 完全来自请求。拥有有效 API 密钥的调用者可以提交白名单设备 TH-10，同时指定范围外区域与父记录，绕过 Active Canary 的业务写入范围。它不是无鉴权攻击，但会破坏灰度隔离。

离线复现：白名单仅 TH-10，提交 `device_id=TH-10, area=OUTSIDE_SCOPE, parent_record_id=rec-outside`，返回 201；writer 收到范围外区域与父记录。现有测试只断言白名单设备能调用 writer，没有断言目标区域或父记录身份。

建议：从权威设备记录解析允许区域，核对请求区域；若允许父记录链接，查询并验证该记录的区域/设备归属。无法确认时拒绝写入。对区域级点检明确制定灰度范围规则。

## B02 / P2：巡检时间覆盖后，幂等身份与持久化业务键不一致

位置：`integrations/feishu_writers.py:1666`、`:1702`、`:1834`。

`create_snapshot` 用 area + inspected_at 查询已有记录并构造默认 client_token；`snapshot_fields` 随后允许 state_recorded_at 覆盖实际写入的“状态记录时间”。相同 inspected_at、不同 state_recorded_at 的两个请求写入内容不同，却拥有相同默认幂等键，可能复用前一次创建结果或触发冲突，导致后一次快照无法正确创建。反向情况（不同 inspected_at、同一 state_recorded_at）则产生不同 token，即使配置 source，查询仍按错误时间寻找已有记录，存在重复业务记录的可能。

离线复现：固定 inspected_at=10:00，分别传入 state_recorded_at=10:00 和 11:00；两个 create 的 client_token 完全相同，而写入字段不同。API 同时暴露了这两个时间参数。

建议：先确定唯一有效状态时间，并让写入字段、已有记录查询、默认幂等键共同使用该时间；或拒绝两个时间不一致的请求。验证两个方向的时间组合。

## B03 / P2：readiness 缓存没有最大有效期（上一轮遗留）

位置：`runtime/bootstrap.py:363`、`:450`。

TTL 仅触发后台刷新，并不使旧 ready=True 失效。刷新线程在 execution lock 上无限等待时，只要调度线程仍存活，`runtime_readiness()` 就无限返回历史就绪状态，`/readyz` 仍可能返回 200。启动装配使用同一结果作为 standards_ready_provider，陈旧结果也可能参与业务门控。

离线复现：缓存 ready=True、缓存时间设为 0、刷新中、liveness 为 available/running，仍返回 ready=True。上一轮已经提出缓存年龄和调度进度探针，本轮确认尚未落实。

建议：设置最大缓存年龄，过期返回 not-ready 和明确原因；提供最后完成 tick 与阶段耗时，并对刷新锁等待设置边界。

## 验证

- `.venv/bin/python -m pytest -q`：668 passed，1 个 Python 3.14 asyncio transport 析构警告。没有将该警告单独认定为业务漏洞。
- `node tests/console_async.test.cjs`：通过。
- 离线复现脚本：`.codex-tmp/audit_followup.py`（忽略目录，不属于生产代码）；使用 Flask 测试客户端和 mock，没有外部写入。
- 上一轮 A01–A13 已有修复记录，本轮未把它们作为未修复问题重复计数。
