#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MA Tracker v2 — rzetelny tracker trafności predykcji intraday.

Uruchamiany codziennie o 22:00 UTC (po zamknięciu NYSE 21:00 UTC).

Dla każdego rekordu 'pending':
  - Snapshoty OHLC (nie tylko close) dla okien 15m/30m/1h/2h/4h
  - Pre-market ruch (filing vs open price)
  - Pełny zakres: range_high, range_low w pierwszych 4h
  - Kiedy PIERWSZY RAZ dotknął target — vs kiedy UTRZYMAŁ target (sustained)
  - Kontekst rynkowy SPY — alpha = czysty M&A move bez rynku
  - direction_correct mierzony w 3 punktach: 15m, 1h, 4h
  - Raport tygodniowy z breakdown wg deal_structure, groq_score, filing_context
"""

import os
import json
import time
import logging
import requests
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional

try:
    import yfinance as yf
    YFINANCE_AVAILABLE = True
except ImportError:
    YFINANCE_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

GIST_TOKEN        = os.environ.get('GIST_TOKEN', '')
GIST_ID           = os.environ.get('GIST_ID_MA', '')
GIST_HISTORY_FILE = 'ma_history.json'
DISCORD_WEBHOOK   = os.environ.get('DISCORD_WEBHOOK_V2', '')

TARGET_PCT   = 1.0   # cel trade +1%
SESSION_OPEN = 14    # godzina otwarcia NYSE w UTC (14:30)

# ============================================
# GIST
# ============================================

def _gist_headers() -> Dict:
    return {'Authorization': f'token {GIST_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}

def load_history() -> List[Dict]:
    try:
        r = requests.get(f'https://api.github.com/gists/{GIST_ID}',
                         headers=_gist_headers(), timeout=10)
        r.raise_for_status()
        data = r.json()
        if GIST_HISTORY_FILE in data.get('files', {}):
            return json.loads(data['files'][GIST_HISTORY_FILE]['content'])
        return []
    except Exception as e:
        logger.error(f"load_history error: {e}")
        return []

def save_history(history: List[Dict]):
    try:
        payload = {'files': {GIST_HISTORY_FILE: {'content': json.dumps(history, indent=2)}}}
        r = requests.patch(f'https://api.github.com/gists/{GIST_ID}',
                           headers=_gist_headers(), json=payload, timeout=10)
        r.raise_for_status()
        logger.info(f"✓ Historia zapisana: {len(history)} rekordów")
    except Exception as e:
        logger.error(f"save_history error: {e}")

# ============================================
# INTRADAY DATA
# ============================================

def _pct(price: Optional[float], base: float) -> Optional[float]:
    if price is None or base == 0:
        return None
    return round((price - base) / base * 100, 2)

def get_1min_data(ticker: str):
    """Zwraca DataFrame z 1-min danymi z ostatnich 5 dni (limit Yahoo), tz=UTC."""
    if not YFINANCE_AVAILABLE:
        return None
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period='5d', interval='1m')
        if hist.empty:
            return None
        return hist.tz_convert('UTC')
    except Exception as e:
        logger.warning(f"   ✗ Yahoo 1-min {ticker}: {e}")
        return None

def build_ohlc_snapshot(day_data, open_time, from_min: int, to_min: int,
                        price_open: float) -> Dict:
    """
    Buduje snapshot OHLC dla okna (from_min, to_min] od otwarcia.
    Zwraca close_pct, high_pct, low_pct, volume_ratio.
    """
    t_from = open_time + timedelta(minutes=from_min)
    t_to   = open_time + timedelta(minutes=to_min)
    window = day_data[(day_data.index >= t_from) & (day_data.index < t_to)]

    if window.empty or price_open == 0:
        return {'close_pct': None, 'high_pct': None, 'low_pct': None, 'volume_ratio': None}

    close = float(window['Close'].iloc[-1])
    high  = float(window['High'].max())
    low   = float(window['Low'].min())
    vol   = float(window['Volume'].sum())

    # volume_ratio: wolumen tego okna vs śr. wolumen na minutę z całego dnia
    day_avg_vol_per_min = float(day_data['Volume'].mean()) or 1
    vol_ratio = round(vol / (day_avg_vol_per_min * (to_min - from_min)), 2)

    return {
        'close_pct':    _pct(close, price_open),
        'high_pct':     _pct(high,  price_open),
        'low_pct':      _pct(low,   price_open),
        'volume_ratio': vol_ratio,
    }

def get_spy_context(session_date: str, open_time) -> Dict:
    """Pobiera SPY open + 4h change dla kontekstu rynkowego."""
    try:
        spy_data = get_1min_data('SPY')
        if spy_data is None:
            return {}
        target_day = datetime.strptime(session_date, '%Y-%m-%d').date()
        day = spy_data[spy_data.index.date == target_day]
        open_slice = day[day.index >= open_time]
        if open_slice.empty:
            return {}
        spy_open = float(open_slice['Open'].iloc[0])
        t4h = open_time + timedelta(minutes=240)
        slice_4h = day[day.index >= t4h]
        spy_4h_price = float(slice_4h['Close'].iloc[0]) if not slice_4h.empty else None
        return {
            'spy_open_price': round(spy_open, 2),
            'spy_4h_pct':     _pct(spy_4h_price, spy_open),
        }
    except Exception as e:
        logger.warning(f"   ✗ SPY context: {e}")
        return {}

def get_premarket_data(ticker: str, session_date: str,
                       price_at_filing: Optional[float],
                       filing_ts: str) -> Dict:
    """
    Szacuje ruch pre-market: od kursu z poprzedniego zamknięcia do open.
    Yahoo 1-min nie daje pre-market danych, więc używamy previousClose vs open.
    """
    try:
        stock = yf.Ticker(ticker)
        info  = stock.info
        prev_close = info.get('previousClose') or info.get('regularMarketPreviousClose')
        if not prev_close:
            return {'available': False}

        # Pobierz open tego dnia
        hist_1d = stock.history(period='5d', interval='1d')
        if hist_1d.empty:
            return {'available': False}
        hist_1d.index = hist_1d.index.tz_localize('UTC') if hist_1d.index.tz is None else hist_1d.index.tz_convert('UTC')
        target_day = datetime.strptime(session_date, '%Y-%m-%d').date()
        day_row = hist_1d[hist_1d.index.date == target_day]
        if day_row.empty:
            return {'available': False}

        open_price = float(day_row['Open'].iloc[0])
        gap_from_prev_close = _pct(open_price, float(prev_close))

        result = {
            'available':           True,
            'prev_close':          round(float(prev_close), 4),
            'open_price':          round(open_price, 4),
            'gap_from_prev_close_pct': gap_from_prev_close,
        }
        # Jeśli mamy kurs z momentu filingu — dodaj też ten dystans
        if price_at_filing:
            result['price_at_filing']      = price_at_filing
            result['gap_from_filing_pct']  = _pct(open_price, float(price_at_filing))
        return result
    except Exception as e:
        logger.warning(f"   ✗ pre-market {ticker}: {e}")
        return {'available': False}

# ============================================
# SESSION DATE
# ============================================

def _next_session_date(filing_ts: str) -> str:
    """
    Zwraca datę sesji na której będzie ruch:
    - pre-market / w godzinach → ta sama sesja
    - after-hours (>=21:00 UTC) lub weekend → następny dzień roboczy
    """
    try:
        dt = datetime.strptime(filing_ts[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        if dt.hour >= 21 or dt.weekday() >= 5:
            dt += timedelta(days=1)
            while dt.weekday() >= 5:
                dt += timedelta(days=1)
        return dt.strftime('%Y-%m-%d')
    except Exception:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')

# ============================================
# PROCESS RECORD
# ============================================

def process_record(record: Dict) -> Dict:
    ticker      = record.get('ticker')
    filing_ts   = record.get('filing_timestamp', '')
    direction   = record.get('direction', 'bullish')
    price_filing = record.get('price_at_filing')

    session_date = _next_session_date(filing_ts)
    logger.info(f"   → {ticker} | sesja: {session_date} | direction: {direction}")

    # --- 1-min data ---
    hist = get_1min_data(ticker)
    if hist is None:
        record['tracker_status'] = 'no_data'
        return record

    target_day = datetime.strptime(session_date, '%Y-%m-%d').date()
    day_data   = hist[hist.index.date == target_day]
    if day_data.empty:
        logger.warning(f"   ✗ Brak danych sesji {session_date} dla {ticker}")
        record['tracker_status'] = 'no_data'
        return record

    open_time  = datetime(target_day.year, target_day.month, target_day.day,
                          14, 30, tzinfo=timezone.utc)
    open_slice = day_data[day_data.index >= open_time]
    if open_slice.empty:
        record['tracker_status'] = 'no_data'
        return record

    price_open = float(open_slice['Open'].iloc[0])
    record['price_at_open'] = round(price_open, 4)

    # --- Pre-market ---
    record['premarket'] = get_premarket_data(ticker, session_date, price_filing, filing_ts)
    time.sleep(0.3)

    # --- OHLC snapshoty (okna kumulatywne od open) ---
    windows = [
        ('t_15m',  0,  15),
        ('t_30m',  0,  30),
        ('t_1h',   0,  60),
        ('t_2h',   0, 120),
        ('t_4h',   0, 240),
    ]
    intraday = {}
    for label, from_m, to_m in windows:
        intraday[label] = build_ohlc_snapshot(day_data, open_time, from_m, to_m, price_open)
    record['intraday'] = intraday

    # --- Pełny zakres 4h ---
    window_4h = day_data[(day_data.index >= open_time) &
                         (day_data.index < open_time + timedelta(minutes=241))]
    if not window_4h.empty and price_open > 0:
        range_high = _pct(float(window_4h['High'].max()),  price_open)
        range_low  = _pct(float(window_4h['Low'].min()),   price_open)
        close_4h_price = float(window_4h['Close'].iloc[-1])
        close_4h_pct   = _pct(close_4h_price, price_open)
    else:
        range_high = range_low = close_4h_pct = None

    # --- Pierwsze dotknięcie targetu vs sustained (utrzymany na close okna) ---
    first_touch_at = first_touch_min = None
    sustained_at = None

    for label, _, to_m in windows:
        snap = intraday.get(label, {})
        high_pct  = snap.get('high_pct')
        close_pct = snap.get('close_pct')

        # Uwzględnij kierunek (bullish = liczymy +, bearish = liczymy -)
        def _move(v):
            if v is None:
                return None
            return v if direction == 'bullish' else -v

        if first_touch_at is None and _move(high_pct) is not None and _move(high_pct) >= TARGET_PCT:
            first_touch_at  = label
            first_touch_min = to_m

        if sustained_at is None and _move(close_pct) is not None and _move(close_pct) >= TARGET_PCT:
            sustained_at = label

    # --- Direction correct — 3 punkty ---
    def dir_correct(label):
        pct = intraday.get(label, {}).get('close_pct')
        if pct is None:
            return None
        return (pct > 0) if direction == 'bullish' else (pct < 0)

    record['trade_result'] = {
        'target_pct':          TARGET_PCT,
        'first_touch_at':      first_touch_at,
        'first_touch_minutes': first_touch_min,
        'sustained_hit':       sustained_at is not None,
        'sustained_at':        sustained_at,
        'range_high_pct':      range_high,
        'range_low_pct':       range_low,
        'close_4h_pct':        close_4h_pct,
    }

    record['accuracy'] = {
        'direction_at_15m': dir_correct('t_15m'),
        'direction_at_1h':  dir_correct('t_1h'),
        'direction_at_4h':  dir_correct('t_4h'),
        # Główna metryka: czy UTRZYMAŁ target na close (nie tylko dotknął)
        'target_sustained': sustained_at is not None,
        # Pomocnicza: czy kiedykolwiek dotknął (do analizy wejścia)
        'target_touched':   first_touch_at is not None,
    }

    # --- Kontekst rynkowy SPY ---
    spy = get_spy_context(session_date, open_time)
    if spy and close_4h_pct is not None and spy.get('spy_4h_pct') is not None:
        spy['alpha_4h'] = round(close_4h_pct - spy['spy_4h_pct'], 2)
    record['market_context'] = spy

    record['tracker_status'] = 'tracked'
    logger.info(f"   ✓ tracked | range: {range_low}% / {range_high}% | close_4h: {close_4h_pct}% | "
                f"sustained: {sustained_at} | dir_1h: {dir_correct('t_1h')}")
    return record

# ============================================
# TYGODNIOWY RAPORT
# ============================================

def send_weekly_report(history: List[Dict]):
    if not DISCORD_WEBHOOK:
        return

    tracked = [r for r in history if r.get('tracker_status') == 'tracked']
    if len(tracked) < 2:
        logger.info("Za mało danych na raport (< 2 rekordy)")
        return

    total = len(tracked)

    def pct_of(condition_fn):
        n = sum(1 for r in tracked if condition_fn(r) is True)
        return n, round(n / total * 100)

    n_dir_1h,  p_dir_1h  = pct_of(lambda r: r.get('accuracy', {}).get('direction_at_1h'))
    n_dir_4h,  p_dir_4h  = pct_of(lambda r: r.get('accuracy', {}).get('direction_at_4h'))
    n_touched, p_touched = pct_of(lambda r: r.get('accuracy', {}).get('target_touched'))
    n_sustained, p_sust  = pct_of(lambda r: r.get('accuracy', {}).get('target_sustained'))

    # Avg range high/low
    highs  = [r['trade_result']['range_high_pct'] for r in tracked if r.get('trade_result', {}).get('range_high_pct') is not None]
    lows   = [r['trade_result']['range_low_pct']  for r in tracked if r.get('trade_result', {}).get('range_low_pct')  is not None]
    avg_high = round(sum(highs) / len(highs), 2) if highs else None
    avg_low  = round(sum(lows)  / len(lows),  2) if lows  else None

    # Avg alpha (czysty M&A move)
    alphas = [r.get('market_context', {}).get('alpha_4h') for r in tracked
              if r.get('market_context', {}).get('alpha_4h') is not None]
    avg_alpha = round(sum(alphas) / len(alphas), 2) if alphas else None

    # Czas pierwszego dotkniecia targetu
    times = [r['trade_result']['first_touch_minutes'] for r in tracked
             if r.get('trade_result', {}).get('first_touch_minutes')]
    avg_time = round(sum(times) / len(times)) if times else None

    # Breakdown wg deal_structure (sustained_hit)
    structs: Dict = {}
    for r in tracked:
        s = r.get('deal_structure', 'unknown')
        structs.setdefault(s, {'total': 0, 'sustained': 0})
        structs[s]['total'] += 1
        if r.get('accuracy', {}).get('target_sustained'):
            structs[s]['sustained'] += 1

    struct_lines = '\n'.join(
        f"• **{s}:** {d['sustained']}/{d['total']} utrzymało target"
        for s, d in sorted(structs.items())
    )

    # Breakdown wg groq_score (9-10 vs 7-8 vs 5-6)
    score_lines = ''
    for bucket, lo, hi in [('9–10 MEGA/MAJOR', 9, 10), ('7–8 MAJOR', 7, 8), ('5–6 STANDARD', 5, 6)]:
        sub = [r for r in tracked if lo <= (r.get('groq_score') or 0) <= hi]
        if sub:
            sust = sum(1 for r in sub if r.get('accuracy', {}).get('target_sustained'))
            score_lines += f"• **Score {bucket}:** {sust}/{len(sub)} utrzymało target\n"

    # Breakdown wg filing_context
    ctx_lines = ''
    for ctx in ['pre-market', 'market-hours', 'after-hours']:
        sub = [r for r in tracked if r.get('filing_context') == ctx]
        if sub:
            sust = sum(1 for r in sub if r.get('accuracy', {}).get('target_sustained'))
            ctx_lines += f"• **{ctx}:** {sust}/{len(sub)} utrzymało target\n"

    desc = (
        f"**Próbka:** {total} alertów\n\n"
        f"**Kierunek trafny @ 1h:** {n_dir_1h}/{total} = **{p_dir_1h}%**\n"
        f"**Kierunek trafny @ 4h:** {n_dir_4h}/{total} = **{p_dir_4h}%**\n\n"
        f"**Target +{TARGET_PCT}% dotknięty (kiedykolwiek):** {n_touched}/{total} = **{p_touched}%**\n"
        f"**Target +{TARGET_PCT}% utrzymany na close:** {n_sustained}/{total} = **{p_sust}%**\n"
        + (f"**Śr. czas do pierwszego dotkn.:** {avg_time} min\n" if avg_time else '')
        + (f"\n**Śr. max ruch 4h:** +{avg_high}% / {avg_low}%\n" if avg_high else '')
        + (f"**Śr. alpha (vs SPY):** +{avg_alpha}%\n" if avg_alpha is not None else '')
        + f"\n**Wg struktury dealu:**\n{struct_lines}\n"
        + (f"\n**Wg Groq score:**\n{score_lines}" if score_lines else '')
        + (f"\n**Wg czasu filingu:**\n{ctx_lines}" if ctx_lines else '')
    )

    payload = {
        'embeds': [{
            'title':       '📊 Tygodniowy raport trafności alertów M&A',
            'description': desc,
            'color':       3447003,
            'footer':      {'text': f"Dane z {total} alertów | tracker v2"},
        }]
    }
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
        logger.info("✓ Raport tygodniowy wysłany na Discord")
    except Exception as e:
        logger.error(f"✗ Discord raport: {e}")

# ============================================
# MAIN
# ============================================

def run_tracker():
    logger.info("=" * 55)
    logger.info(f"MA Tracker v2 | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    logger.info("=" * 55)

    if not GIST_TOKEN or not GIST_ID:
        logger.error("Brak GIST_TOKEN lub GIST_ID_MA")
        return

    history = load_history()
    pending = [r for r in history if r.get('tracker_status') == 'pending']
    logger.info(f"Pending: {len(pending)} / {len(history)} łącznie")

    if not pending:
        logger.info("Brak rekordów do przetworzenia")
        if datetime.now(timezone.utc).weekday() == 0:
            send_weekly_report(history)
        return

    updated = 0
    for i, record in enumerate(history):
        if record.get('tracker_status') != 'pending':
            continue
        logger.info(f"\n[{updated+1}/{len(pending)}] {record.get('id')}")
        history[i] = process_record(record)
        updated += 1
        time.sleep(1)

    save_history(history)
    logger.info(f"\n✓ Tracker zakończony: {updated} rekordów zaktualizowanych")

    if datetime.now(timezone.utc).weekday() == 0:
        send_weekly_report(history)


if __name__ == "__main__":
    run_tracker()
