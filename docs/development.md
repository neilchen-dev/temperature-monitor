# 开发与测试环境

本项目的开发验证应在同一个虚拟环境中完成，避免把某台机器上的临时依赖误认为项目自身的测试保障。

## 创建环境

PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt
```

## 必跑检查

```powershell
python -m pytest
ruff check .
python -c "import domain, application, integrations, repositories, scheduler, runtime; from domain.monitor_engine import evaluate_monitor_state; from application.shadow import compare_states; from integrations.feishu_standard import FeishuStandardAdapter; from integrations.feishu_operation import FeishuOperationAdapter; from repositories.environment_events import SQLiteEnvironmentEventRepository; from runtime.bootstrap import build_runtime; print('imports ok')"
```

`requirements-dev.txt` 包含运行时依赖、Flask、requests、pymodbus，以及 pytest 和 ruff。完成安装后，以上命令应在同一个 `.venv` 中执行。

## 相关领域与运行时回归测试

以下 69 个相关回归测试来自项目当前主机上使用 bundled Python 执行的定向集合：

```text
python -m unittest tests.test_action_executor tests.test_alarm_state_machine \
  tests.test_automation_tasks tests.test_domain_monitor_engine \
  tests.test_environment_events tests.test_feishu_adapters \
  tests.test_monitor_application tests.test_runtime tests.test_scheduler \
  tests.test_shadow_comparison tests.test_standard_resolver \
  tests.test_standard_sync
```

它不是 pytest 全量结果。完整套件仍需在可创建和清理临时 SQLite 目录的环境中执行；本文件和 `requirements-dev.txt` 将正式入口固定为上面的同环境 `python -m pytest`、`ruff check .` 和 import smoke test。

## 飞书读写接入约束

真实飞书读写需要通过显式配置的 App Token、table_id 和字段映射；凭据只放在本地 `.env` 或部署密钥中，不提交仓库。标准 Adapter 仍只读取并规范化记录，写入由 `integrations/feishu_writers.py` 集中负责。

新增作业/异常/点检写入默认关闭。只有 `AUTOMATION_MODE=active` 且 `FEISHU_WRITE_ENABLED=true` 时，Active 动作才会写环境异常事件；作业登记和仓库点检通过同样的受保护 API 写入。Shadow/disabled 不触发这些新增写入；既有 `/temperature` 主表更新链路不受此开关改变。点检记录的 `点检时间`、`点检人` 是飞书系统字段，不由 Python 覆盖；点检表的 `异常/报警编号` 是数字字段，不写入 `ENV-...` 文本编号。
