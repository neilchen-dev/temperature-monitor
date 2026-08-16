# Temperature Monitor Home Assistant Add-on

安装后，在 Add-on 的“配置”页面填写飞书应用凭据。程序默认按飞书表中的“设备编号”字段自动匹配设备名并识别 `record_id`。

若字段名称不同，请修改 `device_id_field`。也可以使用可选的 JSON `device_record_map` 手动覆盖个别设备：

```json
{"DEV-01":"recxxxxxxxxxxxx"}
```

启动 Add-on 后，Home Assistant 自动化可通过 `http://<home-assistant-host>:5000/temperature` 调用服务。健康检查地址为 `/health`。
