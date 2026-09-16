import tushare as ts

import os

pro = ts.pro_api('22620aa015426c177e6fbbd2b5a7b53fbc19daffb2a423e2893e94f1')

# 下载 2026年9月1日至9月11日 期间所有交易日的涨跌停价格
df = pro.stk_limit(start_date='20180601', end_date='20260912')

print(df.head())

# 保存到本地
df.to_parquet("stk_limit_20260911.parquet", index=False)

print(f"共下载 {len(df)} 条记录")
