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

## 功耗预算分配

`POST /v1/power/budget/allocate`（也可直接调用 `Service.allocate_power_budget`）在可用功率预算内按优先级为负载分配功率。

请求字段：

- `available_power_w`（必填）：非负有限数可用功率（瓦）。
- `reserve_power_w`（可选）：非负有限数保留功率，默认 `0`，不得超过可用功率。
- `loads`（必填）：非空数组，每项为对象，含：
  - `id`：非空字符串，数组内唯一；
  - `demand_power_w`：非负有限数需求功率；
  - `min_power_w`：非负有限数最低功率，不得超过需求功率；
  - `priority`：`[0, 100]` 内整数，数值越大优先级越高。

可分配功率为 `available_power_w - reserve_power_w`。分配分两阶段：先按优先级从高到低满足各负载 `min_power_w`，同一优先级最低功率之和超过余量时按各自最低功率比例分配，更低优先级归零；再将剩余功率按同样优先级顺序补足需求，同级无法全额补足时按各自未满足需求（`demand_power_w` 减去已分配）的比例分配。任何分配不为负、不超过需求，总额不突破可分配功率；调用不修改请求对象，负载换序后按 `id` 对应的结果不变。

响应 `allocations` 与输入 `loads` 等长同序，每项含 `id`、`allocated_power_w`、`shortfall_power_w`（需求减分配，不为负）与 `state`：达到需求（含零需求）为 `powered`，零分配且需求为正为 `shed`，其余为 `limited`。`allocated_power_w`、`unallocated_power_w` 汇总已分配与剩余可分配功率；全部需求满足时 `status` 为 `satisfied`，否则为 `constrained`。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；预算字段缺失、为布尔值、非有限数、为负或保留量超 available 为 `422 invalid_budget`；`loads` 非非空数组或元素非对象为 `422 invalid_loads`；`id` 缺失、为空或重复为 `422 invalid_load_id`；`demand_power_w`/`min_power_w` 缺失或违反数值范围为 `422 invalid_load_power`；`priority` 缺失、为布尔值、非整数或越界为 `422 invalid_priority`。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含电池建模、充放电策略与功耗预算的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
