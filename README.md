# PowerRail

这是一个面向电源与能耗管理的电源与能耗管理平台。长期目标是提供电池建模与荷电状态估算、充放电策略、均衡控制、DC-DC 与 LDO 效率建模、功耗预算与分配、能量采集、告警与分级降载，把电源与能耗管理沉淀为可复用服务。

仓库采用 Python，当前冻结基线只提供进程健康检查。后续能力必须通过独立题目逐步实现；每个题目都应定义可观察的公共行为、兼容边界和失败语义，不得依赖未公开内部 API。

## 启动

```bash
PYTHONPATH=src python3 -m powerrail.server --host 127.0.0.1 --port 8080
```

服务默认监听 `127.0.0.1:8080`，可通过 `POWERRAIL_ADDR` 修改。`GET /healthz` 返回 JSON 健康状态。

## SOC 估算

`POST /v1/battery/soc/estimate` 基于库仑计数估算电池荷电状态，可选 OCV 曲线在长时间静置后修正。

请求字段：

- `capacity_ah`（必填，正数）：电池容量（安时）。
- `initial_soc`（必填，0 至 1）：首项采样的 SOC。
- `samples`（必填，非空数组）：按 `timestamp_s` 严格递增排列的采样，每项含 `timestamp_s`、`current_a`、`voltage_v`，正电流表示放电。
- `ocv_curve`（可选，至少两个点）：每项含严格递增的 `voltage_v` 与位于 0 至 1、单调不减的 `soc`。
- `rest_current_a`、`rest_duration_s`、`ocv_weight`（可选，默认 `0.05`、`300`、`0.2`）：静置判定阈值、累计时长与修正权重。

响应包含与采样等长同序的 `estimates`（每项返回 `timestamp_s`、`soc`、`source`）以及等于末项 SOC 的 `final_soc`；OCV 修正项 `source` 为 `ocv_corrected`，其余为 `coulomb`。非法或非对象 JSON 返回 `400 invalid_json`；字段校验失败返回 422，错误码包括 `invalid_capacity`、`invalid_initial_soc`、`invalid_samples`、`invalid_timestamp`、`invalid_measurement`、`invalid_ocv_curve`、`invalid_options`。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含电池建模、充放电策略与功耗预算的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
