# -*- coding: utf-8 -*-
'''Build the daily council brief from the live paper book.

The council (4 AI reviewers) gets ONE compact text block: what the bots did, what
it cost in fees, and the standing rules. It is a reviewer, not a data pipeline.

Usage:  python -X utf8 council.py   (reads origin/main:arena_state.json)
'''
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import arena
import fees_in

BASE = os.path.dirname(os.path.abspath(__file__))
NAMES = {b['id']: b['name'] for b in arena.BOTS}
IST = timezone(timedelta(hours=5, minutes=30))
LOG = os.path.join(BASE, 'council_log.jsonl')


def git_show(rel, ref='origin/main'):
    try:
        p = subprocess.run(['git', 'show', ref + ':' + rel], cwd=BASE,
                           capture_output=True, text=True, timeout=60)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout
    except Exception:
        pass
    try:
        with open(os.path.join(BASE, rel), encoding='utf-8') as f:
            return f.read()
    except Exception:
        return ''


def close_fee(sym, qty, entry, exit_px, side):
    try:
        s = str(sym)
        if '/' in s and any(l.endswith('.NS') for l in s.split('/')):
            q = abs(qty)
            return 2.0 * (fees_in.delivery_charges('BUY', q)['total']
                          + fees_in.delivery_charges('SELL', q)['total'])
        if '/' in s:
            return 2.0 * abs(qty) * 0.001
        if s.endswith('.NS'):
            buy = abs(qty) * (entry if side > 0 else exit_px)
            sell = abs(qty) * (exit_px if side > 0 else entry)
            return (fees_in.delivery_charges('BUY', buy)['total']
                    + fees_in.delivery_charges('SELL', sell)['total'])
        return abs(qty) * (abs(entry) + abs(exit_px)) * 0.001
    except Exception:
        return 0.0


def main():
    try:
        st = json.loads(git_show('arena_state.json') or '{}')
    except Exception:
        st = {}
    if not st.get('bots'):
        print('NO STATE')
        return 1
    bots = st['bots']
    now = datetime.now(IST)
    today = now.strftime('%Y-%m-%d')
    hist = st.get('equity_history') or []
    marks = [h for h in hist if str(h.get('t', ''))[:10] == today]
    first_mark = marks[0] if marks else (hist[-1] if hist else {})
    last_mark = marks[-1] if marks else (hist[-1] if hist else {})
    pool = float(last_mark.get('total', 0.0) or 0.0)
    pool_start = float(first_mark.get('total', pool) or pool)
    L = []
    L.append('STRATEGY ARENA - daily council brief')
    L.append('now %s | season created %s | Rs 100,000 per bot | bots %d'
             % (now.strftime('%Y-%m-%d %H:%M IST'), str(st.get('created', ''))[:16], len(bots)))
    L.append('pool now Rs %.2f | since midnight %+.2f | since season start %+.2f (%+.3f%%)'
             % (pool, pool - pool_start, pool - 1600000.0,
                100.0 * (pool - 1600000.0) / 1600000.0))
    L.append('')
    L.append('BOT TABLE  id | name | equity now | vs season start | open | closed')
    for bid in sorted(bots):
        b = bots[bid]
        e = float(last_mark.get(bid, b.get('cash', 0.0)) or 0.0)
        L.append('%-4s | %-32s | %9.0f | %+8.2f | open %d | closed %d'
                 % (bid, str(NAMES.get(bid, ''))[:32], e, e - 100000.0,
                    len(b.get('positions') or {}), len(b.get('closed_trades') or [])))
    L.append('')
    rows = [r for r in (git_show('arena_trades.csv') or '').splitlines() if r.strip()]
    hdr = rows[0].split(',') if rows else []
    closed = []
    if hdr:
        for r in rows[1:]:
            v = r.split(',')
            if len(v) >= len(hdr):
                closed.append(dict(zip(hdr, v)))
    net = sum(float(c.get('pnl') or 0) for c in closed)
    fees = sum(close_fee(c.get('symbol', ''), float(c.get('qty') or 0),
                         float(c.get('entry') or 0), float(c.get('exit') or 0),
                         1 if c.get('side') == 'LONG' else -1) for c in closed)
    L.append('CLOSED TRADES this season: %d | net %+.2f | fees ~%.2f | gross %+.2f'
             % (len(closed), net, fees, net + fees))
    for c in closed[-12:]:
        L.append('  %s %-4s %-13s %-5s qty %8.3f %10.2f -> %10.2f  pnl %+8.2f  (%s)'
                 % (str(c.get('time', ''))[11:16], c.get('bot', ''), c.get('symbol', ''),
                    c.get('side', ''), float(c.get('qty') or 0), float(c.get('entry') or 0),
                    float(c.get('exit') or 0), float(c.get('pnl') or 0), c.get('reason', '')))
    L.append('')
    live = []
    for bid in sorted(bots):
        b = bots[bid]
        for sym, p in (b.get('positions') or {}).items():
            px = (b.get('last_prices') or {}).get(sym, p.get('entry', 0.0))
            u = p.get('side', 1) * p.get('qty', 0.0) * (px - p.get('entry', 0.0))
            n = (abs(p.get('qty', 0.0)) if '/' in sym
                 else abs(p.get('qty', 0.0)) * abs(p.get('entry', 0.0)))
            live.append('  %-4s %-14s %-5s notional Rs %8.0f (%4.1f%% of book) upnl %+8.2f'
                        % (bid, sym, 'LONG' if p.get('side', 1) > 0 else 'SHORT',
                           n, 100.0 * n / 100000.0, u))
    L.append('OPEN POSITIONS (%d)' % len(live))
    L += live or ['  none']
    L.append('')
    L.append('STANDING RULES (risk controls are not up for removal)')
    L.append('- NSE fills pay Upstox delivery: Rs 20/order, STT 0.1% both sides, stamp 0.015%')
    L.append('  buy, exchange 0.00297%, DP Rs 20 on sell, GST 18%. Round trip 0.51% at Rs 25k,')
    L.append('  0.29% at Rs 1L, 7.3% at Rs 1k.')
    L.append('- US: Interactive Brokers schedule - USD 0.0035/share, min USD 0.35/order, 18% GST,')
    L.append('  FX 0.002% with a USD 2/conversion minimum that only bites if each trade converts')
    L.append('  cash (a funded USD float is assumed). Crypto: 0.1%/side. US min ticket Rs 15,000.')
    L.append('- Entry gates: session must be open, stop move >= 0.30%, NSE notional lifted to')
    L.append('  Rs 25000 (or US Rs 15000) when that would not risk >2% of the book, move >= 2x')
    L.append('  round-trip fee, no entry in the last 20 min, re-entry cooldown after a stop.')
    L.append('- Sizing: risk based (1% of book default), 30% notional cap per position,'
             ' 30% net exposure per market, 40% gross same-side per market.')
    L.append('YOUR JOB: review the last 24h. Classify every loss as (a) CODE BUG, (b) STRATEGY')
    L.append('FLAW, (c) MARKET. Then give: 3 findings ranked by rupee impact, 1-3 concrete changes')
    L.append('naming the file or constant they touch, and the check that proves the change worked.')
    print('\n'.join(L))
    return 0


if __name__ == '__main__':
    sys.exit(main())