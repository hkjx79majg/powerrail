# PowerRail

这是一个面向电源与能耗管理的电源与能耗管理平台。长期目标是提供电池建模与荷电状态估算、充放电策略、均衡控制、DC-DC 与 LDO 效率建模、功耗预算与分配、能量采集、告警与分级降载，把电源与能耗管理沉淀为可复用服务。

仓库采用 Python，当前冻结基线只提供进程健康检查。后续能力必须通过独立题目逐步实现；每个题目都应定义可观察的公共行为、兼容边界和失败语义，不得依赖未公开内部 API。

## 启动

```bash
PYTHONPATH=src python3 -m powerrail.server --host 127.0.0.1 --port 8080
```

服务默认监听 `127.0.0.1:8080`，可通过 `POWERRAIL_ADDR` 修改。`GET /healthz` 返回 JSON 健康状态；未知路径返回 `404 {"error":{"code":"not_found",...}}`。

## 电池 SoC 估算

`POST /v1/battery/soc/estimate` 基于库仑计数估算荷电状态（正电流表示放电），并可在长时间静置后用 OCV 曲线修正。
请求字段：

- `capacity_ah`（必填）：正数电池容量（安时）。
- `initial_soc`（必填）：`[0, 1]` 内的初始 SoC，首项采样直接采用该值。
- `samples`（必填）：非空数组，每项含严格递增的有限 `timestamp_s` 以及有限数值 `current_a`、`voltage_v`。
- `ocv_curve`（可选）：至少两个点，`voltage_v` 严格递增，`soc` 单调不减且位于 `[0, 1]`。
- `rest_current_a`、`rest_duration_s`、`ocv_weight`（可选）：默认 `0.05`、`300`、`0.2`。

算法：后续每项以相邻电流均值计算 `soc_next = soc_prev - current_avg * delta_time / (capacity_ah * 3600)`，并限制在 `[0, 1]`。提供曲线时，仅当区间两端电流绝对值均不超过 `rest_current_a` 才累计静置时间，否则清零；累计达到 `rest_duration_s` 后，按当前电压在曲线上线性插值（越界取最近端点）得到 `ocv_soc`，并以 `(1-ocv_weight)*soc_next + ocv_weight*ocv_soc` 修正，该响应项 `source` 为 `ocv_corrected`，其余为 `coulomb`。

响应 `estimates` 与采样等长同序，每项含 `timestamp_s`、`soc`、`source`；`final_soc` 等于末项 `soc`。错误响应统一为 `{"error":{"code":...}}`：非法或非对象 JSON 为 `400 invalid_json`；`capacity_ah`、`initial_soc`、`samples`、`timestamp_s`、`current_a`/`voltage_v`、`ocv_curve`、可选项不合法分别返回 `422` 与 `invalid_capacity`、`invalid_initial_soc`、`invalid_samples`、`invalid_timestamp`、`invalid_measurement`、`invalid_ocv_curve`、`invalid_options`。

## 电池健康度与循环寿命估算

`POST /v1/battery/health/estimate`（也可直接调用 `Service.estimate_health`）依据容量检测记录估算电池健康度（SoH）与剩余等效循环次数。

请求字段：

- `nominal_capacity_ah`（必填）：正有限数额定容量（安时）。
- `measurements`（必填）：至少两项的数组，每项含：
  - `timestamp_s`：有限且严格递增的时间戳；
  - `measured_capacity_ah`：正有限数容量检测值；
  - `cumulative_discharge_ah`：非负、有限且单调不减的累计放电量。
- `end_of_life_soh`（可选）：寿命终止 SoH 阈值，取 `[0, 1]` 闭区间内有限数，默认 `0.8`。

每项 `soh` 为容量检测值除以额定容量后限制在 `[0, 1]`，`equivalent_cycles` 为累计放电量除以额定容量；响应 `estimates` 与测量等长同序，每项含 `timestamp_s`、`soh`、`equivalent_cycles`；`latest_soh`、`consumed_cycles` 取末项。`latest_soh` 不高于阈值时，`remaining_cycles` 为 `0` 且 `projection_status` 为 `end_of_life`；否则仅当首末等效循环跨度为正且 SoH 严格下降时，按首末下降率（SoH 降幅 / 等效循环跨度）将 SoH 线性外推到阈值，返回 `remaining_cycles` 且 `projection_status` 为 `projected`；其余情况 `remaining_cycles` 为 `null`、`projection_status` 为 `insufficient_trend`。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；`422` 字段错误包括 `invalid_nominal_capacity`、`invalid_measurements`（数组类型、数量或元素类型非法）、`invalid_timestamp`、`invalid_capacity_measurement`、`invalid_throughput`（累计放电量缺失、非有限、为负或下降）、`invalid_end_of_life_soh`。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含电池建模、充放电策略与功耗预算的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
