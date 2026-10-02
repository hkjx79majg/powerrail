# PowerRail

这是一个面向电源与能耗管理的电源与能耗管理平台。长期目标是提供电池建模与荷电状态估算、充放电策略、均衡控制、DC-DC 与 LDO 效率建模、功耗预算与分配、能量采集、告警与分级降载，把电源与能耗管理沉淀为可复用服务。

仓库采用 Python，当前冻结基线只提供进程健康检查。后续能力必须通过独立题目逐步实现；每个题目都应定义可观察的公共行为、兼容边界和失败语义，不得依赖未公开内部 API。

## 启动

```bash
PYTHONPATH=src python3 -m powerrail.server --host 127.0.0.1 --port 8080
```

服务默认监听 `127.0.0.1:8080`，可通过 `POWERRAIL_ADDR` 修改。`GET /healthz` 返回 JSON 健康状态。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含电池建模、充放电策略与功耗预算的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
