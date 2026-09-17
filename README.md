# EV PACK 运行数据统计分析

一车一个 CSV，原始文件只读。分布分析和模型结果输出为 CSV/JSON，可接入已有作图程序。本目录没有真实车辆数据，验证使用合成数据。

## 怎么运行

以下命令都在本项目目录执行。首次使用只需按顺序完成前三步；原始 CSV 不会被修改。

1. 准备 Python 环境。项目中已有 `.venv` 时跳过安装；在新电脑上执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

2. 检查 [config.example.json](config.example.json)。默认假定充电允许字段名为 `chargeAllow`、报警编码为 `0=正常` 和 `1=报警`。如果实际字段名或编码不同，复制该文件后修改，例如保存为 `config.json`。

3. 预处理原始数据。将 `/path/to/raw_csv` 替换为存放 VIN CSV 的目录；每个 CSV 应只包含一个 VIN。

```bash
.venv/bin/python prepare_data.py /path/to/raw_csv outputs/cache --config config.example.json
```

预处理完成后，先查看这两个文件，确认数据质量和闪充识别是否合理：

```bash
open outputs/cache/quality.csv
open outputs/cache/charging_events.csv
```

4. 执行工况关联分析：

```bash
.venv/bin/python analyze_conditions.py outputs/cache outputs/conditions
```

重点先看 `outputs/conditions/model_diagnostics.json` 是否显示 `status: "ok"`，再查看 `model_coefficients.csv`、`stratified_rates.csv` 和 `rates_by_vin.csv`。

5. 软件版本前后分析：

```bash
.venv/bin/python analyze_software.py outputs/cache outputs/software --target 3.03.07
```

6. 打胶方案前后分析。先准备一个实施清单 CSV，再运行：

```bash
.venv/bin/python analyze_glue.py outputs/cache outputs/glue --interventions /path/to/glue_dates.csv
```

每次运行请使用新的或空的输出目录，避免混入旧结果。预处理生成的 `outputs/cache` 可被三个分析脚本复用；只有修改闪充定义、字段映射或窗口大小时，才需要重新预处理原始 CSV。仅修改模型时不需要重读原始数据。

打胶实施清单格式，每 VIN 一行：

```csv
vin,glue_time
EXAMPLE_VIN,2026-08-01 12:00:00
```

`message_time` 和 `glue_time` 必须使用一致的时区约定。无时区字符串按同一时钟解释；混用有时区和无时区值前需统一。Unix 时间戳需在配置里明确 `timestamp_unit: "s"` 或 `"ms"`。

软件、打胶默认观察前后各 30 天，排除实施时间两侧各 1 天，每车每侧、每项结局至少 1 小时有效数据：

```bash
python analyze_software.py outputs/cache outputs/software_14d --days 14 --exclude-days 1 --min-hours 2
```

## 指标口径

- `AFE_error` 是非负整数累计值。相邻时间间隔在 `(0, 5]` 秒内、两端数值有效且不下降时，使用差值。首条记录、下降区间、断档区间及缺失区间不计误码，也不计该指标的暴露时间。下降后的下一段恢复计算。没有擅自假定计数器回绕上限。
- 主指标是 **新增误码次数 / 有效观测小时**。没有通信报文总数，也没有完整上电标志，因此不能解释为报文错误比例或真实上电小时发生率。无法观测到的“清零后又增加到更高值”仅靠该计数器不能识别。
- 七个报警分别统计 **正常→报警次数 / 有效观测小时** 和 **报警持续时间 / 有效观测时间**。默认正常编码 `[0]`、报警编码 `[1]`，其余为未知。需按实际协议修改配置；不会把 255 等未知码默认为报警。
- 报警持续时间采用相邻有效记录之间保持前值的近似；两端都必须是已知编码。断档后第一帧已报警时，不猜测触发时刻；后续连续报警时间仍可计入。
- 所有分子使用各自匹配的有效时长作分母。零暴露对应空值，不伪装成零风险。
- 默认 5 分钟窗口。间隔整体归入前一条记录所在窗口，最多有一个有效采样间隔的边界偏移；保留 `span_start/span_end`，前后比较剔除跨实施边界窗口。工况按实际间隔加权，用间隔开始时的值对应其后的误码增量／报警触发。

## 闪充定义与修改位置

在 `prepare_data.py` 的 `Config`、`FlashDetector.feed()` 中修改；阈值也可通过 JSON 配置覆盖。

1. `chargeAllow` 属于允许值且连续 10 帧电流 `< -2 A`，开始时间回溯到这 10 帧中的第一帧。
2. 开始前必须观察到不满足开始条件的有效帧。文件或断档中途已经在充电时，不冒充一次完整事件。
3. 连续 10 帧电流 `>= 0 A` 后确认结束，结束时间取这 10 帧中的第一帧。
4. 最小电流 `< -600 A` 且结束 SOC − 开始前 SOC `> 10`，才认定闪充。SOC 假定采用 0–100 百分点。
5. 超过最大允许间隔或电流缺失会中断识别；跨 CSV 读取块保留识别状态。末尾未完成事件不计完整闪充。

`charging_events.csv` 保存全部完整充电事件和 `is_flash` 标记。`during_flash` 是窗口内处于闪充的时间比例；`recent_flash` 是距最近已确认闪充结束不超过 24 小时的时间比例；`prior_flash_count` 是时刻之前已确认完成的闪充次数的窗口均值。后者仅覆盖观测期，不是终生次数。历史次数在结束确认之后才增加。

`during_flash` 依赖整段充电的峰值和 SOC 增量，是回顾性关联变量，不能作为实时预测特征。未观测完整的充电会使已确认次数偏低；有大量断档的车辆需结合质量表判断。

## 模型及数据分布

主模型使用 **Poisson GEE + log(有效小时) offset**，按 VIN 计算稳健协方差，处理同车重复观测。独立工作相关结构无需建立巨大的车内相关矩阵。实现依据 [statsmodels GEE 文档](https://www.statsmodels.org/stable/gee.html) 和 [GEE API](https://www.statsmodels.org/stable/generated/statsmodels.genmod.generalized_estimating_equations.GEE.html)。

模型分别拟合新增 AFE 误码和七类报警触发次数；报警持续占比先做描述及配对比较，不把“秒数”误当独立二项试验。

`analysis_common.py` 的 `SCALES` 和 `fit_models()` 集中定义变量：最高温度、温差、车速、电流、SOC、闪充变量，以及日期趋势和软件版本。连续变量拆为本车平均值 `*_between` 与偏离本车平均值 `*_within`，区分车间差异和车内工况变化。温度、车速另加二次项。

- 这是调整已观测工况的关联模型。GEE 的 VIN 分组处理相关性，**不等同于 VIN 固定效应，也不能消除所有车辆混杂**。日期项为线性趋势，未声称完整校正季节效应。
- `rate_ratio = exp(coefficient)`，输出 95% 区间和 BH 多重检验校正值。温度单位为 10℃、车速为 10 km/h、电流为 100 A、SOC 为 10 个百分点。包含二次项时，一次项的率比不能单独解释为全温度／车速范围的恒定效应。
- 输出每车零值比例、均值、方差、95 分位数及最大值。窗口有效时长不等，原始计数的“方差大于均值”只能描述，不能单独决定模型。
- 输出拟合后的 Pearson 离散程度、实际零比例和 Poisson 预期零比例。稳健协方差允许一定方差失配，但不修复错误的均值关系或未观测混杂；零多不自动意味着需要零膨胀模型。真实数据诊断后再决定是否加入负二项或两部分模型。
- 描述统计使用全部压缩窗口。模型默认总计最多 100000 行，按 VIN 均匀分配抽样额度、固定随机种子。它较偏向等车辆贡献，而非按车队全部记录量加权；抽样不会更改描述统计。可用 `--max-model-rows` 调整。
- 模型仅使用结局有效时长达到窗口 50% 的记录，工况要求覆盖窗口有效时间的 90%。模型默认至少 20 个有效 VIN；此值是可修改的实现门槛，不是统计有效性的保证。缺失工况导致完整案例筛选；缺失／恒定因素会明确列入诊断，不能将未估计因素解读为无影响。
- 全零结局、变量共线或拟合失败时保留原因，不输出伪造的效应估计。

## 软件与打胶前后

`analyze_software.py` 使用每车首次进入目标版本的时间。只比较其之前最近一个已知旧版本与首次连续目标版本记录；排除未知版本、后续切换及再次回到目标版本的记录。一开始就是目标版本的车辆没有可用的升级前数据。`interventions.csv` 保留旧版本最后观测时间和目标版本首次观测时间，可检查升级时间的不确定区间。

`analyze_glue.py` 使用外部实际实施时间，并在调整模型中加入软件版本。若软件和打胶完全同步，模型可能因共线而无法区分，诊断会说明。当前没有擅自从软件版本推断打胶日期，也没有实现无对照数据支持的双重差分。

两者均输出逐 VIN 配对结果、等车辆权重的平均发生率差、以 VIN 为单位的 1000 次配对 bootstrap 区间，以及工况调整模型。零基线的率比留空，仍报告绝对差。前后表述为观察到的变化，不能仅凭这些结果认定软件、打胶或闪充导致报警。

## 输出与阅读顺序

| 文件 | 内容 |
|---|---|
| 缓存 `quality.csv` | 缺字段、坏时间、乱序、重复、长断档、累计值下降等 |
| 缓存 `windows/*.parquet` | 每 VIN 的小窗口指标和工况，可直接供作图 |
| `rates_by_vin.csv` | 全量数据的每车误码／报警发生率、报警时间占比 |
| `stratified_rates.csv` | 温度、温差、车速、闪充次数与闪充状态分层统计 |
| `distributions.csv` | 各车各结局的计数分布；前后分析中为两个时期合并分布 |
| `model_sampling.csv` | 每车模型候选及实际抽样窗口数 |
| `model_diagnostics.json` | 最终样本量、公式、遗漏项、离散程度和拟合状态 |
| `model_coefficients.csv` | 调整后的发生率比、区间及校正 p 值 |
| 前后分析 `paired_by_vin.csv` / `paired_summary.csv` | 每车前后差异及车队配对汇总 |

缓存的时间字段采用 Unix 秒；可用 `pd.to_datetime(series, unit="s", utc=True)` 转换。分层区间基于窗口均值，缺失工况单独标记为 `nan`。

磁盘处理使用 SQLite 暂存**单 VIN 的必要字段**并排序，同时间戳保留原文件第一条。需要为单车暂存表和索引留出临时磁盘空间；可通过 `TMPDIR` 指定位置。内存主要取决于一个原始块、单 VIN 压缩表和限定行数的模型输入，不会 concat 全部原始 CSV。

验证命令：`python -m unittest -v test_analysis.py`。
