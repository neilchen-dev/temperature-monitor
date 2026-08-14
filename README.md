# Temperature Monitor

企业内部温湿度监控服务。接收 Home Assistant 上报的设备状态和温湿度，完成校验与华氏/摄氏转换后写入飞书多维表格，同时保留 CSV 历史记录和轮转日志。

本工程由已稳定运行的单文件版本进行结构化拆分，保留以下行为：

- `POST /temperature` 请求及响应兼容旧版；
- 未提供 `status` 时按“在线”处理；
- 离线时只更新飞书“在线状态”，不覆盖最后温湿度和更新时间；
- 飞书 Token 缓存、提前刷新和失效自动重试；
- HTTP 429、5xx、连接超时的指数退避重试；
- CSV 月度历史、INFO/ERROR 控制台及文件轮转日志；
- Waitress 生产服务器。

## 本地运行

建议使用 Python 3.12：

```powershell
cd temperature-monitor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:APP_ID="你的飞书应用ID"
$env:APP_SECRET="你的飞书应用密钥"
$env:APP_TOKEN="你的多维表格App Token"
$env:TABLE_ID="你的数据表ID"
python app.py
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:5000/health
```

## Docker 运行

构建镜像：

```bash
docker build -t temperature-monitor:latest .
```

启动并挂载持久化目录：

```bash
docker run -d \
  --name temperature-monitor \
  --restart unless-stopped \
  -p 5000:5000 \
  -e APP_ID="your_app_id" \
  -e APP_SECRET="your_app_secret" \
  -e APP_TOKEN="your_app_token" \
  -e TABLE_ID="your_table_id" \
  -v temperature-monitor-data:/app/data \
  -v temperature-monitor-logs:/app/logs \
  temperature-monitor:latest
```

生产环境建议在云效中通过安全变量注入飞书凭据，构建完成后推送至阿里云 ACR。不要把凭据写入 Dockerfile、代码仓库或镜像构建参数。

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `APP_ID` | 是 | 空 | 飞书应用 ID |
| `APP_SECRET` | 是 | 空 | 飞书应用密钥 |
| `APP_TOKEN` | 是 | 空 | 飞书多维表格 App Token |
| `TABLE_ID` | 是 | 空 | 飞书多维表格数据表 ID |
| `HOST` | 否 | `0.0.0.0` | HTTP 监听地址 |
| `PORT` | 否 | `5000` | HTTP 监听端口 |
| `SOURCE_TEMPERATURE_UNIT` | 否 | `F` | HA 上报单位，只能为 `F` 或 `C` |
| `USE_SYSTEM_PROXY` | 否 | `false` | 飞书请求是否读取系统代理 |
| `REQUEST_TIMEOUT_SECONDS` | 否 | `10` | 单次 HTTP 请求超时秒数 |
| `REQUEST_RETRY_TIMES` | 否 | `3` | HTTP 请求尝试次数 |
| `REQUEST_RETRY_BACKOFF_SECONDS` | 否 | `0.8` | 指数退避基数 |
| `TOKEN_REFRESH_MARGIN_SECONDS` | 否 | `300` | Token 提前刷新秒数 |
| `WAITRESS_THREADS` | 否 | `4` | Waitress 工作线程数 |
| `DATA_DIR` | 否 | `/app/data` | 容器内 CSV 目录 |
| `LOG_DIR` | 否 | `/app/logs` | 容器内日志目录 |

## Home Assistant 调用示例

```yaml
rest_command:
  send_temperature:
    url: "http://<服务地址>:5000/temperature"
    method: POST
    content_type: "application/json"
    payload: >
      {
        "device": "{{ device }}",
        "temperature": "{{ temperature }}",
        "humidity": "{{ humidity }}",
        "status": "{{ status }}"
      }
```

请求示例：

```json
{
  "device": "TH-01",
  "temperature": 86.0,
  "humidity": 60,
  "status": "在线"
}
```

默认 `SOURCE_TEMPERATURE_UNIT=F`，因此示例中的 `86.0` 会转换为 `30.0°C`。若 HA 已直接发送摄氏温度，请将该环境变量设为 `C`。

## CI/CD 目标链路

```text
Codeup → 云效流水线 → Docker Build → 阿里云 ACR → Home Assistant 拉取运行
```

云效流水线只需要在项目目录执行 Docker 构建并推送成品镜像。运行阶段在 Home Assistant Add-on 配置或容器环境中注入四个飞书环境变量，并持久化挂载 `/app/data` 与 `/app/logs`。
