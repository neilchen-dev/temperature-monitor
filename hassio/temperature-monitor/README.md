# Temperature Monitor Home Assistant Add-on

安装后，在 Add-on 的“配置”页面填写飞书应用凭据。程序默认按飞书表中的“设备编号”字段自动匹配设备名并识别 `record_id`。

若字段名称不同，请修改 `device_id_field`。如果 HA 上报设备名和飞书设备编号不同，使用可选的 JSON `device_name_map` 映射，例如 `{"sensor.warehouse_temp":"DEV-01"}`。也可以使用可选的 JSON `device_record_map` 手动覆盖个别设备：

```json
{"DEV-01":"recxxxxxxxxxxxx"}
```

启动 Add-on 后，Home Assistant 自动化应通过 Add-on 的内部主机名调用服务：`http://<addon-hostname>:5000/temperature`。请将 `<addon-hostname>` 替换为当前安装实例的名称；从自定义 GitHub 仓库安装时，该名称由 Home Assistant 根据仓库生成，不能假定为固定的 `local-temperature-monitor`。健康检查地址为 `/health`。
