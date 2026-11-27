# -*- coding: utf-8 -*-
"""Upstox equity DELIVERY (CNC) charges for NSE cash trades.

Source: https://upstox.com/pricing/ (read 27 Nov 2026). Per executed order:
  brokerage      Rs 20
  STT            0.1% on buy and on sell (delivery)
  stamp duty     0.015% on buy only
  exchange (NSE) 0.00297% on both sides
  IPFT (NSE)     Rs 0.10 per lakh  = 0.0001%
  SEBI           Rs 10 per crore  = 0.0001%
  DP charge      Rs 20 per scrip per day, sell side only
  GST            18% on brokerage + exchange + IPFT + DP
"""

BROKERAGE = 20.0
STT = 0.001
STAMP = 0.00015
EXCH = 0.0000297
IPFT = 1e-6
SEBI = 1e-6
DP = 20.0
GST = 0.18

ZERO = {'brokerage': 0.0, 'stt': 0.0, 'stamp': 0.0, 'exchange': 0.0,
        'ipft': 0.0, 'sebi': 0.0, 'dp': 0.0, 'gst': 0.0, 'total': 0.0}


def delivery_charges(side, turnover):
    # charges for one executed delivery order; side is BUY or SELL
    v = abs(float(turnover or 0.0))
    if v <= 0:
        return dict(ZERO)
    buy = str(side).upper() == 'BUY'
    brokerage = BROKERAGE
    stt = v * STT
    stamp = v * STAMP if buy else 0.0
    exch = v * EXCH
    ipft = v * IPFT
    sebi = v * SEBI
    dp = 0.0 if buy else DP
    gst = GST * (brokerage + exch + ipft + dp)
    total = brokerage + stt + stamp + exch + ipft + sebi + dp + gst
    return {'brokerage': round(brokerage, 2), 'stt': round(stt, 2),
            'stamp': round(stamp, 2), 'exchange': round(exch, 2),
            'ipft': round(ipft, 2), 'sebi': round(sebi, 2),
            'dp': round(dp, 2), 'gst': round(gst, 2), 'total': round(total, 2)}


def round_trip(turnover):
    # buy + sell charges on the same turnover, plus the % they cost
    v = abs(float(turnover or 0.0))
    b = delivery_charges('BUY', v)
    s = delivery_charges('SELL', v)
    tot = b['total'] + s['total']
    return {'buy': b, 'sell': s, 'total': round(tot, 2),
            'pct': round(100.0 * tot / v, 4) if v else 0.0}
