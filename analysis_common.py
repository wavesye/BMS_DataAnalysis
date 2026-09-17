"""小窗口表上的分布统计、计数模型和同车前后比较。"""
import json
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import patsy
import statsmodels.api as sm
from statsmodels.stats.multitest import multipletests

from prepare_data import ALARMS, new_output


METRICS = {"afe": ("error_count", "error_seconds"),
           **{key: (key + "_onsets", key + "_seconds") for key in ALARMS}}
# 单位缩放后的系数更容易阅读；within 表示相对本车平均工况的变化。
SCALES = {"temp": 10, "temp_spread": 10, "speed": 10, "current": 100,
          "soc": 10, "prior_flash_count": 1, "recent_flash": 1, "during_flash": 1}
BINS = {"temp": [-np.inf, 0, 20, 30, 40, 50, np.inf],
        "speed": [-np.inf, 1, 30, 60, 90, np.inf],
        "temp_spread": [-np.inf, 5, 10, 20, np.inf],
        "prior_flash_count": [-np.inf, 1, 5, 10, 20, np.inf],
        "during_flash": [-np.inf, .001, .999, np.inf]}


def rate_table(data, groups):
    tables = []
    for metric, (count, seconds) in METRICS.items():
        columns = [count, seconds]
        if metric != "afe":
            columns += [metric + "_active_seconds"]
        table = data.groupby(groups, observed=True, dropna=False)[columns].sum().reset_index()
        table = table.rename(columns={count: "count", seconds: "valid_seconds"})
        table["metric"] = metric
        table["rate_per_hour"] = table["count"] / (table.valid_seconds / 3600).replace(0, np.nan)
        if metric != "afe":
            table["active_fraction"] = table.pop(metric + "_active_seconds") / table.valid_seconds.replace(0, np.nan)
        tables.append(table)
    return pd.concat(tables, ignore_index=True)


def load_analysis(cache, out, transform=None, max_rows=100_000, min_coverage=.5):
    """描述统计使用全量窗口；只对模型输入按 VIN 均匀抽样，限制内存。"""
    cache = Path(cache)
    if not (cache / "COMPLETE").exists():
        raise ValueError("缓存未完整生成，请重新运行 prepare_data.py")
    files = sorted((cache / "windows").glob("*.parquet"))
    if max_rows < len(files):
        raise ValueError("max-model-rows 至少应等于车辆数")
    per_vin = max_rows // len(files)
    samples, distributions, strata, rates, coverage = [], [], [], [], []
    for file in files:
        data = pd.read_parquet(file)
        if transform is not None:
            data = transform(data)
        if data.empty:
            continue
        vin = data.vin.iloc[0]
        data["period"] = data.get("period", "all")
        rates.append(rate_table(data, ["vin", "period"]))
        for metric, (count, seconds) in METRICS.items():
            valid = data[seconds] > 0
            values = data.loc[valid, count]
            distributions.append({"vin": vin, "metric": metric, "windows": len(values),
                                  "zero_fraction": values.eq(0).mean(), "count_mean": values.mean(),
                                  "count_variance": values.var(), "count_p95": values.quantile(.95),
                                  "count_max": values.max(),
                                  "valid_hours": data[seconds].sum() / 3600})
        for feature, boundaries in BINS.items():
            data["bin"] = pd.cut(data[feature], boundaries, right=False).astype(str)
            table = rate_table(data, ["vin", "period", "bin"])
            table["feature"] = feature
            strata.append(table)
        # 缺失工况不以零补齐；模型要求使用的工况覆盖窗口的大部分有效时间。
        for feature in SCALES:
            full = data[feature + "_seconds"] >= .9 * data.observed_seconds
            data.loc[~full, feature] = np.nan
            weights = data[feature + "_seconds"].where(full, 0)
            mean = (data[feature].fillna(0) * weights).sum() / weights.sum() if weights.sum() else np.nan
            data[feature + "_between"] = mean / SCALES[feature]
            data[feature + "_within"] = (data[feature] - mean) / SCALES[feature]
        eligible = data.observed_seconds >= data.window_seconds * min_coverage
        data = data.loc[eligible].copy()
        available = len(data)
        if available > per_vin:
            data = data.sample(n=per_vin, random_state=42).sort_values("window")
        coverage.append({"vin": vin, "eligible_windows": available, "sampled_windows": len(data)})
        samples.append(data)
    if not rates:
        raise ValueError("没有符合分析条件的窗口")
    full_rates = pd.concat(rates, ignore_index=True)
    full_rates.to_csv(out / "rates_by_vin.csv", index=False)
    pd.DataFrame(distributions).to_csv(out / "distributions.csv", index=False)
    pd.concat(strata, ignore_index=True).to_csv(out / "stratified_rates.csv", index=False)
    pd.DataFrame(coverage).to_csv(out / "model_sampling.csv", index=False)
    return pd.concat(samples, ignore_index=True), full_rates


def fit_models(data, out, treatment=None, min_vins=20, min_coverage=.5, adjust_software=True):
    """Poisson GEE：log(有效小时) offset，独立工作相关结构、VIN 稳健协方差。"""
    coefficients, diagnostics = [], []
    if data.empty:
        (out / "model_diagnostics.json").write_text('[{"status":"没有满足覆盖要求的模型窗口"}]', encoding="utf-8")
        return
    data = data.copy()
    data["calendar_days"] = (data.window - data.window.min()) / 86400
    terms = []
    omitted = []
    for feature in SCALES:
        for suffix in ("within", "between"):
            name = feature + "_" + suffix
            if data[name].nunique() > 1:
                terms.append(name)
            else:
                omitted.append(name)
    # 二次项用于温度/车速非线性；可直接修改此处扩展样条或交互。
    for feature in ("temp", "speed"):
        if feature + "_within" in terms:
            terms.append(f"I({feature}_within ** 2)")
    if data.calendar_days.nunique() > 1:
        terms.append("calendar_days")
    if adjust_software and data.software.nunique() > 1:
        terms.append("C(software)")
    if treatment:
        terms.append(treatment)
    for metric, (count, seconds) in METRICS.items():
        subset = data.loc[data[seconds] >= data.window_seconds * min_coverage].copy()
        if treatment and metric + "_paired" in subset:
            subset = subset[subset[metric + "_paired"]]
        diag = {"metric": metric, "omitted_constant_or_missing_terms": omitted,
                "sampled_windows": len(subset)}
        try:
            if subset.empty or subset[count].sum() == 0:
                raise ValueError("无有效暴露或结局全为零，不拟合")
            formula = count + " ~ " + (" + ".join(terms) if terms else "1")
            y, x = patsy.dmatrices(formula, subset, return_type="dataframe", NA_action="drop")
            subset = subset.loc[x.index]
            if treatment:
                exposure = subset.groupby(["vin", treatment])[seconds].sum().unstack(fill_value=0)
                if not {0, 1}.issubset(exposure.columns):
                    raise ValueError("前后两种状态不完整")
                keep = exposure.index[(exposure[0] > 0) & (exposure[1] > 0)]
                subset = subset[subset.vin.isin(keep)]
                if subset.empty:
                    raise ValueError("没有在模型抽样中保留双方有效窗口的 VIN")
                # 筛选后重建设计矩阵，删除未出现的分类水平。
                y, x = patsy.dmatrices(formula, subset, return_type="dataframe", NA_action="drop")
            diag.update(formula=formula, used_windows=len(subset), vins=subset.vin.nunique())
            if treatment:
                diag["sampled_hours_by_post"] = {str(k): float(v / 3600) for k, v in subset.groupby(treatment)[seconds].sum().items()}
            if subset.vin.nunique() < min_vins:
                raise ValueError(f"有效 VIN 少于 {min_vins}，仅保留描述统计")
            if treatment and subset[treatment].nunique() < 2:
                raise ValueError("前后两种状态不完整")
            if len(subset) <= x.shape[1] + 10 or np.linalg.matrix_rank(x) < x.shape[1]:
                raise ValueError("样本不足或模型变量共线；请精简 SCALES/公式，不能单独识别相关效应")
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter("always")
                model = sm.GEE(y.iloc[:, 0], x, groups=subset.vin,
                               offset=np.log(subset[seconds] / 3600),
                               family=sm.families.Poisson(),
                               cov_struct=sm.cov_struct.Independence())
                result = model.fit(maxiter=100)
            if not result.converged or not np.isfinite(result.params).all() or not np.isfinite(result.bse).all():
                raise ValueError("模型未收敛或协方差不可用")
            mu = np.asarray(result.fittedvalues)
            diag.update(status="ok", warnings=[str(w.message) for w in captured],
                        observed_zero_fraction=float((y.iloc[:, 0] == 0).mean()),
                        poisson_expected_zero_fraction=float(np.exp(-mu).mean()),
                        pearson_dispersion=float(np.sum((y.iloc[:, 0] - mu) ** 2 / np.maximum(mu, 1e-12))
                                                 / max(len(y) - x.shape[1], 1)))
            ci = result.conf_int()
            for name in x.columns:
                if name == "Intercept":
                    continue
                coefficients.append({"metric": metric, "term": name, "coefficient": result.params[name],
                                     "rate_ratio": np.exp(result.params[name]),
                                     "ci_low": np.exp(ci.loc[name, 0]), "ci_high": np.exp(ci.loc[name, 1]),
                                     "p_value": result.pvalues[name]})
        except (ValueError, np.linalg.LinAlgError) as exc:
            diag["status"] = str(exc)
        diagnostics.append(diag)
    table = pd.DataFrame(coefficients, columns=["metric", "term", "coefficient", "rate_ratio", "ci_low", "ci_high", "p_value"])
    if len(table):
        # 多报警、多因素的探索性比较，统一输出 BH 校正值。
        table["q_value_bh"] = multipletests(table.p_value, method="fdr_bh")[1]
    table.to_csv(out / "model_coefficients.csv", index=False)
    (out / "model_diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")


def paired_comparison(rates, out, min_hours=1, bootstrap=1000):
    """每个结局单独筛选双方暴露充分的 VIN；配对自助法以 VIN 为抽样单位。"""
    rows, summaries = [], []
    rng = np.random.default_rng(42)
    for metric in METRICS:
        table = rates.loc[rates.metric.eq(metric)].set_index(["vin", "period"])
        before = table.xs("before", level="period") if "before" in table.index.get_level_values("period") else table.iloc[:0]
        after = table.xs("after", level="period") if "after" in table.index.get_level_values("period") else table.iloc[:0]
        paired = before.join(after, lsuffix="_before", rsuffix="_after", how="inner")
        paired = paired[(paired.valid_seconds_before >= min_hours * 3600)
                        & (paired.valid_seconds_after >= min_hours * 3600)].copy()
        if paired.empty:
            summaries.append({"metric": metric, "vins": 0})
            continue
        paired["rate_difference"] = paired.rate_per_hour_after - paired.rate_per_hour_before
        paired["rate_ratio"] = paired.rate_per_hour_after / paired.rate_per_hour_before.replace(0, np.nan)
        paired["active_fraction_difference"] = paired.active_fraction_after - paired.active_fraction_before
        paired["metric"] = metric
        rows.append(paired.reset_index())
        changes = paired.rate_difference.to_numpy()
        # 逐次抽样，避免 bootstrap 次数 × VIN 数的大矩阵。
        draws = [rng.choice(changes, len(changes), replace=True).mean() for _ in range(bootstrap)]
        summaries.append({"metric": metric, "vins": len(paired),
                          "mean_rate_difference": changes.mean(),
                          "median_rate_difference": np.median(changes),
                          "difference_ci_low": np.quantile(draws, .025) if len(paired) >= 2 else np.nan,
                          "difference_ci_high": np.quantile(draws, .975) if len(paired) >= 2 else np.nan,
                          "mean_active_fraction_difference": paired.active_fraction_difference.mean()})
    pd.concat(rows, ignore_index=True).to_csv(out / "paired_by_vin.csv", index=False) if rows else pd.DataFrame(columns=["vin", "metric"]).to_csv(out / "paired_by_vin.csv", index=False)
    pd.DataFrame(summaries).to_csv(out / "paired_summary.csv", index=False)


def analysis_parser(description):
    import argparse
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("cache", help="prepare_data.py 生成的目录")
    parser.add_argument("output", help="新的分析结果目录")
    parser.add_argument("--max-model-rows", type=int, default=100_000)
    parser.add_argument("--min-vins", type=int, default=20)
    parser.add_argument("--min-coverage", type=float, default=.5)
    return parser


def run_before_after(args, changes, adjust_software):
    out = new_output(args.output)
    changes.to_csv(out / "interventions.csv", index=False)
    lookup = changes.set_index("vin")
    def select(data):
        vin = data.vin.iloc[0]
        if vin not in lookup.index:
            return data.iloc[:0]
        change = lookup.loc[vin]
        t = change.change_time
        before = ((data.span_start >= t - args.days * 86400)
                  & (data.span_end <= t - args.exclude_days * 86400))
        after = ((data.span_start >= t + args.exclude_days * 86400)
                 & (data.span_end <= t + args.days * 86400))
        if "old_version" in changes:
            before &= data.software.eq(change.old_version) & data.span_end.le(change.last_old_time)
            after &= data.software.eq(args.target) & data.span_end.le(change.target_end)
        chosen = data.loc[before | after].copy()
        chosen["post"] = after.loc[chosen.index].astype(int)
        chosen["period"] = np.where(chosen.post.eq(1), "after", "before")
        return chosen
    data, rates = load_analysis(args.cache, out, select, args.max_model_rows, args.min_coverage)
    paired_comparison(rates, out, args.min_hours)
    for metric in METRICS:
        exposure = rates[rates.metric.eq(metric)].pivot(index="vin", columns="period", values="valid_seconds")
        eligible = exposure.reindex(columns=["before", "after"]).ge(args.min_hours * 3600).all(axis=1)
        data[metric + "_paired"] = data.vin.isin(exposure.index[eligible])
    fit_models(data, out, treatment="post", min_vins=args.min_vins,
               min_coverage=args.min_coverage, adjust_software=adjust_software)


def before_after_parser(description):
    parser = analysis_parser(description)
    parser.add_argument("--days", type=float, default=30, help="实施前后各观察多少天")
    parser.add_argument("--exclude-days", type=float, default=1, help="实施时间两侧排除多少天")
    parser.add_argument("--min-hours", type=float, default=1, help="每个 VIN 每侧最少有效暴露小时")
    return parser
