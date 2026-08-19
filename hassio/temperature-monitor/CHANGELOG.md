# Changelog

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
