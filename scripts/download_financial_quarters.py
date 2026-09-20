#!/usr/bin/env python3
"""补充下载财务缺失季度（STAGE1_NEXT_IMPROVEMENTS.md 改进项 4）。

背景
----
本地三张 VIP 财务（income_vip / balancesheet_vip / cashflow_vip）按报告期分区，
实际覆盖 20180630–20260630，存在两类缺口：

* 头部缺 20170930 / 20171231 / 20180331 三个报告期（首期 20180630）。
* 尾部 20260331 每表仅约 1100 行、20260630 每表仅 1 行，远非全市场。

当前账号对三个 VIP 接口无权限（40203），但非 VIP 的 income / cashflow /
balancesheet 可用：字段是 VIP 的子集（85/94 列，缺 9 个扩展列），且必须
按 ts_code 逐股查询。本脚本据此逐股拉取并过滤到目标报告期。

关键约定（与既有数据共存的前提）
--------------------------------
1. 只保留 report_type='1'（本地 VIP 数据只含该类型）。
2. 只取五个目标报告期：20170930 / 20171231 / 20180331（头部三期，其中
   20171231 / 20180331 为 TTM 前史所需）与 20260331 / 20260630（尾部补充）。
   公告窗口固定为 20170901 起，因此取得的都是该窗口内公告的原始报表及更正版本；
   其余报告期一行都不取，避免与非 VIP 行哈希不同的版本与本地 VIP 记录
   形成同键冲突。更早报告期（20170630 及之前）即便存在窗口内公告的更正
   也按超出补数范围丢弃。
3. 尾部两期只写"本地原文件中不存在的股票"，原文件保持只读不动。
4. update_flag 0/1 两个版本都保留（与本地 VIP 数据一致），同键不同值时由
   处理层按 null_with_ambiguous 约定处理，不在本脚本择一。
5. 写入数值按本地 VIP parquet 的列类型显式转换（double/字符串），保证
   union_by_name 合并时类型一致。

日历边界说明：20170930 的原始公告（2017-10）早于本地日历起点 2018-01-01，
这些记录在处理层会被标为 calendar_before_coverage（保留但不可见）；2017-09
之后公告的更正版本可以正常进入可见性映射。

用法
----
    export TUSHARE_TOKEN=<你的token>
    python scripts/download_financial_quarters.py            # 全量（可续跑）
    python scripts/download_financial_quarters.py --limit 20 # 试跑前 20 只
    python scripts/download_financial_quarters.py --finalize-only

暂存与断点：data_middle/_tmp/financial_backfill/（可随时中断重跑）。
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

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = REPO_ROOT / 'data'
STAGING = REPO_ROOT / 'data_middle' / '_tmp' / 'financial_backfill'
API_URL = 'https://api.tushare.pro'
COMPRESSION = 'snappy'

ANN_START = '20170901'
HEAD_PERIODS = ('20170930', '20171231', '20180331')
TAIL_PERIODS = ('20260331', '20260630')
TARGET_PERIODS = set(HEAD_PERIODS) | set(TAIL_PERIODS)
TABLES = {'income': 'income_vip', 'cashflow': 'cashflow_vip', 'balancesheet': 'balancesheet_vip'}
REQUIRED_FIELDS = {
    'income_vip': ['ts_code', 'end_date', 'report_type', 'ann_date', 'f_ann_date',
                   'update_flag', 'revenue', 'n_income_attr_p'],
    'cashflow_vip': ['ts_code', 'end_date', 'report_type', 'ann_date', 'f_ann_date',
                     'update_flag', 'n_cashflow_act'],
    'balancesheet_vip': ['ts_code', 'end_date', 'report_type', 'ann_date', 'f_ann_date',
                         'update_flag', 'total_assets'],
}
FATAL_MARKERS = ('没有接口', '没有访问', '权限', 'token')
RATE_MARKER = '每分钟'
FLUSH_EVERY = 50


class TushareError(RuntimeError):
    def __init__(self, code, msg):
        super().__init__(f'[{code}] {msg}')
        self.msg = str(msg)
        self.fatal = any(marker in self.msg for marker in FATAL_MARKERS)
        self.rate = RATE_MARKER in self.msg


def api(token: str, api_name: str, **params):
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


def api_retry(token: str, api_name: str, attempts: int = 5, **params):
    delay, rate_wait = 2.0, 65.0
    for attempt in range(1, attempts + 1):
        try:
            return api(token, api_name, **params)
        except TushareError as exc:
            if exc.fatal or attempt == attempts:
                raise
            wait = rate_wait if exc.rate else delay
            print(f'      {api_name} 重试 {attempt}/{attempts - 1}（等 {wait:.0f}s）: {exc}',
                  flush=True)
            time.sleep(wait)
            delay = min(delay * 2, 30)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if attempt == attempts:
                raise
            print(f'      {api_name} 网络重试 {attempt}/{attempts - 1}（等 {delay:.0f}s）: {exc}',
                  flush=True)
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError('unreachable')


def target_codes() -> list:
    """stock_basic 与既有财务源代码的并集（含旧退市/北交所历史代码）。"""
    db = duckdb.connect()
    codes = set()
    sources = ["read_parquet('data/stock_basic/stock_basic.parquet')",
               "read_parquet('data/income_vip/by_period/*.parquet')",
               "read_parquet('data/balancesheet_vip/by_period/*.parquet')",
               "read_parquet('data/cashflow_vip/by_period/*.parquet')"]
    for source in sources:
        codes.update(row[0] for row in db.execute(
            f'select distinct ts_code from {source} where ts_code is not null').fetchall())
    return sorted(codes)


def local_schema(table: str):
    """用本地既有分区的列类型作为写入转换依据。"""
    samples = sorted((DATA_ROOT / table / 'by_period').glob('20*.parquet'))
    schema = pq.read_schema(samples[0])
    return {name: str(schema.field(name).type) for name in schema.names}


def cast_records(records, schema):
    for record in records:
        for field, kind in schema.items():
            value = record.get(field)
            if value is None:
                continue
            if kind == 'double':
                record[field] = float(value)
            elif kind.startswith(('string', 'large_string')):
                record[field] = str(value)
    return records


def tail_existing_codes(table: str, period: str):
    path = DATA_ROOT / table / 'by_period' / f'{period}.parquet'
    if not path.exists():
        return set()
    db = duckdb.connect()
    return set(row[0] for row in db.execute(
        f"select distinct ts_code from read_parquet('{path.as_posix()}')").fetchall())


def load_checkpoint():
    path = STAGING / 'checkpoint.json'
    if path.exists():
        return set(json.loads(path.read_text(encoding='utf-8'))['completed'])
    return set()


def save_checkpoint(completed):
    STAGING.mkdir(parents=True, exist_ok=True)
    temporary = STAGING / 'checkpoint.json.tmp'
    temporary.write_text(json.dumps({'completed': sorted(completed)}, ensure_ascii=False),
                         encoding='utf-8')
    os.replace(temporary, STAGING / 'checkpoint.json')


def flush(buffers, counters):
    """先落盘数据，再由调用方写断点；顺序保证中断时不丢行。"""
    for table, rows in buffers.items():
        if not rows:
            continue
        directory = STAGING / table
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'batch_{counters[table]:04d}.parquet'
        pq.write_table(pa.Table.from_pylist(rows), path, compression=COMPRESSION)
        counters[table] += 1
        rows.clear()


def read_staging(table: str):
    rows = []
    for path in sorted((STAGING / table).glob('batch_*.parquet')):
        rows.extend(pq.read_table(path).to_pylist())
    return rows


def finalize(force: bool) -> None:
    print('[finalize] 汇总暂存并写入目标分区', flush=True)
    for table in TABLES.values():
        rows = read_staging(table)
        if not rows:
            print(f'  {table}: 暂存为空，跳过', flush=True)
            continue
        schema = local_schema(table)
        groups = {}
        for row in rows:
            groups.setdefault(str(row['end_date']), []).append(row)
        for period in sorted(groups):
            records = groups[period]
            if period in HEAD_PERIODS:
                target = DATA_ROOT / table / 'by_period' / f'{period}.parquet'
            elif period in TAIL_PERIODS:
                target = DATA_ROOT / table / 'by_period' / f'{period}.supplement.parquet'
                existing = tail_existing_codes(table, period)
                before = len(records)
                records = [r for r in records if r['ts_code'] not in existing]
                print(f'  {table}/{period}: {before} 行中排除原文件已有股票 {before - len(records)} 行',
                      flush=True)
            else:
                print(f'  {table}/{period}: 非目标报告期，丢弃 {len(records)} 行', flush=True)
                continue
            if target.exists() and not force:
                print(f'  {table}: {target.name} 已存在，跳过（{len(records)} 行未写入）', flush=True)
                continue
            records = cast_records(records, schema)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.tmp')
            pq.write_table(pa.Table.from_pylist(records), temporary, compression=COMPRESSION)
            os.replace(temporary, target)
            codes = len({r['ts_code'] for r in records})
            print(f'  {table}: 写入 {target.name}  行数={len(records)} 代码数={codes}', flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='补充下载财务缺失季度')
    parser.add_argument('--token', default=os.environ.get('TUSHARE_TOKEN', ''),
                        help='Tushare token；默认读取环境变量 TUSHARE_TOKEN')
    parser.add_argument('--end', default=dt.date.today().strftime('%Y%m%d'),
                        help='公告窗口结束日（默认今天）')
    parser.add_argument('--sleep', type=float, default=0.12,
                        help='每次调用后的间隔秒数（默认 0.12）')
    parser.add_argument('--limit', type=int, default=0, help='只处理前 N 只股票（试跑用）')
    parser.add_argument('--force', action='store_true', help='覆盖已存在的目标文件')
    parser.add_argument('--finalize-only', action='store_true',
                        help='不做下载，只用暂存数据写目标文件')
    args = parser.parse_args()

    if not args.finalize_only and not args.token:
        print('错误：未提供 token。请设置环境变量 TUSHARE_TOKEN，或使用 --token 传入。',
              file=sys.stderr)
        return 2

    if not args.finalize_only:
        codes = target_codes()
        completed = load_checkpoint()
        pending = [code for code in codes if code not in completed]
        if args.limit:
            pending = pending[:args.limit]
        print(f'目标代码 {len(codes)} 只，已完成 {len(completed)}，本次待处理 {len(pending)}',
              flush=True)
        buffers = {table: [] for table in TABLES.values()}
        counters = {table: 0 for table in TABLES.values()}
        for table in TABLES.values():
            directory = STAGING / table
            if directory.exists():
                counters[table] = len(list(directory.glob('batch_*.parquet')))
        failed, started = [], time.time()
        for position, code in enumerate(pending, start=1):
            code_failed = False
            for api_name, table in TABLES.items():
                try:
                    fields, items = api_retry(args.token, api_name, ts_code=code,
                                              start_date=ANN_START, end_date=args.end)
                except Exception as exc:  # 单只失败不终止整体
                    print(f'  {code} {api_name}: 失败 {exc}', flush=True)
                    failed.append((code, api_name))
                    code_failed = True
                    continue
                index = {name: offset for offset, name in enumerate(fields)}
                missing = [f for f in REQUIRED_FIELDS[table] if f not in index]
                if missing:
                    print(f'  {code} {api_name}: 接口缺少字段 {missing}，中止', flush=True)
                    return 3
                kept = []
                for row in items:
                    if str(row[index['report_type']]) != '1':
                        continue
                    if str(row[index['end_date']]) not in TARGET_PERIODS:
                        continue
                    kept.append({name: row[offset] for name, offset in index.items()})
                if kept:
                    buffers[table].extend(kept)
            if not code_failed:
                completed.add(code)
            if position % FLUSH_EVERY == 0 or position == len(pending):
                flush(buffers, counters)
                save_checkpoint(completed)
                pending_rows = {t: len(buffers[t]) for t in buffers}
                elapsed = time.time() - started
                print(f'  [{position}/{len(pending)}] {code} 未落盘行={pending_rows} '
                      f'耗时={elapsed:.0f}s', flush=True)
            time.sleep(args.sleep)
        flush(buffers, counters)
        save_checkpoint(completed)
        if failed:
            print(f'失败 {len(failed)} 项（未记入断点，重跑会重试）:', flush=True)
            for item in failed[:20]:
                print(f'  {item}', flush=True)

    finalize(args.force)
    print('完成。', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
