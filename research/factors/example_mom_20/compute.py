"""新因子计算模板：过去 N 个交易日的动量，默认 N=20。

运行示例
--------
在项目根目录执行默认的 20 日动量实验：

    python scripts/run_factor_experiment.py example_mom_20 --start 20200101 --end 20241231

通过 params 改用 60 日窗口：

    python scripts/run_factor_experiment.py example_mom_20 --start 20200101 --end 20241231 --param lookback=60

计算成功后，按终端输出的 next 命令评价，例如：

    python scripts/evaluate_factor.py factor_research/experiments/example_mom_20/<experiment_id>/factor_values.parquet

SPEC 说明
---------
尚未进入正式注册表的新因子必须在源码中提供完整 SPEC。它是程序读取的 Python 字典，
用于实验校验、评价、报告和入库。factor_id 必须与源码目录名一致。

SPEC 中的 formula 和 pit 是说明文本，不会被当成计算代码执行；真正的公式由 compute()
实现。修改公式时，应同步更新 compute()、formula、版本和相关 PIT 说明。
"""
from core.factor.compute import momentum_factor


SPEC = {
    "factor_id": "example_mom_20",
    "version": 1,
    "category": "momentum",
    "inputs": {"table": "daily_adjusted_price", "fields": ["adjusted_close"]},
    "formula": "adjusted_close(T) / adjusted_close(T-lookback) - 1; default lookback=20",
    "lookback": 20,
    "min_periods": "exact_endpoints",
    "expected_direction": 1,  # 1=预期正向，-1=预期反向，0=暂不预设方向。
    "unit": "ratio",
    "valid_from": "20180601",
    "pit": "Only adjusted_close observed at the two exact trading-day endpoints is used.",
}


def compute(store, start=None, end=None, input_end=None, spec=None, params=None):
    """计算因子并返回 DuckDB relation/临时视图名称。

    参数：
        store: 已连接正式上游产物的 Store，不直接读取 raw 表。
        start, end: 输出日期闭区间，继续传给底层计算函数。
        input_end: 截断重算的数据截止日期，不能忽略，否则可能引入未来数据。
        spec: 当前模块的 SPEC，或已入库因子的注册信息。
        params: 命令行 --param key=value 形成的字典；只有主动读取才会影响计算。

    返回：
        可供 DuckDB 查询的 relation/临时视图名称，而非 Parquet 文件路径。
        查询结果至少包含 trade_date、ts_code、factor_id、value 四列。

    保存由统一实验入口完成：补充 stock_name、校验数据、写出 Parquet，并生成
    experiment.yaml、manifest.json、代码快照和配置快照。本函数不导入收益标签。
    """
    options = params or {}
    lookback = int(options.get("lookback", SPEC["lookback"]))
    if lookback < 1:
        raise ValueError("lookback must be a positive integer")
    return momentum_factor(store, SPEC["factor_id"], lookback=lookback,
                           start=start, end=end, input_end=input_end)
