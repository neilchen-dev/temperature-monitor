# Temperature Monitor Home Assistant Add-on

安装后，在 Add-on 的“配置”页面填写飞书应用凭据，并使用 JSON 格式设置设备名到飞书多维表格记录 ID 的映射：

```json
{"DEV-01":"recxxxxxxxxxxxx"}
```

启动 Add-on 后，Home Assistant 自动化可通过 `http://<home-assistant-host>:5000/temperature` 调用服务。健康检查地址为 `/health`。
