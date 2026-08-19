# Changelog

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
