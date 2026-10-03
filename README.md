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

## 电池一阶 Thevenin 模型仿真

`POST /v1/battery/model/simulate`（也可直接调用 `Service.simulate_battery_model`）按一阶 Thevenin 等效电路模型对电流序列做带载电压仿真（正电流放电、负电流充电）。
请求字段：

- `capacity_ah`（必填）：正有限数电池容量（安时）。
- `initial_soc`（必填）：`[0, 1]` 内的初始 SoC。
- `model`（必填）：对象，含正有限数 `r0_ohm`、`r1_ohm`、`c1_f`（欧姆电阻、极化电阻、极化电容）。
- `ocv_curve`（必填）：至少两个点，`soc` 与 `voltage_v` 均严格递增，且 `soc` 位于 `[0, 1]`。
- `samples`（必填）：非空数组，每项含严格递增的有限 `timestamp_s` 与有限 `current_a`。

算法：首个结果采用 `initial_soc`、极化电压为零。后续区间使用前一样本电流 `I` 与时间差 `dt`：`soc = clamp(soc - I*dt/(capacity_ah*3600), 0, 1)`；极化电压更新为 `v*exp(-dt/(r1_ohm*c1_f)) + r1_ohm*I*(1-exp(-dt/(r1_ohm*c1_f)))`。开路电压按当前 SoC 在曲线上线性插值（SoC 为自变量，越界取端点）；端电压为 `ocv - 当前样本电流*r0_ohm - 极化电压`。

响应 `estimates` 与样本等长同序，每项含 `timestamp_s`、`soc`、`ocv_voltage_v`、`polarization_voltage_v`、`terminal_voltage_v`；顶层 `final_soc`、`final_terminal_voltage_v` 取末项值，成功返回 `200` 且不修改请求对象。错误语义：`400 invalid_json`（请求体缺失、解析失败或顶层非对象）；`422` 依次为 `invalid_capacity`、`invalid_initial_soc`、`invalid_battery_model`、`invalid_ocv_curve`、`invalid_samples`、`invalid_timestamp`、`invalid_current`，布尔值不视为数值。

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

`POST /v1/power/budget/allocate`（也可直接调用 `Service.allocate_power_budget`）在可用功率约束下按优先级为负载分配功率。

请求字段：

- `available_power_w`（必填）：非负有限数可用功率（瓦）。
- `reserve_power_w`（可选）：非负有限数保留功率，默认 `0`，不得超过可用功率。
- `loads`（必填）：非空数组，每项含唯一非空字符串 `id`、非负有限数 `demand_power_w` 与 `min_power_w`（最低功率不得超过需求）、`[0, 100]` 内整数 `priority`（数值越大优先级越高）。

可分配功率为可用功率减去保留功率。先按优先级从高到低满足各负载最低功率；同级最低功率之和超过余量时按各自最低功率比例分配，更低优先级归零。此后剩余功率仍按优先级从高到低补足需求；同级无法全额补足时按各自未满足需求的比例分配。响应 `allocations` 与输入等长同序，每项含 `id`、`allocated_power_w`、`shortfall_power_w`、`state`（达到需求或零需求为 `powered`，部分供电为 `limited`，其余零分配为 `shed`）；`allocated_power_w`、`unallocated_power_w` 汇总功率；全部需求满足时 `status` 为 `satisfied`，否则为 `constrained`。任何分配不为负、不超过需求，总额不突破可分配功率；调用不修改请求对象，负载换序后按 `id` 对应的结果不变。

错误响应沿用统一结构：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；预算字段非法或保留量超限为 `422 invalid_budget`；`loads` 数组或元素类型非法为 `422 invalid_loads`；`id` 缺失、为空或重复为 `422 invalid_load_id`；功率字段非法为 `422 invalid_load_power`；`priority` 缺失、为布尔值、非整数或越界为 `422 invalid_priority`。

## 迟滞告警与分级降载

`POST /v1/power/load-shed/decide`（也可直接调用 `Service.decide_load_shedding`）按可用功率样本序列执行带迟滞的分级降载决策，并对余下负载沿用功耗预算分配规则。

请求字段：

- `loads`（必填）：非空数组，每项含唯一非空字符串 `id`、非负有限数 `demand_power_w` 与 `min_power_w`（最低功率不得超过需求）、`[0, 100]` 内整数 `priority`，以及 `shed_level`（`warning`、`critical`、`never` 三选一）。
- `config`（必填）：对象，含非负有限数 `reserve_power_w`、满足 `0 < warning_shortfall_w < critical_shortfall_w` 的有限数阈值，以及正整数 `recovery_samples`。
- `samples`（必填）：非空数组，每项为对象，含严格递增的有限 `timestamp_s` 与非负有限 `available_power_w`；布尔值不作为数值接受。

每项样本的原始缺口为总需求加保留功率减可用功率，下限取零；达到临界、告警阈值或更低时目标级别依次为 `critical`、`warning`、`normal`。级别从 `normal` 开始：目标高于当前级别时立即升级（跨两级仍计一次升级）；目标低于当前级别时，需连续 `recovery_samples` 项低于当前级别才降一级，否则连续计数清零，降级后重新计数。`warning` 级别切除 `shed_level` 为 `warning` 的负载，`critical` 级别再切除 `critical`，`never` 永不切除。余下负载以可用功率减保留功率（下限取零）按预算规则分配；被切除项分配为零且状态为 `shed`。

响应 `decisions` 与样本等长同序，每项含 `timestamp_s`（原样保留）、`raw_shortfall_w`、`target_level`、`level`、`shed_ids`、`status`、`allocated_power_w`、`unallocated_power_w` 与 `allocations`（与负载同序，含 `id`、`allocated_power_w`、`shortfall_power_w`、`state`）；全部负载被策略切除时 `status` 为 `policy_shed`，余下需求全部满足为 `satisfied`，否则为 `constrained`。顶层含 `final_level` 与 `escalation_count`。成功返回 `200`，且不修改请求对象。

错误响应沿用统一结构：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；`loads` 结构非法为 `422 invalid_loads`；`id` 非法或重复为 `422 invalid_load_id`；功率字段非法为 `422 invalid_load_power`；`priority` 非法为 `422 invalid_priority`；`shed_level` 非法为 `422 invalid_shed_level`；`config` 结构或字段非法为 `422 invalid_load_shed_config`；`samples` 结构非法为 `422 invalid_samples`；时间戳非法或未严格递增为 `422 invalid_timestamp`；可用功率非法为 `422 invalid_available_power`。

## DC-DC 效率评估

`POST /v1/power/dcdc/efficiency/estimate`（也可直接调用 `Service.estimate_dcdc_efficiency`）按工况评估 DC-DC 转换器的效率、损耗与累计能量。

请求字段：

- `input_voltage_v`（必填）：正有限数输入电压（伏）。
- `operating_points`（必填）：非空数组，每项为对象，含正有限数 `duration_s`、`output_voltage_v` 以及非负有限数 `output_current_a`。
- `efficiency_curve`（必填）：至少两个点的数组，每项为对象，`output_current_a` 为非负有限数且严格递增，`efficiency` 位于 `(0, 1]`。
- `quiescent_current_a`（可选）：非负有限数静态电流（安），默认 `0`。

按工况输出电流在效率曲线上线性插值，越界取最近端点。输出功率为 `output_voltage_v * output_current_a`；输入功率为输出功率除以插值效率，再加 `input_voltage_v * quiescent_current_a`；损耗为输入功率减输出功率；返回效率为输出功率除以输入功率，输入功率为零时取 `0`。各项能量按功率乘 `duration_s` 除以 `3600` 累加，总效率为总输出能量除以总输入能量，分母为零时取 `0`。

响应 `estimates` 与工况等长同序，每项含 `input_power_w`、`output_power_w`、`loss_power_w`、`efficiency`；顶层含 `input_energy_wh`、`output_energy_wh`、`loss_energy_wh`、`overall_efficiency`。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；输入电压非法为 `422 invalid_input_voltage`；静态电流非法为 `422 invalid_quiescent_current`；工况数组非法或含非对象为 `422 invalid_operating_points`，工况字段非法为 `422 invalid_operating_point`；曲线缺失、结构或数值非法、电流未严格递增为 `422 invalid_efficiency_curve`。

## LDO 效率评估

`POST /v1/power/ldo/efficiency/estimate`（也可直接调用 `Service.estimate_ldo_efficiency`）按工况评估线性稳压器（LDO）的效率、压差状态与累计能量。

请求字段：

- `operating_points`（必填）：非空数组，每项为对象，含正有限数 `duration_s`、`input_voltage_v`、`requested_output_voltage_v` 以及非负有限数 `output_current_a`。
- `dropout_curve`（必填）：至少两个点的数组，每项为对象，`output_current_a` 与 `dropout_voltage_v` 均为非负有限数，且电流严格递增。
- `quiescent_current_a`（可选）：非负有限数静态电流（安），默认 `0`。

每个工况按输出电流在压差曲线上线性插值得到压差，越界取最近端点。最高输出电压为 `max(0, input_voltage_v - dropout_voltage_v)`，`actual_output_voltage_v` 取它与请求输出电压的较小值；最高值不低于请求值时 `state` 为 `regulated`，否则为 `dropout`。输出功率为实际输出电压乘输出电流；输入功率为输入电压乘输出电流与静态电流之和；损耗为二者之差；效率为输出功率除以输入功率，分母为零时取 `0`。各项能量按功率乘 `duration_s` 除以 `3600` 累加（使用未舍入值），总效率为总输出能量除以总输入能量，累计输入能量为零时取 `0`。

响应 `estimates` 与工况等长同序，每项含 `actual_output_voltage_v`、`dropout_voltage_v`、`input_power_w`、`output_power_w`、`loss_power_w`、`efficiency`、`state`；顶层含 `input_energy_wh`、`output_energy_wh`、`loss_energy_wh`、`overall_efficiency`。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；工况数组非法或含非对象为 `422 invalid_operating_points`，工况字段非法为 `422 invalid_operating_point`；压差曲线缺失、结构或数值非法、电流未严格递增为 `422 invalid_dropout_curve`；静态电流非法为 `422 invalid_quiescent_current`。

## 遥测采样过滤

`POST /v1/telemetry/filter`（也可直接调用 `Service.filter_telemetry`）将含尖峰或不等间隔的电流/电压遥测转为稳定序列，电流与电压两通道独立过滤但共享分段边界。

请求字段：

- `samples`（必填）：非空数组，每项为对象，含严格递增的有限 `timestamp_s` 以及有限数值 `current_a`、`voltage_v`；布尔值不作为数值接受。
- `median_window`（可选）：中值窗口，只能是 `1` 至 `11` 的奇数，默认 `3`。
- `smoothing_factor`（可选）：指数平滑系数，位于 `(0, 1]`，默认 `0.25`。
- `reset_gap_s`（可选）：分段重置间隔，正有限秒数，默认 `30`。

首项开始新段；相邻时间差严格大于 `reset_gap_s` 时，当前项另起新段并清空两通道状态（间隔恰好等于阈值不重置）。每段内分别取当前值与同通道此前至多 `median_window-1` 个原始值的中位数，偶数个候选取中间两值的均值；分段首项的过滤值直接采用中位数，其余项为 `smoothing_factor * 当前中位数 + (1-smoothing_factor) * 上一过滤值`。

响应 `samples` 与输入等长同序，每项含 `timestamp_s`（原样保留）、`filtered_current_a`、`filtered_voltage_v`、`segment_start`（仅首项及间隔触发重置时为 `true`）；顶层含 `segment_count` 与 `sample_count`。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；`samples` 不是非空数组或元素不是对象为 `422 invalid_samples`；时间戳缺失、非有限、为布尔值或未严格递增为 `422 invalid_timestamp`；`current_a`/`voltage_v` 缺失、非有限或为布尔值为 `422 invalid_measurement`；过滤选项类型或范围不符为 `422 invalid_filter_options`。

## 遥测能耗聚合

`POST /v1/telemetry/aggregate`（也可直接调用 `Service.aggregate_telemetry`）把电流/电压遥测按固定时长窗口聚合成充放电能量，并对窗口平均功率做最小二乘趋势估计。正电流表示放电，负电流表示充电。

请求字段：

- `samples`（必填）：至少两项的数组，每项为对象，含严格递增的有限 `timestamp_s` 以及有限数值 `current_a`、`voltage_v`。
- `bucket_duration_s`（必填）：正有限数窗口时长（秒）。
- `max_gap_s`（必填）：正有限数最大允许采样间隔（秒）。
- `trend_threshold_w_per_hour`（可选）：非负有限数趋势阈值，默认 `0`。

从首项时间戳起按 `bucket_duration_s` 划分左闭右开窗口，截止末项时间戳。相邻样本的功率 `current_a * voltage_v` 按线性变化；相邻间隔严格大于 `max_gap_s` 时整段跳过并计入 `skipped_gap_count`（恰好等于不跳过），否则在窗口边界与功率零点处分段做梯形积分。正功率计入放电能量，负功率绝对值计入充电能量，净能量为两者之差。

响应 `buckets` 仅包含有覆盖时长的窗口，每项含 `bucket_start_s`、`bucket_end_s`（末窗口截止末项时间戳）、`covered_duration_s`、`average_power_w`（净能量与覆盖时长折算）、`discharge_energy_wh`、`charge_energy_wh`、`net_energy_wh`；顶层含三种能量总计、`skipped_gap_count`、`trend_slope_w_per_hour` 与 `trend_status`。趋势以窗口中点的小时数与 `average_power_w` 做普通最小二乘：少于两个窗口时斜率为 `null`、状态为 `insufficient`；否则斜率严格高于阈值为 `increasing`，严格低于负阈值为 `decreasing`，其余为 `stable`。成功返回 `200`，且不修改请求对象。

错误响应沿用统一结构：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；`samples` 数量或成员结构非法为 `422 invalid_samples`；时间戳非法或未严格递增为 `422 invalid_timestamp`；`current_a`/`voltage_v` 非法为 `422 invalid_measurement`；`bucket_duration_s`、`max_gap_s` 或 `trend_threshold_w_per_hour` 缺失、为布尔值、非有限或越界为 `422 invalid_options`。

## 遥测趋势分析（滚动功率基线与迟滞告警）

`POST /v1/telemetry/trend/analyze`（也可直接调用 `Service.analyze_telemetry_trend`）对聚合窗口建立滚动中位数功率基线并输出迟滞告警。窗口字段沿用聚合结果：`bucket_start_s`、`bucket_end_s`、`covered_duration_s`、`average_power_w`。

请求字段：

- `buckets`（必填）：非空对象数组，同序处理。四个窗口字段均为有限且非布尔数值；`bucket_start_s < bucket_end_s`，起点严格递增且相邻窗口不重叠（恰好相接允许）；`0 <= covered_duration_s <= bucket_end_s - bucket_start_s`。
- `config`（必填）：对象。
  - `baseline_window`（必填）：正整数，取此前最近多少个有效功率算中位数；偶数个取中间两值均值。
  - `recovery_windows`（必填）：正整数，连续多少个目标较低的有效窗口才降一级。
  - `min_coverage_ratio`（必填）：`[0, 1]` 内有限数覆盖率下限。
  - `warning_deviation_w`、`critical_deviation_w`（必填）：有限阈值，满足 `0 <= warning_deviation_w < critical_deviation_w`。

覆盖时长大于零且覆盖率 `covered_duration_s / (bucket_end_s - bucket_start_s)` 达到下限的窗口才有效。有效窗口先以之前最近 `baseline_window` 个有效功率的中位数为基线计算偏差（当前功率减基线），再把自身加入历史；历史不足 `baseline_window` 时目标为 `warming_up`，实际级别保持 `normal`，基线与偏差返回 `null`。基线就绪后，偏差达到临界或告警阈值目标为 `critical` 或 `warning`，否则为 `normal`。级别从 `normal` 开始：升级立即生效（可连跳两级）；连续 `recovery_windows` 个有效窗口目标较低时只降一级并重新计数，其他有效窗口清零计数。无效窗口目标为 `insufficient`，基线和偏差为 `null`，保持当前级别、清零恢复计数且不进入基线历史。

响应 `results` 与输入同序，每项回显窗口三字段并含 `coverage_ratio`、`average_power_w`、`baseline_power_w`、`deviation_w`、`target_level`、`level`；顶层返回 `final_level`。成功返回 `200`，且不修改请求对象。

错误响应沿用统一结构：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；窗口结构、数值、顺序或重叠错误为 `422 invalid_buckets`；`config` 缺失或字段缺失、为布尔值、非有限、非正整数或不满足顺序约束为 `422 invalid_trend_config`。

## 充电热保护

`POST /v1/battery/thermal/protect`（也可直接调用 `Service.protect_thermal`）按电芯温度与期望充电电流生成限流决定。

请求字段：

- `protection`（必填）：对象，含正有限数 `max_charge_current_a`，以及满足 `recovery_temperature_c < warning_temperature_c < critical_temperature_c` 的三个有限温度阈值。
- `samples`（必填）：非空数组，每项为对象，含严格递增的有限 `timestamp_s`、有限 `temperature_c` 和非负有限 `requested_current_a`；布尔值不作为数值接受。

温度低于告警阈值时热电流上限为 `max_charge_current_a`；处于告警与临界阈值之间时上限随温度线性降至零；达到临界阈值时上限为零并锁存切断。锁存后仅在温度降到恢复阈值或更低时解除，并在该点重新按温区计算。`allowed_current_a` 取期望电流与热上限的较小值；`state` 在低温区、线性降额区和锁存期依次为 `normal`、`derated`、`cutoff`。

响应 `decisions` 与输入等长同序，每项含 `timestamp_s`（原样保留）、`allowed_current_a`、`thermal_limit_a`、`state`；顶层 `final_state` 取末项状态，`cutoff_count` 统计进入 cutoff 的次数。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；`protection` 结构、数值或阈值顺序非法为 `422 invalid_protection_config`；`samples` 结构非法为 `422 invalid_samples`；时间戳非法或未严格递增为 `422 invalid_timestamp`；温度非法为 `422 invalid_temperature`；期望电流非法为 `422 invalid_current`。

## 电芯均衡计划

`POST /v1/battery/balance/plan`（也可直接调用 `Service.plan_balance`）依据各电芯电压、温度与上一轮激活状态生成本轮被动均衡决定。

请求字段：

- `cells`（必填）：非空数组，每项含唯一非空字符串 `id`、正有限数 `voltage_v` 和有限数 `temperature_c`。
- `config`（必填）：对象，含正有限数 `start_delta_v` 与 `bleed_current_a`、满足 `0 <= stop_delta_v < start_delta_v` 的有限数 `stop_delta_v`、有限数 `max_temperature_c`，以及不超过电芯数的正整数 `max_channels`。
- `previous_active_ids`（可选）：无重复且都存在于 `cells` 的 id 数组，省略视为空。

以最低电芯电压为 `target_voltage_v`，各电芯压差为自身电压减该值。温度达到 `max_temperature_c`（含相等）时禁止均衡；温度低于上限时，原未激活电芯压差达到（含等于）`start_delta_v` 即成为候选，原激活电芯压差严格大于 `stop_delta_v` 即可继续。候选按压差降序、`id` 升序排序，取前 `max_channels` 个激活。

响应 `decisions` 与输入等长同序，每项含 `id`、`delta_voltage_v`、`active`、`bleed_current_a`（选中项为配置电流，其余为 `0`）和 `reason`（`selected`、`below_threshold`、`temperature_blocked` 或 `channel_limited`）；顶层含 `target_voltage_v`、按上述排序得到的 `active_ids` 和 `status`（有选中项为 `balancing`，否则为 `idle`）。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；`cells` 结构非法为 `422 invalid_cells`；`id` 缺失、为空或重复为 `422 invalid_cell_id`；电压、温度非法分别为 `422 invalid_cell_voltage`、`422 invalid_cell_temperature`；`config` 缺失或非法为 `422 invalid_balance_config`；`previous_active_ids` 类型错误、重复或引用未知 id 为 `422 invalid_previous_active_ids`。

## 充电过程计划

`POST /v1/battery/charge/plan`（也可直接调用 `Service.plan_charging`）按固定步长生成恒流转 taper 的充电计划，使 SoC 从初值充至目标值。

请求字段：

- `capacity_ah`（必填）：正有限数，电池容量。
- `initial_soc`、`target_soc`（必填）：位于 `[0, 1]` 的有限数，且 `initial_soc <= target_soc`；布尔值不作为数值接受。
- `step_duration_s`（必填）：正有限数，单步时长。
- `max_steps`（必填）：正整数，最多规划步数。
- `config`（必填）：对象，含正有限数 `max_current_a` 与 `charge_voltage_v`、位于 `(0, 1]` 的有限数 `coulombic_efficiency`、位于 `[0, target_soc)` 的有限数 `taper_start_soc`，以及大于零且不超过 `max_current_a` 的有限数 `taper_end_current_a`。

从 `initial_soc` 起，当前 SoC 不高于 `taper_start_soc` 时使用 `max_current_a`；此后电流随距目标的剩余 SoC 距离线性下降，到达目标时恰为 `taper_end_current_a`。每步 SoC 增量为 `current_a × coulombic_efficiency × duration_s ÷ (capacity_ah × 3600)`。整步会越过目标时缩短该步时长，使末值恰好等于目标。

响应 `steps` 依次含 `start_soc`、`end_soc`、`duration_s`、`current_a`、`input_energy_wh`（能量为 `charge_voltage_v × current_a × duration_s ÷ 3600`）；顶层含 `final_soc`、`elapsed_s`、`input_energy_wh`、`status`。到达目标时 `status` 为 `completed`；步数耗尽仍未到达为 `incomplete`。初始 SoC 即目标时无步骤、汇总量为零且 `completed`。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；容量非法为 `422 invalid_capacity`；SoC 越界或初值高于目标为 `422 invalid_soc_range`；`config` 缺失或非法为 `422 invalid_charge_config`；`step_duration_s` 或 `max_steps` 非法为 `422 invalid_plan_options`。

## 多电池包并联放电调度

`POST /v1/battery/packs/parallel/dispatch`（也可直接调用 `Service.dispatch_parallel_packs`）在多电池包并联母线上执行放电调度与故障隔离。

请求字段：

- `config`（必填）：对象，含非负有限数 `max_bus_voltage_delta_v`（包电压与母线电压允许偏差）和正整数 `recovery_samples`（恢复接通所需连续安全样本数）。
- `samples`（必填）：非空数组，每项含严格递增的有限 `timestamp_s`、非负有限 `requested_bus_current_a`、正有限 `bus_voltage_v` 以及非空 `packs`；各样本须保持同一组唯一非空 `id`，每个包含正有限 `voltage_v`、非负有限 `max_discharge_current_a` 和布尔 `fault`。

所有包初始接通。`fault` 为 `true` 或包电压与母线电压绝对差大于允许值时，该包在当前样本立即隔离并分配零电流；隔离包连续 `recovery_samples` 个样本不再命中条件后，在最后一个安全样本重新接通，期间再次不安全会把连续计数清零。母线请求在接通包间等额分配，达到自身上限的包固定在上限，余量继续由其余包等分，直至请求满足或全部到达上限。

响应 `decisions` 与样本同序，每项含 `timestamp_s`、`pack_decisions`（按输入顺序给出 `id`、`allocated_current_a`、`state`，`state` 仅取 `connected` 或 `isolated`）、`allocated_bus_current_a`、`unmet_bus_current_a` 和 `status`；未满足量为零时 `status` 为 `satisfied`，请求大于零且无接通包时为 `no_available_pack`，其余为 `constrained`。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；`config` 缺失或非法为 `422 invalid_parallel_config`；`samples` 结构、时间戳、母线请求电流或母线电压非法为 `422 invalid_samples`；`packs` 结构、id 集合或包字段非法为 `422 invalid_packs`。

## 无线充电协商

`POST /v1/power/wireless/negotiate`（也可直接调用 `Service.negotiate_wireless_charging`）按遥测样本在充电档位间协商无线传输功率，并对异物与过热执行锁存保护。

请求字段：

- `config`（必填）：对象，含正有限数 `transmitter_max_power_w` 与 `receiver_max_power_w`（两端功率上限）、满足 `recovery_temperature_c < warning_temperature_c < critical_temperature_c` 的三个有限温度阈值，以及非空 `profiles`；每个档位含唯一非空 `id`、正有限数 `voltage_v` 与 `max_current_a`、布尔 `accepted`，且至少一个档位 `accepted` 为 `true`。
- `samples`（必填）：非空数组，每项为对象，含严格递增的有限 `timestamp_s`、布尔 `receiver_present`、非负有限 `requested_power_w`、`[0, 1]` 内有限 `coupling`、有限 `coil_temperature_c` 和布尔 `foreign_object`。

档位功率为 `voltage_v * max_current_a`；交付上限为档位功率与两端上限的最小值乘 `coupling` 与 `thermal_factor`。`thermal_factor` 在告警温度及以下为 `1`，告警与临界温度之间线性降至 `0`。每个样本在可接受档位中优先选交付上限满足请求的最小档位（按档位功率、`id` 升序），否则选交付上限最大者（并列按档位功率、`id` 升序）；交付功率为请求与所选档位交付上限的较小值。零请求或接收端不在位时不选档位、不传输。样本出现异物或温度达到临界值时锁存故障并停止传输；仅在后续样本接收端不在位、无异物且温度不高于恢复值时解除，该样本空闲。

响应 `decisions` 与样本等长同序，每项含 `timestamp_s`（原样保留）、`selected_profile_id`、`delivered_power_w`、`unmet_power_w`、`thermal_factor`、`state`（`idle`、`charging`、`limited` 或 `fault`）和 `fault_reason`（`foreign_object`、`over_temperature` 或 `null`）；顶层 `final_state` 取末项状态，`fault_count` 统计锁存次数。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；`config` 结构、功率上限或温度阈值非法为 `422 invalid_wireless_config`；`profiles` 结构或档位字段非法为 `422 invalid_profiles`；`samples` 结构非法为 `422 invalid_samples`；时间戳非法或未严格递增为 `422 invalid_timestamp`；样本字段非法为 `422 invalid_wireless_sample`。

## 能耗基准比较

`POST /v1/energy/benchmark/compare`（也可直接调用 `Service.compare_energy_benchmark`）按场景把历史基准运行与当前运行折算成功率后比较。

请求字段：

- `scenarios`（必填）：非空数组，每项为对象，含唯一非空字符串 `id`、非空 `baseline_runs` 和非空 `current_runs`；每次运行为对象，含非负有限数 `energy_wh` 和正有限数 `duration_s`。
- `regression_threshold_percent`（可选）：非负有限数，默认 `5`。

单次运行功率为 `energy_wh * 3600 / duration_s`，每组取算术平均。响应 `results` 与输入同序，每项含 `id`、`baseline_power_w`、`current_power_w`、`delta_power_w`（当前减基准）、`change_percent` 和 `status`。基准功率大于零时 `change_percent` 为功率差相对基准的百分比；两组均为零时取 `0`；仅基准为零时取 `null`。上下边界分别为基准功率乘以 `1 + 阈值/100` 与 `1 - 阈值/100`：当前功率严格高于上界为 `regression`，严格低于下界为 `improvement`，否则为 `stable`；仅基准为零而当前大于零时直接判为 `regression`。顶层返回 `regression_count`、`improvement_count` 和 `overall_status`，总体判定退化优先，其次为改善，否则为稳定。成功返回 `200`，且不修改请求对象。

错误响应沿用 `{"error":{"code":...,"message":...}}`：请求体缺失、JSON 解析失败或顶层非对象为 `400 invalid_json`；`scenarios` 缺失、为空、非数组或成员非对象为 `422 invalid_scenarios`；`id` 非法或重复为 `422 invalid_scenario_id`；运行集合缺失、为空、非数组或含非对象为 `422 invalid_runs`；运行数值缺失、为布尔值、非有限或超出范围为 `422 invalid_run_measurement`；阈值非法为 `422 invalid_options`。

## 验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

当前基线刻意不包含电池建模与充放电策略的实现，以便后续任务从已冻结事实出发独立设计并验证这些能力。
