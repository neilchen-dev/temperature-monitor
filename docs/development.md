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

## 飞书只读接入约束

真实飞书读取需要通过显式配置的 App Token、table_id 和字段映射；凭据只放在本地 `.env` 或部署密钥中，不提交仓库。标准 Adapter 只读取并规范化记录，StandardSyncService 负责校验与本地激活；本阶段不向既有飞书业务表写入状态，也不修改或关闭现有工作流。
