"""计算单个因子并自动归档实验证据，不执行因子评价。

调用示例
--------
在项目根目录运行：

    python scripts/run_factor_experiment.py example_mom_20 --start 20200101 --end 20241231

传入因子参数：

    python scripts/run_factor_experiment.py example_mom_20 --start 20200101 --end 20241231 --param lookback=60

参数说明
--------
factor_id: 因子 ID，对应 research/factors/<factor_id>/compute.py，不是文件路径。
--start/--end: 输出与后续评价的日期闭区间。
--param key=value: 传入 compute() 的 params；只有 compute() 主动读取才会影响计算。

输出与后续评价
--------------
计算成功后输出实验目录、行数和 next 命令。本脚本保存 factor_values.parquet，
并生成实验记录、manifest、代码和配置快照。将 Parquet 路径交给 evaluate_factor.py
即可继续评价。
"""
import argparse
import json
import logging

import yaml

import _bootstrap  # noqa: F401
from core.factor.experiment import run_experiment


def main():
    """解析命令行参数，调用统一实验函数并打印下一步评价命令。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("factor_id")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--input-end")
    parser.add_argument("--param", action="append", default=[], help="key=value; value is parsed as YAML")
    parser.add_argument("--note")
    parser.add_argument("--profile")
    args = parser.parse_args()
    params = {}
    for item in args.param:
        if "=" not in item:
            parser.error("--param must be key=value")
        key, value = item.split("=", 1)
        params[key] = yaml.safe_load(value)
    result = run_experiment(args.factor_id, start=args.start, end=args.end,
                            input_end=args.input_end, params=params, note=args.note,
                            profile=args.profile)
    print(json.dumps({"experiment_id": result["experiment"]["experiment_id"],
                      "root": str(result["root"]), "rows": result["experiment"]["rows"],
                      "next": f"python scripts/evaluate_factor.py {result['root'] / 'factor_values.parquet'}"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
