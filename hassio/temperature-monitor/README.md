# Temperature Monitor Home Assistant Add-on

安装后，Home Assistant 会从公开的 GitHub Container Registry 拉取与 `config.yaml` 版本一致的 Add-on 镜像，不依赖项目内置的私有镜像地址。在 Add-on 的“配置”页面填写飞书应用凭据，并为 `history_api_key` 配置至少 32 字节的随机共享密钥。程序默认按飞书表中的“设备编号”字段自动匹配设备名并识别 `record_id`。

若字段名称不同，请修改 `device_id_field`。如果 HA 上报设备名和飞书设备编号不同，使用可选的 JSON `device_name_map` 映射，例如 `{"sensor.warehouse_temp":"DEV-01"}`。也可以使用可选的 JSON `device_record_map` 手动覆盖个别设备：

```json
{"DEV-01":"recxxxxxxxxxxxx"}
```

历史采样使用配置中的 `history_table_map`，每十分钟从实时总表读取设备状态并分别写入历史表。请只填写自己飞书空间中的表 ID，不要把真实 ID 提交到 Git。第一阶段务必保持 `history_cleanup_enabled: false`；此时会完全跳过过期记录筛选与删除接口。

启动 Add-on 后，Home Assistant 自动化应通过 Add-on 的内部主机名调用服务：实时更新为 `http://<addon-hostname>:5000/temperature`，历史采样为 `http://<addon-hostname>:5000/history/sample`。请将 `<addon-hostname>` 替换为当前安装实例的名称；从自定义 GitHub 仓库安装时，该名称由 Home Assistant 根据仓库生成，不能假定为固定的 `local-temperature-monitor`。健康检查地址为 `/health`。
