"""同车首次进入目标软件版本前后：新增误码与报警变化。"""
from pathlib import Path

import pandas as pd

from analysis_common import before_after_parser, run_before_after


def software_changes(cache, target):
    transitions = pd.read_csv(Path(cache) / "software_transitions.csv", dtype={"vin": str, "software": str})
    changes = []
    for vin, runs in transitions.groupby("vin", sort=False):
        runs = runs.sort_values("timestamp").reset_index(drop=True)
        matches = runs.index[runs.software.eq(target)]
        if not len(matches):
            continue
        index = matches[0]
        previous = runs.loc[:index - 1]
        previous = previous[previous.software.ne("unknown")]
        # 一开始就已经是目标版本的车辆，没有可识别的升级前数据。
        if previous.empty:
            continue
        old = previous.iloc[-1]
        changes.append({"vin": vin, "change_time": runs.loc[index, "timestamp"],
                        "old_version": old.software, "last_old_time": old.last_seen,
                        "target_end": runs.loc[index, "last_seen"]})
    if not changes:
        raise ValueError("没有同时观测到旧版本和首次目标版本的 VIN")
    return pd.DataFrame(changes)


def main():
    parser = before_after_parser(__doc__)
    parser.add_argument("--target", default="3.03.07")
    args = parser.parse_args()
    if not 0 <= args.exclude_days < args.days:
        parser.error("需要 0 <= exclude-days < days")
    run_before_after(args, software_changes(args.cache, args.target), adjust_software=False)


if __name__ == "__main__":
    main()
