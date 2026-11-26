# -*- coding: utf-8 -*-
"""ETF 映射模块（配置类）：把量化策略暴露（turn_low defensive 主力）映射到可交易 ETF。

流程：
  1) 从 bars.db 重算 turn_low defensive 组合日收益/净值（40 日调仓 top20 等权，T+1，无止损无择时，
     与因子池 daily_scores/turnover_rank 同口径：20 日均换手取反，2019+）。
  2) akshare 拉 ETF 候选池日线（前复权），缓存 data/cache/etf/{code}.csv（增量）。
  3) 相关性检验：策略 vs 每只 ETF（pearson 全期/年度、滚动 60 日相关、beta）+ ETF 间相关矩阵。
  4) ETF pitch 卡：匹配分(相关) + 稳定分(滚动相关) + 流动分(近20日成交额) → 综合分/档位。
  5) 配置建议：高分且互不冗余的 ETF 子集 → 等权/相关加权/相关×流动加权三方案，回测复制组合 vs 策略。
输出：deepseek-harness-quant/output/etf_map.json
"""
import os
import sys
import json
import sqlite3
import time
import datetime
import glob

import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # deepseek-harness-quant
OUT_PATH = os.path.join(BASE, 'output', 'etf_map.json')
BARS = r'data\cache\bars.db'
ETF_CACHE = r'data\cache\etf'
START = '20190101'   # turn_low 可验证区间 2019+
END = datetime.date.today().strftime('%Y%m%d')

# ---------------- ETF 候选池（配置类映射池：宽基/风格/行业/跨境/商品/债券） ----------------
ETF_POOL = [
    ('510300', '沪深300ETF', '宽基'), ('510500', '中证500ETF', '宽基'), ('512100', '中证1000ETF', '宽基'),
    ('510050', '上证50ETF', '宽基'), ('159915', '创业板ETF', '宽基'), ('588000', '科创50ETF', '宽基'),
    ('510180', '上证180ETF', '宽基'), ('159901', '深100ETF', '宽基'),
    ('510880', '红利ETF', '风格'), ('512890', '红利低波ETF', '风格'), ('159905', '深红利ETF', '风格'),
    ('515080', '中证红利ETF', '风格'), ('510330', '华夏300ETF', '宽基'), ('159949', '创业板50ETF', '宽基'),
    ('512330', '信息ETF', '行业'), ('512480', '半导体ETF', '行业'), ('515030', '新能源车ETF', '行业'),
    ('512690', '酒ETF', '行业'), ('159928', '消费ETF', '行业'), ('512010', '医药ETF', '行业'),
    ('512170', '医疗ETF', '行业'), ('512800', '银行ETF', '行业'), ('512200', '房地产ETF', '行业'),
    ('515220', '煤炭ETF', '行业'), ('512660', '军工ETF', '行业'), ('512720', '计算机ETF', '行业'),
    ('512880', '证券ETF', '行业'), ('515790', '光伏ETF', '行业'), ('512760', '芯片ETF', '行业'),
    ('159995', '芯片ETF华夏', '行业'), ('515000', '科技ETF', '行业'), ('516160', '新能源ETF', '行业'),
    ('512000', '券商ETF', '行业'), ('515880', '通信ETF', '行业'),
    ('513100', '纳指ETF', '跨境'), ('513500', '标普500ETF', '跨境'), ('513050', '中概互联ETF', '跨境'),
    ('159941', '纳指100ETF', '跨境'), ('513180', '恒生科技ETF', '跨境'),
    ('518880', '黄金ETF', '商品'), ('518800', '黄金ETF华夏', '商品'), ('511010', '国债ETF', '债券'),
    ('511260', '十年国债ETF', '债券'), ('511380', '可转债ETF', '债券'), ('510230', '金融ETF', '行业'),
]

REBAL_DAYS = 40
TOP_N = 20
MIN_PRICE = 1.5


def _load_pool():
    """★2026-08-16 动态化铁律：ETF 候选池 = config/etf_pool.yaml（存在则替代内置，增删 ETF 不改代码）。
    缺省回退内置 ETF_POOL（兜底不崩溃）。"""
    try:
        import yaml as _yaml
        cfg_path = os.path.join(BASE, "config", "etf_pool.yaml")
        if os.path.exists(cfg_path):
            cfg = _yaml.safe_load(open(cfg_path, encoding="utf-8")) or {}
            items = cfg.get("pool") or []
            if items:
                return [(str(x.get("code")), str(x.get("name") or x.get("code")),
                         str(x.get("category") or "其他")) for x in items if x.get("code")]
    except Exception as _e:
        print(f"[etf_map] etf_pool.yaml 加载失败（用内置池）: {_e}")
    return ETF_POOL


def load_turn_low_nav():
    """从 bars.db 重算 turn_low defensive 日收益/净值（与因子池 daily_scores 同口径）。"""
    con = sqlite3.connect(BARS)
    tdays = pd.read_sql(
        "SELECT DISTINCT date FROM daily_bar WHERE adjust='qfq' AND date>=%s ORDER BY date" % "'2019-01-01'", con
    )['date'].tolist()
    # 因子值：20 日均换手（turn 列，2019+ 覆盖 90%+），缺失剔除
    turn = pd.read_sql(
        "SELECT date, code, close, turn FROM daily_bar WHERE adjust='qfq' AND date>='2019-01-01' AND is_st=0", con
    )
    close = turn.pivot_table(index='date', columns='code', values='close', aggfunc='last')
    t = turn.pivot_table(index='date', columns='code', values='turn', aggfunc='last')
    con.close()
    t20 = t.rolling(20, min_periods=20).mean()          # factor_turnover = 20 日均换手
    rank = t20.rank(axis=1, pct=True)                   # 截面分位 0-1（rank 小 = 换手低 = 防守 = 好）
    ret = close.pct_change()

    dates = [d for d in tdays if d in rank.index]
    # 调仓日历：turn 覆盖稳定后（2019-04 起）每 40 交易日（2019 前换手率缺失，早期覆盖 60% 会污染选股）
    pick_days = [d for d in dates if d >= '2019-04-01'][::REBAL_DAYS]
    port_ret = []
    for i, pick in enumerate(pick_days):
        if pick not in rank.index:
            continue
        row = rank.loc[pick].dropna()
        price = close.loc[pick]
        valid = [c for c in row.index if c in price.index and not pd.isna(price.get(c)) and price.get(c) >= MIN_PRICE]
        top = row.loc[valid].nsmallest(TOP_N).index.tolist() if valid else []   # 低换手 top20
        if not top:
            continue
        hold_days = dates[dates.index(pick) + 1: dates.index(pick) + 1 + REBAL_DAYS]
        for d in hold_days:
            if d not in ret.index:
                continue
            r = ret.loc[d, [c for c in top if c in ret.columns]]
            r = r.dropna()
            if len(r):
                port_ret.append((d, float(r.mean())))
    s = pd.DataFrame(port_ret, columns=['date', 'ret']).set_index('date')
    s.index = pd.to_datetime(s.index)
    s['nav'] = (1 + s['ret']).cumprod()
    return s


def fetch_etf(code):
    """新浪源拉 ETF 日线（不复权），本地缓存增量（东财 fund_etf_hist_em 批量被封，改用新浪稳定）。"""
    path = os.path.join(ETF_CACHE, code + '.csv')
    os.makedirs(ETF_CACHE, exist_ok=True)
    import akshare as ak
    prefix = 'sh' if code[0] == '5' else 'sz'
    symbol = prefix + code
    for attempt in range(4):
        try:
            if os.path.exists(path):
                df = pd.read_csv(path)
                last = str(df['date'].max())[:10]
                new = ak.fund_etf_hist_sina(symbol=symbol)
                new['date'] = new['date'].astype(str).str[:10]
                new = new[new['date'] > last]
                if new is not None and len(new):
                    df = pd.concat([df, new]).drop_duplicates(subset=['date'], keep='last').sort_values('date')
                    df.to_csv(path, index=False)
                return df
            df = ak.fund_etf_hist_sina(symbol=symbol)
            df.to_csv(path, index=False)
            return df
        except Exception as e:
            if attempt < 3:
                time.sleep(1.2 * (attempt + 1))
            else:
                raise
        finally:
            time.sleep(0.35)   # 节流


def etf_ret_series(code):
    df = fetch_etf(code)
    if df is None or not len(df):
        return None
    df = df.copy()
    df['date'] = pd.to_datetime(df['date'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df['amount'] = pd.to_numeric(df['amount'], errors='coerce')   # 新浪 amount = 成交额(元)
    df = df.sort_values('date').dropna(subset=['close'])
    df['ret'] = df['close'].pct_change()
    df = df.set_index('date')
    return df[['ret', 'amount', 'close']]


def corr_stats(strat_ret, etf_ret):
    """对齐后：全期 pearson、滚动 60 日相关（均值/末值/波动）、beta。"""
    a = strat_ret.reindex(etf_ret.index)
    b = etf_ret
    m = pd.concat([a, b], axis=1, keys=['s', 'e']).dropna()
    if len(m) < 120:
        return None
    corr = float(m['s'].corr(m['e']))
    roll = m['s'].rolling(60).corr(m['e'])
    roll_drop = roll.dropna()
    beta = float(np.cov(m['s'], m['e'])[0, 1] / np.var(m['e'])) if np.var(m['e']) > 0 else 0.0
    yearly = {}
    m2 = m.copy()
    m2['year'] = m2.index.year
    for y, g in m2.groupby('year'):
        if len(g) > 60:
            yearly[str(y)] = round(float(g['s'].corr(g['e'])), 3)
    return {
        'corr': round(corr, 3),
        'corr_roll_mean': round(float(roll_drop.mean()), 3) if len(roll_drop) else None,
        'corr_roll_last': round(float(roll_drop.iloc[-1]), 3) if len(roll_drop) else None,
        'corr_roll_vol': round(float(roll_drop.std()), 3) if len(roll_drop) else None,
        'beta': round(beta, 3),
        'yearly': yearly,
        'n': len(m),
    }


def main():
    t0 = time.time()
    print('[1/5] 构建 turn_low defensive 策略净值（bars.db, 2019+, 40日 top20 T+1）...')
    strat = load_turn_low_nav()
    strat.to_csv(os.path.join(BASE, 'output', 'turn_low_nav.csv'))
    strat_ret = strat['ret']
    print('    策略天数 %d, 年化 %.2f%%, 回撤 %.2f%%' % (
        len(strat_ret),
        float(strat['nav'].iloc[-1]) ** (252 / len(strat_ret)) - 1,
        float((strat['nav'] / strat['nav'].cummax() - 1).min()) * 100,
    ))

    pool = _load_pool()
    print('[2/5] 拉取 ETF 日线（%d 只, akshare；来源 config/etf_pool.yaml 或内置兜底）...' % len(pool))
    etf_rets = {}
    fail = []
    for code, name, cat in pool:
        try:
            s = etf_ret_series(code)
            if s is not None and len(s) > 250:
                etf_rets[code] = s
                print('    %s %s ok (%d 行)' % (code, name, len(s)))
            else:
                fail.append(code)
                print('    %s %s 数据不足' % (code, name))
        except Exception as e:
            fail.append(code + ':' + str(e)[:60])
            print('    %s %s 失败: %s' % (code, name, str(e)[:80]))
    print('    失败: %s' % (fail if fail else '无'))

    print('[3/5] 相关性检验（策略 vs ETF + ETF 间矩阵）...')
    rows = []
    for code, name, cat in pool:
        if code not in etf_rets:
            continue
        st = corr_stats(strat_ret, etf_rets[code]['ret'])
        if st is None:
            continue
        amt = float(etf_rets[code]['amount'].tail(20).mean()) / 1e8   # 近20日日均成交额(亿)
        st.update({'code': code, 'name': name, 'category': cat, 'avg_amount_yi': round(amt, 2)})
        rows.append(st)
    corr_df = pd.DataFrame(rows).sort_values('corr', ascending=False)

    # ETF 间相关矩阵（去冗余用）
    top_codes = corr_df.head(14)['code'].tolist()
    mat = {}
    if len(top_codes) >= 2:
        etf_close = pd.DataFrame({c: etf_rets[c]['close'] for c in top_codes})
        r = etf_close.pct_change().corr()
        mat = {c1: {c2: round(float(r.loc[c1, c2]), 2) for c2 in r.columns if c1 != c2} for c1 in r.index}

    print('[4/5] ETF pitch 卡 + 去冗余...')
    cards = []
    for _, row in corr_df.iterrows():
        match = max(0.0, row['corr']) * 100
        roll = row['corr_roll_mean'] if not pd.isna(row['corr_roll_mean']) else 0
        stable = max(0.0, roll) * 100 * (1 - min(1.0, (row['corr_roll_vol'] or 0.3) / 0.6))
        amount = row['avg_amount_yi']
        liq = min(100.0, 40 + 30 * (np.log1p(amount) / np.log1p(300)))   # 成交额 0→40 分, ~300亿→100 分
        score = 0.55 * match + 0.20 * stable + 0.25 * liq
        tier = 'A' if score >= 60 else ('B' if score >= 45 else 'C')
        cards.append({
            'code': row['code'], 'name': row['name'], 'category': row['category'],
            'corr': row['corr'], 'corr_roll_mean': row['corr_roll_mean'], 'corr_roll_vol': row['corr_roll_vol'],
            'beta': row['beta'], 'avg_amount_yi': row['avg_amount_yi'], 'score': round(float(score), 1),
            'tier': tier, 'yearly': row['yearly'],
        })
    cards = [c for c in cards if c['corr'] > 0.05]

    print('[5/5] 配置建议（高分互不冗余子集 → 三方案）...')
    sel = []
    for c in cards:
        if len(sel) >= 8:
            break
        dup = False
        for have in sel:
            if have['code'] in mat and c['code'] in mat[have['code']]:
                if mat[have['code']][c['code']] > 0.85:
                    dup = True
                    break
        if not dup:
            sel.append(c)

    alloc = {}
    if sel:
        wts = []
        for c in sel:
            wts.append(c['code'])
        # A 等权
        wa = {c: 1.0 / len(sel) for c in wts}
        # B 相关加权（正相关归一）
        cs = [c['corr'] for c in sel]
        wb = {c['code']: max(0.0, c['corr']) / sum(max(0.0, x) for x in cs) for c in sel}
        # C 相关 × 流动性加权
        sc = [c['score'] for c in sel]
        wc = {c['code']: max(0.0, c['score']) / sum(max(0.0, x) for x in sc) for c in sel}
        for name, w in (('equal', wa), ('corr_w', wb), ('corr_liq_w', wc)):
            comp = pd.DataFrame({c: etf_rets[c]['ret'] for c in wts}).dropna()
            comp['ret'] = sum(w[c] * comp[c] for c in wts)
            m = pd.concat([strat_ret.reindex(comp.index), comp['ret']], axis=1, keys=['s', 'e']).dropna()
            if len(m) < 120:
                continue
            nav = (1 + m['e']).cumprod()
            alloc[name] = {
                'weights': {c: round(float(w[c]), 3) for c in wts},
                'corr_to_strategy': round(float(m['s'].corr(m['e'])), 3),
                'annual': round(float(nav.iloc[-1] ** (252 / len(nav)) - 1), 4),
                'max_dd': round(float((nav / nav.cummax() - 1).min()), 4),
                'sharpe': round(float(m['e'].mean() / m['e'].std() * np.sqrt(252)), 2) if m['e'].std() > 0 else 0,
            }
        rec = alloc.get('corr_liq_w') or alloc.get('corr_w') or alloc.get('equal')
    else:
        rec = None

    out = {
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'strategy': {
            'name': 'turn_low_defensive', 'rebalance_days': REBAL_DAYS, 'top_n': TOP_N, 't1': True,
            'days': int(len(strat_ret)),
            'annual': round(float(strat['nav'].iloc[-1] ** (252 / len(strat_ret)) - 1), 4),
            'max_dd': round(float((strat['nav'] / strat['nav'].cummax() - 1).min()), 4),
        },
        'etf_corr_top': corr_df.head(20)[['code', 'name', 'category', 'corr', 'corr_roll_mean', 'corr_roll_vol', 'beta', 'avg_amount_yi']].to_dict('records'),
        'etf_pitch': cards,
        'etf_corr_matrix': mat,
        'allocation': alloc,
        'recommend': rec,
        'notes': [
            '策略=turn_low defensive（2019+，40日 top20 等权，T+1，无止损无择时；年化 ~15.9% 是横截面因子暴露）',
            'ETF 相关 ~0.3-0.6 属正常：ETF 是风格/行业暴露代理，只能部分复制策略暴露，不是等价替代',
            '小资金建议：直接按 recommend.weights 买 ETF（场内，1手100份，几千元即可起步），作为策略暴露的补充表达',
            'ETF 跟踪误差与策略不等价：策略日收益来自 20 只低换手个股，ETF 无法复制个股选择，只能贴近风格',
            '数据：bars.db qfq + 新浪 ETF 日线（不复权，ETF 分红小，相关分析可接受）；相关为 pearson 日频（全期+滚动60日）',
        ],
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print('完成 -> %s （%.0fs）' % (OUT_PATH, time.time() - t0))


if __name__ == '__main__':
    main()
