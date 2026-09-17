"""逐 VIN 分块读取，磁盘排序；只输出事件和窗口汇总，不改原始 CSV。"""
import argparse
import json
import sqlite3
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


ALARMS = {
    "sampling": "sampling_alarm",
    "communication": "host_slave_communication_alarm",
    "battery": "battery_alarm",
    "voltage_disconnect": "实时电压采样断线",
    "temperature_disconnect": "实时温度采样断线",
    "cascade": "实时级联故障",
    "register": "实时寄存器异常",
}


@dataclass
class Config:
    chunksize: int = 200_000
    encoding: str = "utf-8-sig"
    timestamp_unit: str | None = None  # Unix 时间戳时填 s / ms；默认日期字符串
    window_seconds: int = 300
    max_gap_seconds: float = 5
    start_frames: int = 10
    end_frames: int = 10
    start_current: float = -2
    peak_current: float = -600
    end_current: float = 0
    min_soc_gain: float = 10
    recent_flash_hours: float = 24
    charge_allowed_values: list = field(default_factory=lambda: [1])
    alarm_normal_values: list = field(default_factory=lambda: [0])
    alarm_active_values: list = field(default_factory=lambda: [1])
    columns: dict = field(default_factory=lambda: {
        "timestamp": "message_time", "vin": "vin", "error": "AFE_error",
        "current": "current_neg", "soc": "SOC", "allow": "chargeAllow",
        "temp": "Atemp_max", "temp_min": "Atemp_min", "speed": "speed",
        "software": "software_version_gb", **ALARMS,
    })


def load_config(path=None):
    cfg = Config()
    if path:
        values = json.loads(Path(path).read_text(encoding="utf-8"))
        for key, value in values.items():
            if key not in asdict(cfg):
                raise ValueError(f"未知配置项: {key}")
            if key == "columns":
                cfg.columns.update(value)
            else:
                setattr(cfg, key, value)
    if min(cfg.chunksize, cfg.window_seconds, cfg.max_gap_seconds,
           cfg.start_frames, cfg.end_frames) <= 0:
        raise ValueError("帧数、窗口、分块大小和断档阈值必须大于 0")
    if set(cfg.alarm_active_values) & set(cfg.alarm_normal_values):
        raise ValueError("报警与正常编码不能重叠")
    return cfg


def new_output(path):
    path = Path(path)
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"请使用新的或空的输出目录: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


def stage_csv(path, db, cfg):
    """SQLite 仅作一辆车的临时磁盘排序，支持任意程度乱序。"""
    header = pd.read_csv(path, nrows=0, encoding=cfg.encoding).columns
    missing = [v for v in cfg.columns.values() if v not in header]
    required = [cfg.columns[k] for k in ("timestamp", "vin", "error")]
    if any(c not in header for c in required):
        raise ValueError(f"{path.name}: 必须包含 {required}")
    quality = {"source": str(path.resolve()), "rows": 0, "invalid_time": 0,
               "out_of_order": 0, "missing_columns": ";".join(missing)}
    vins, previous_time = set(), None
    rename = {v: k for k, v in cfg.columns.items()}
    reader = pd.read_csv(path, usecols=lambda c: c in rename, dtype=str,
                         encoding=cfg.encoding, chunksize=cfg.chunksize)
    for chunk in reader:
        chunk = chunk.rename(columns=rename)
        quality["rows"] += len(chunk)
        vin = chunk["vin"].str.strip()
        if vin.isna().any() or vin.eq("").any():
            raise ValueError(f"{path.name}: VIN 缺失")
        vins.update(vin.unique())
        if len(vins) != 1:
            raise ValueError(f"{path.name}: 一个 CSV 必须只含一个 VIN")
        raw_time = chunk["timestamp"]
        if cfg.timestamp_unit:
            timestamp = pd.to_datetime(pd.to_numeric(raw_time, errors="coerce"),
                                       unit=cfg.timestamp_unit, errors="coerce", utc=True)
        else:
            timestamp = pd.to_datetime(raw_time, format="mixed", errors="coerce", utc=True)
        quality["invalid_time"] += int(timestamp.isna().sum())
        chunk = chunk.loc[timestamp.notna()].copy()
        times = timestamp[timestamp.notna()].astype("int64") / 1e9
        if len(times):
            quality["out_of_order"] += int((times.diff() < 0).sum())
            quality["out_of_order"] += int(previous_time is not None and times.iloc[0] < previous_time)
            previous_time = times.iloc[-1]
        chunk["timestamp"] = times
        for col in cfg.columns:
            if col not in chunk:
                chunk[col] = np.nan
            if col not in ("timestamp", "vin", "software"):
                chunk[col] = pd.to_numeric(chunk[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
        chunk["software"] = chunk["software"].fillna("").astype(str).str.strip()
        chunk[list(cfg.columns)].to_sql("raw", db, if_exists="append", index=False)
    if not vins:
        raise ValueError(f"空文件: {path}")
    db.execute('CREATE INDEX time_index ON raw(timestamp)')
    quality["vin"] = next(iter(vins))
    return quality


def sorted_chunks(db, cfg):
    last = None
    # 同时间戳保留原文件第一条，结果不受 chunksize 影响。
    for chunk in pd.read_sql_query("SELECT * FROM raw ORDER BY timestamp, rowid", db,
                                   chunksize=cfg.chunksize):
        chunk = chunk.drop_duplicates("timestamp", keep="first")
        if last is not None:
            chunk = chunk[chunk.timestamp > last]
        if len(chunk):
            last = chunk.timestamp.iloc[-1]
            for col in cfg.columns:
                if col not in ("timestamp", "vin", "software"):
                    chunk[col] = pd.to_numeric(chunk[col], errors="coerce")
            yield chunk.reset_index(drop=True)


class FlashDetector:
    """配置驱动的充电状态机；只承认观测到开始、结束的完整事件。"""
    def __init__(self, cfg):
        self.cfg = cfg
        self.previous = None
        self.pending = []
        self.active = None
        self.ending = []
        self.events = []
        self.incomplete = 0

    def feed(self, t, current, soc, allow):
        c = self.cfg
        previous = self.previous
        gap = previous is None or t - previous[0] > c.max_gap_seconds
        if gap or not np.isfinite(current):
            self.incomplete += int(self.active is not None)
            self.pending, self.active, self.ending = [], None, []
        if np.isfinite(current):
            if self.active is None:
                if allow in c.charge_allowed_values and current < c.start_current:
                    if not self.pending:
                        # 必须看到开始前的非充电帧，避免将文件/断档中途当作开始。
                        if previous is None or gap or not np.isfinite(previous[1]) or (
                            previous[1] < c.start_current and previous[3] in c.charge_allowed_values
                        ):
                            self.previous = (t, current, soc, allow)
                            return
                        self.pending = [(t, current, previous[2])]
                    else:
                        self.pending.append((t, current, soc))
                    if len(self.pending) >= c.start_frames:
                        self.active = {"start": self.pending[0][0],
                                       "soc_before": self.pending[0][2],
                                       "min_current": min(r[1] for r in self.pending)}
                        self.pending = []
                else:
                    self.pending = []
            else:
                self.active["min_current"] = min(self.active["min_current"], current)
                self.ending = self.ending + [(t, soc)] if current >= c.end_current else []
                if len(self.ending) >= c.end_frames:
                    event = {**self.active, "end": self.ending[0][0],
                             "confirmed_at": t, "soc_after": self.ending[0][1]}
                    event["soc_gain"] = event["soc_after"] - event["soc_before"]
                    event["is_flash"] = bool(event["min_current"] < c.peak_current
                                             and event["soc_gain"] > c.min_soc_gain)
                    self.events.append(event)
                    self.active, self.ending = None, []
        self.previous = (t, current, soc, allow)


def scan_events(db, cfg, quality):
    detector = FlashDetector(cfg)
    versions, count = [], 0
    last_version = None
    for chunk in sorted_chunks(db, cfg):
        count += len(chunk)
        for row in chunk[["timestamp", "current", "soc", "allow", "software"]].itertuples(index=False):
            detector.feed(*row[:4])
            version = row.software or "unknown"
            if version != last_version:
                versions.append({"timestamp": row.timestamp, "last_seen": row.timestamp, "software": version})
                last_version = version
            else:
                versions[-1]["last_seen"] = row.timestamp
    quality["duplicates"] = quality["rows"] - quality["invalid_time"] - count
    quality["incomplete_charges"] = detector.incomplete + int(detector.active is not None)
    quality["valid_rows"] = count
    return pd.DataFrame(detector.events, columns=["start", "end", "confirmed_at", "soc_before",
                        "soc_after", "soc_gain", "min_current", "is_flash"]), pd.DataFrame(versions)


def flash_features(times, events, cfg, available):
    if not available:
        return {k: np.full(len(times), np.nan) for k in ("during_flash", "recent_flash", "prior_flash_count")}
    flash = events.loc[events.is_flash.eq(True)]
    starts, ends = flash.start.to_numpy(float), flash.end.to_numpy(float)
    confirmed = flash.confirmed_at.to_numpy(float)
    before = np.searchsorted(confirmed, times, side="left")
    during = np.searchsorted(starts, times, side="right") > np.searchsorted(ends, times, side="right")
    last_end = np.full(len(times), -np.inf)
    if len(ends):
        valid = before > 0
        last_end[valid] = ends[before[valid] - 1]
    return {"during_flash": during.astype(float), "prior_flash_count": before.astype(float),
            "recent_flash": ((times - last_end) <= cfg.recent_flash_hours * 3600).astype(float)}


def aggregate_windows(db, cfg, events, quality):
    pieces, tail, first = [], None, None
    quality.update(gaps=0, counter_decreases=0, invalid_counter_rows=0)
    for chunk in sorted_chunks(db, cfg):
        if first is None:
            first = chunk.timestamp.iloc[0]
        rows = pd.concat([tail, chunk], ignore_index=True) if tail is not None else chunk
        tail = rows.iloc[[-1]].copy()
        prev, now = rows.iloc[:-1].reset_index(drop=True), rows.iloc[1:].reset_index(drop=True)
        if now.empty:
            continue
        dt = now.timestamp - prev.timestamp
        valid = dt.between(0, cfg.max_gap_seconds, inclusive="right")
        quality["gaps"] += int((dt > cfg.max_gap_seconds).sum())
        difference = now.error - prev.error
        quality["counter_decreases"] += int((difference < 0).sum())
        counter_now = now.error.ge(0) & now.error.mod(1).eq(0)
        counter_prev = prev.error.ge(0) & prev.error.mod(1).eq(0)
        quality["invalid_counter_rows"] += int((~counter_now).sum())
        # 下降区间不计误码也不计该指标暴露；清零后的下一段可重新计算。
        error_ok = valid & counter_now & counter_prev & difference.ge(0)
        frame = pd.DataFrame({"window": np.floor(prev.timestamp / cfg.window_seconds) * cfg.window_seconds,
                              "span_start": prev.timestamp, "span_end": now.timestamp,
                              "observed_seconds": dt.where(valid, 0),
                              "error_count": difference.where(error_ok, 0),
                              "error_seconds": dt.where(error_ok, 0)})
        frame["software"] = prev.software.where(prev.software.eq(now.software) & prev.software.ne(""), "unknown")
        features = {"temp": prev.temp, "temp_spread": prev.temp - prev.temp_min,
                    "speed": prev.speed, "current": prev.current, "soc": prev.soc,
                    "observation_days": (prev.timestamp - first) / 86400}
        features.update(flash_features(prev.timestamp.to_numpy(), events, cfg,
                        all(cfg.columns[k] not in quality["missing_columns"].split(";")
                            for k in ("allow", "current", "soc"))))
        for key, value in features.items():
            value = pd.Series(value, index=frame.index)
            frame[key + "_sum"] = (value * dt).where(valid & value.notna(), 0)
            frame[key + "_seconds"] = dt.where(valid & value.notna(), 0)
        for key in ALARMS:
            known_prev = prev[key].isin(cfg.alarm_normal_values + cfg.alarm_active_values)
            known_now = now[key].isin(cfg.alarm_normal_values + cfg.alarm_active_values)
            known = valid & known_prev & known_now
            frame[key + "_onsets"] = (known & prev[key].isin(cfg.alarm_normal_values)
                                         & now[key].isin(cfg.alarm_active_values)).astype(int)
            frame[key + "_seconds"] = dt.where(known, 0)
            frame[key + "_active_seconds"] = dt.where(known & prev[key].isin(cfg.alarm_active_values), 0)
        frame = frame.loc[valid]
        if len(frame):
            aggregation = {col: "sum" for col in frame if col not in ("window", "span_start", "span_end", "software")}
            aggregation.update(span_start="min", span_end="max")
            pieces.append(frame.groupby(["window", "software"], dropna=False).agg(aggregation).reset_index())
    if not pieces:
        raise ValueError(f"{quality['vin']}: 无有效相邻记录")
    # 这里只合并单 VIN 的压缩窗口表，原始秒级数据始终逐块处理。
    compact = pd.concat(pieces, ignore_index=True)
    result = compact.groupby(["window", "software"], dropna=False).agg(aggregation).reset_index()
    for name in features:
        result[name] = result[name + "_sum"] / result[name + "_seconds"].replace(0, np.nan)
        result = result.drop(columns=name + "_sum")
    result["vin"] = quality["vin"]
    result["window_seconds"] = cfg.window_seconds
    return result


def prepare(input_dir, output_dir, cfg):
    files = sorted(Path(input_dir).glob("*.csv"))
    if not files:
        raise ValueError(f"没有 CSV: {input_dir}")
    out = new_output(output_dir)
    (out / "windows").mkdir()
    (out / "config.json").write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=2), encoding="utf-8")
    qualities, all_events, all_versions, seen = [], [], [], set()
    for index, path in enumerate(files):
        print(f"[{index + 1}/{len(files)}] {path.name}", flush=True)
        with tempfile.TemporaryDirectory(prefix="afe_sort_") as temp:
            with sqlite3.connect(str(Path(temp) / "sort.sqlite")) as db:
                db.execute("PRAGMA temp_store=FILE")
                quality = stage_csv(path, db, cfg)
                if quality["vin"] in seen:
                    raise ValueError("同一 VIN 出现在多个 CSV；当前约定是一车一文件")
                seen.add(quality["vin"])
                events, versions = scan_events(db, cfg, quality)
                windows = aggregate_windows(db, cfg, events, quality)
                windows.to_parquet(out / "windows" / f"{index:06d}.parquet", index=False)
                all_events.append(events.assign(vin=quality["vin"]))
                all_versions.append(versions.assign(vin=quality["vin"]))
                qualities.append(quality)
    pd.DataFrame(qualities).to_csv(out / "quality.csv", index=False)
    pd.concat(all_events, ignore_index=True).to_csv(out / "charging_events.csv", index=False)
    pd.concat(all_versions, ignore_index=True).to_csv(out / "software_transitions.csv", index=False)
    (out / "COMPLETE").write_text("ok\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_dir", help="每个 CSV 一个 VIN 的目录")
    parser.add_argument("output_dir", help="新的缓存目录")
    parser.add_argument("--config", help="JSON 配置；未指定项使用 Config 默认值")
    args = parser.parse_args()
    prepare(args.input_dir, args.output_dir, load_config(args.config))
