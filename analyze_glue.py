"""按外部 VIN—打胶日期清单分析同车实施前后的误码与报警。"""
import pandas as pd

from analysis_common import before_after_parser, run_before_after


def main():
    parser = before_after_parser(__doc__)
    parser.add_argument("--interventions", required=True, help="包含 vin、glue_time 的 CSV")
    args = parser.parse_args()
    if not 0 <= args.exclude_days < args.days:
        parser.error("需要 0 <= exclude-days < days")
    changes = pd.read_csv(args.interventions, dtype={"vin": str})
    if not {"vin", "glue_time"}.issubset(changes.columns):
        parser.error("实施清单需要 vin 和 glue_time")
    if changes.vin.isna().any() or changes.vin.duplicated().any():
        parser.error("实施清单需要非空且唯一的 VIN")
    times = pd.to_datetime(changes.glue_time, format="mixed", errors="coerce", utc=True)
    if times.isna().any():
        parser.error("glue_time 存在无法解析的时间")
    changes["change_time"] = times.astype("int64") / 1e9
    run_before_after(args, changes, adjust_software=True)


if __name__ == "__main__":
    main()
