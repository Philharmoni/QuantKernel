#!/usr/bin/env python3
"""补充下载 Stage 1 缺口数据：交易日历、namechange、stk_limit。

背景
----
Stage 1 因原始数据表缺失而挂起的能力（见 STAGE1_NEXT_IMPROVEMENTS.md 前 3 点）：

* 历史 ST / 名称变更 / 退市整理期：官方 `stock_st`、`st`、`bak_basic` 三个接口
  该账号无权限（错误码 40203），改用 `namechange`。`namechange` 提供带
  start_date/end_date 的完整名称区间，`change_reason` 内含 ST/*ST/退市整理期，
  可据此重建逐日 ST 状态。注意：这是**派生区间**，不等同于官方每日 ST 列表。
* 逐股逐日涨跌停：`stk_limit` 提供 `up_limit` / `down_limit`，且覆盖 ST 股票
  （原 `limit_list_d` 官方明确不统计 ST）。

为什么 namechange 默认拉全历史
------------------------------
名称区间对每只股票是**首尾相接**的（实测相邻段 end_date+1天 == 下段 start_date）。
若只按公告日 >= 2018-01-01 过滤，则所有"2018 年之前改名、且该名称延续到 2018 年之后"
的段会整段丢失——包括 2018 年仍处于 ST 的股票，会导致重建出的 ST 状态错误。
因此必须下载跨 2018-01-01 的全部历史区间。全历史实测仅 14,206 行、约 10 次调用。

目录约定（与 data/ 下既有表一致）
--------------------------------
* `namechange/`       按股票分文件  -> `namechange/by_code/<ts_code>.parquet`
* `stk_limit/`        按交易日分区  -> `stk_limit/daily/YYYYMMDD.parquet`
* `trade_cal/`        单文件        -> `trade_cal/trade_cal_{exchange}_{lo}_{hi}.parquet`

写入策略：全部 Parquet 使用 SNAPPY 压缩、单 row group、日期以 YYYYMMDD 字符串保存，
与既有文件保持一致。已存在的文件默认跳过（可续跑），不修改、不覆盖任何既有原始文件；
交易日历补头写入**新文件**，避免改动 `trade_cal_SSE.parquet`。

用法
----
    export TUSHARE_TOKEN=<你的token>
    python scripts/download_missing_data.py all
    python scripts/download_missing_data.py calendar
    python scripts/download_missing_data.py namechange
    python scripts/download_missing_data.py stk_limit --sleep 0.35

数据根目录默认取仓库下的 `data/`，可用 `--data-root` 覆盖。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = REPO_ROOT / 'data'
API_URL = 'https://api.tushare.pro'
COMPRESSION = 'snappy'
# 这些错误的出现说明是权限/参数问题，重试无意义，直接失败。
FATAL_MARKERS = ('没有接口', '没有访问', '权限', 'token', 'TOKEN')


class TushareError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(f'[{code}] {msg}')
        self.code = code
        self.msg = str(msg)
        self.fatal = any(marker in self.msg for marker in FATAL_MARKERS)


def api(token: str, api_name: str, **params):
    """单次调用 Tushare HTTP 接口，返回 (fields, items)。"""
    payload = json.dumps({'api_name': api_name, 'token': token,
                          'params': params, 'fields': ''}).encode()
    request = urllib.request.Request(
        API_URL, data=payload, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=90) as response:
        body = json.loads(response.read())
    if body.get('code') != 0:
        raise TushareError(body.get('code'), body.get('msg'))
    data = body.get('data') or {}
    return data.get('fields') or [], data.get('items') or []


def api_retry(token: str, api_name: str, attempts: int = 6, **params):
    """带指数退避的调用；权限类错误立即抛出，不做无意义重试。"""
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            return api(token, api_name, **params)
        except TushareError as exc:
            if exc.fatal or attempt == attempts:
                raise
            reason = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt == attempts:
                raise
            reason = exc
        print(f'      重试 {attempt}/{attempts - 1}（{delay:.0f}s 后）: {reason}', flush=True)
        time.sleep(delay)
        delay = min(delay * 2, 30)
    raise RuntimeError('unreachable')


def write_parquet(path: Path, fields, rows) -> None:
    """原子写入 Parquet，格式与 data/ 下既有文件一致。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [dict(zip(fields, row)) for row in rows]
    table = pa.Table.from_pylist(records)
    temporary = path.with_name(path.name + '.tmp')
    pq.write_table(table, temporary, compression=COMPRESSION)
    os.replace(temporary, path)


def trading_days(token: str, start: str, end: str):
    """按 SSE 日历取交易日列表（is_open=1），支持跨 10000 行上限的分块。"""
    days = []
    cursor = dt.datetime.strptime(start, '%Y%m%d').date()
    stop = dt.datetime.strptime(end, '%Y%m%d').date()
    while cursor <= stop:
        chunk_end = min(cursor + dt.timedelta(days=365 * 4), stop)
        fields, items = api_retry(token, 'trade_cal', exchange='SSE',
                                  start_date=cursor.strftime('%Y%m%d'),
                                  end_date=chunk_end.strftime('%Y%m%d'))
        index = {name: position for position, name in enumerate(fields)}
        days.extend(row[index['cal_date']] for row in items if row[index['is_open']] == 1)
        cursor = chunk_end + dt.timedelta(days=1)
        time.sleep(0.2)
    return sorted(set(days))


def step_calendar(token: str, start: str, end: str, force: bool) -> None:
    """补全交易日历缺失的头部。既有文件不动，只写区间不重叠的新文件。"""
    print('[calendar] 补全交易日历头部', flush=True)
    for exchange in ('SSE', 'CFFEX'):
        existing = DATA_ROOT / 'trade_cal' / f'trade_cal_{exchange}.parquet'
        head_lo, head_hi = start, end
        if existing.exists():
            column = pq.read_table(existing, columns=['cal_date']).column('cal_date').to_pylist()
            if column:
                earliest = min(column)
                if earliest <= start:
                    print(f'  {exchange}: 既有文件已覆盖到 {earliest}，无需补全', flush=True)
                    continue
                head_hi = (dt.datetime.strptime(earliest, '%Y%m%d').date()
                           - dt.timedelta(days=1)).strftime('%Y%m%d')
        if head_lo > head_hi:
            print(f'  {exchange}: 区间为空，跳过', flush=True)
            continue
        target = DATA_ROOT / 'trade_cal' / f'trade_cal_{exchange}_{head_lo}_{head_hi}.parquet'
        if target.exists() and not force:
            print(f'  {exchange}: {target.name} 已存在，跳过', flush=True)
            continue
        fields, items = api_retry(token, 'trade_cal', exchange=exchange,
                                  start_date=head_lo, end_date=head_hi)
        if not items:
            print(f'  {exchange}: 接口无返回，跳过', flush=True)
            continue
        write_parquet(target, fields, items)
        opened = sum(1 for row in items if row[fields.index('is_open')] == 1)
        print(f'  {exchange}: {target.name}  行数={len(items)} 交易日={opened} '
              f'区间={head_lo}~{head_hi}', flush=True)


def step_namechange(token: str, start: str, end: str, force: bool) -> None:
    """下载 namechange 并按股票拆分保存。"""
    print(f'[namechange] 下载名称变更历史 {start}~{end}', flush=True)
    rows, seen = [], set()
    cursor = dt.datetime.strptime(start, '%Y%m%d').date()
    stop = dt.datetime.strptime(end, '%Y%m%d').date()
    fields = None
    while cursor <= stop:
        chunk_end = min(cursor + dt.timedelta(days=365 * 3), stop)
        fields, items = api_retry(token, 'namechange',
                                  start_date=cursor.strftime('%Y%m%d'),
                                  end_date=chunk_end.strftime('%Y%m%d'))
        added = 0
        for row in items:
            key = tuple(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
                added += 1
        print(f'  {cursor}~{chunk_end}: 返回 {len(items)} 行，新增 {added} 行（累计 {len(rows)}）',
              flush=True)
        cursor = chunk_end + dt.timedelta(days=1)
        time.sleep(0.3)
    if not rows:
        print('  无数据，结束', flush=True)
        return

    code_index = fields.index('ts_code')
    grouped = {}
    for row in rows:
        grouped.setdefault(row[code_index], []).append(row)
    base = DATA_ROOT / 'namechange' / 'by_code'
    base.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    for code, code_rows in sorted(grouped.items()):
        target = base / f'{code}.parquet'
        if target.exists() and not force:
            skipped += 1
            continue
        write_parquet(target, fields, code_rows)
        written += 1
    print(f'  写入 {written} 个文件，跳过已存在 {skipped} 个，目录 {base}', flush=True)

    reasons = {}
    for row in rows:
        reasons[row[fields.index('change_reason')]] = reasons.get(
            row[fields.index('change_reason')], 0) + 1
    st_rows = sum(count for reason, count in reasons.items() if reason and 'ST' in reason)
    delist_rows = sum(count for reason, count in reasons.items()
                      if reason and '退市整理' in reason)
    print(f'  总行数={len(rows)} 唯一代码={len(grouped)} ST类={st_rows} 退市整理期={delist_rows}',
          flush=True)


def step_stk_limit(token: str, start: str, end: str, force: bool, sleep: float) -> None:
    """按交易日逐日下载 stk_limit 涨跌停价格。"""
    print(f'[stk_limit] 下载逐股逐日涨跌停 {start}~{end}', flush=True)
    days = trading_days(token, start, end)
    print(f'  待下载交易日 {len(days)} 个（{days[0]}~{days[-1]}）', flush=True)
    base = DATA_ROOT / 'stk_limit' / 'daily'
    base.mkdir(parents=True, exist_ok=True)

    written = skipped = empty = total_rows = 0
    started = time.time()
    for position, day in enumerate(days, start=1):
        target = base / f'{day}.parquet'
        if target.exists() and not force:
            skipped += 1
            continue
        fields, items = api_retry(token, 'stk_limit', trade_date=day)
        if not items:
            empty += 1
            print(f'  [{position}/{len(days)}] {day}: 接口无返回，未写入', flush=True)
        else:
            write_parquet(target, fields, items)
            written += 1
            total_rows += len(items)
            if position % 50 == 0 or position == len(days):
                elapsed = time.time() - started
                print(f'  [{position}/{len(days)}] {day}: {len(items)} 行；'
                      f'累计文件={written} 行数={total_rows} 耗时={elapsed:.0f}s', flush=True)
        time.sleep(sleep)
    print(f'  完成：新增文件 {written}，跳过已存在 {skipped}，无数据 {empty}，'
          f'总行数 {total_rows}，目录 {base}', flush=True)


def main() -> int:
    global DATA_ROOT
    parser = argparse.ArgumentParser(description='补充下载 Stage 1 缺口数据')
    parser.add_argument('steps', nargs='+',
                        choices=['calendar', 'namechange', 'stk_limit', 'all'])
    parser.add_argument('--token', default=os.environ.get('TUSHARE_TOKEN', ''),
                        help='Tushare token；默认读取环境变量 TUSHARE_TOKEN')
    parser.add_argument('--start', default='20180101', help='下载起始日（默认 20180101）')
    parser.add_argument('--end', default=dt.date.today().strftime('%Y%m%d'),
                        help='下载结束日（默认今天）')
    parser.add_argument('--namechange-start', default='19910101',
                        help='namechange 起始日；默认全历史，理由见文件头说明')
    parser.add_argument('--sleep', type=float, default=0.35,
                        help='stk_limit 每次调用后的间隔秒数（默认 0.35，约 170 次/分钟）')
    parser.add_argument('--force', action='store_true', help='覆盖已存在的目标文件')
    parser.add_argument('--data-root', default=str(DATA_ROOT),
                        help=f'数据根目录（默认 {DATA_ROOT}）')
    args = parser.parse_args()
    DATA_ROOT = Path(args.data_root).resolve()

    if not args.token:
        print('错误：未提供 token。请设置环境变量 TUSHARE_TOKEN，或使用 --token 传入。',
              file=sys.stderr)
        return 2

    steps = ['calendar', 'namechange', 'stk_limit'] if 'all' in args.steps else args.steps
    print(f'数据根目录: {DATA_ROOT}')
    print(f'执行步骤: {steps}  区间: {args.start}~{args.end}', flush=True)
    for step in steps:
        if step == 'calendar':
            step_calendar(args.token, args.start, args.end, args.force)
        elif step == 'namechange':
            step_namechange(args.token, args.namechange_start, args.end, args.force)
        elif step == 'stk_limit':
            step_stk_limit(args.token, args.start, args.end, args.force, args.sleep)
    print('全部完成。', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
