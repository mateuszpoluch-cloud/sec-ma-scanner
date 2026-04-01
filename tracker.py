#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MA Tracker — uzupełnia snapshoty intraday w historii alertów.

Uruchamiany codziennie o 22:00 UTC (po zamknięciu NYSE 21:00 UTC).
Dla każdego rekordu ze statusem 'pending':
  - Pobiera 1-minutowe dane z Yahoo Finance dla dnia sesji
  - Uzupełnia price_at_open, gap_at_open_pct, snapshoty t_15m/t_30m/t_1h/t_2h/t_4h
  - Wylicza max_move_pct, max_drawdown_pct, czy target +1% został osiągnięty
  - Aktualizuje accuracy.direction_correct, accuracy.target_reached
  - Ustawia tracker_status: 'tracked' (dane pobrane) lub 'no_session' (brak sesji)
  - Wysyła tygodniowy raport trafności na Discord (każdy poniedziałek)
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

TARGET_PCT = 1.0  # cel trades +1%

# ============================================
# GIST
# ============================================

def _gist_headers():
    return {'Authorization': f'token {GIST_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}

def load_history() -> List[Dict]:
    try:
        r = requests.get(f'https://api.github.com/gists/{GIST_ID}', headers=_gist_headers(), timeout=10)
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
        r = requests.patch(f'https://api.github.com/gists/{GIST_ID}', headers=_gist_headers(), json=payload, timeout=10)
        r.raise_for_status()
        logger.info(f"✓ Historia zapisana: {len(history)} rekordów")
    except Exception as e:
        logger.error(f"save_history error: {e}")

# ============================================
# INTRADAY DATA
# ============================================

def get_intraday_snapshots(ticker: str, session_date: str) -> Optional[Dict]:
    """
    Pobiera 1-minutowe dane z Yahoo Finance dla danego dnia sesji.
    session_date: 'YYYY-MM-DD' (dzień sesji na którym szukamy otwarcia)
    Zwraca dict z price_at_open, gap_pct i snapshotami t_15m/t_30m/t_1h/t_2h/t_4h.
    """
    if not YFINANCE_AVAILABLE:
        return None
    try:
        stock = yf.Ticker(ticker)
        # Pobierz dane 1-minutowe z ostatnich 5 dni (Yahoo nie daje 1-min starszych niż 7 dni)
        hist = stock.history(period='5d', interval='1m')
        if hist.empty:
            logger.warning(f"   ✗ Brak danych 1-min dla {ticker}")
            return None

        hist = hist.tz_convert('UTC')

        # Filtruj do dnia sesji
        target_day = datetime.strptime(session_date, '%Y-%m-%d').date()
        day_data = hist[hist.index.date == target_day]

        if day_data.empty:
            logger.warning(f"   ✗ Brak danych dla {ticker} w dniu {session_date}")
            return None

        # Otwarcie rynku NYSE: 14:30 UTC
        open_time = datetime(target_day.year, target_day.month, target_day.day, 14, 30, tzinfo=timezone.utc)

        # Kurs na otwarciu — pierwsza świeczka >= 14:30
        open_data = day_data[day_data.index >= open_time]
        if open_data.empty:
            return None

        price_open = float(open_data['Open'].iloc[0])

        def price_at_offset(minutes: int) -> Optional[float]:
            t = open_time + timedelta(minutes=minutes)
            slice_ = day_data[day_data.index >= t]
            if slice_.empty:
                return None
            return round(float(slice_['Close'].iloc[0]), 4)

        def change_pct(price: Optional[float]) -> Optional[float]:
            if price is None or price_open == 0:
                return None
            return round((price - price_open) / price_open * 100, 2)

        p15  = price_at_offset(15)
        p30  = price_at_offset(30)
        p60  = price_at_offset(60)
        p120 = price_at_offset(120)
        p240 = price_at_offset(240)

        # Max move i max drawdown w ciągu pierwszych 4h
        window = day_data[day_data.index < open_time + timedelta(minutes=241)]
        if not window.empty and price_open > 0:
            max_price  = float(window['High'].max())
            min_price  = float(window['Low'].min())
            max_move   = round((max_price - price_open) / price_open * 100, 2)
            max_ddwn   = round((min_price - price_open) / price_open * 100, 2)
        else:
            max_move = max_ddwn = None

        return {
            'price_at_open': round(price_open, 4),
            't_15m': {'price': p15,  'change_from_open_pct': change_pct(p15)},
            't_30m': {'price': p30,  'change_from_open_pct': change_pct(p30)},
            't_1h':  {'price': p60,  'change_from_open_pct': change_pct(p60)},
            't_2h':  {'price': p120, 'change_from_open_pct': change_pct(p120)},
            't_4h':  {'price': p240, 'change_from_open_pct': change_pct(p240)},
            'max_move_pct':     max_move,
            'max_drawdown_pct': max_ddwn,
        }

    except Exception as e:
        logger.error(f"   ✗ get_intraday_snapshots {ticker}: {e}")
        return None

# ============================================
# TRACKING LOGIC
# ============================================

def _next_session_date(filing_ts: str) -> str:
    """
    Zwraca datę sesji (YYYY-MM-DD) na której powinien być ruch:
    - filing w pre-market lub w nocy → ta sama sesja
    - filing po zamknięciu (>21:00 UTC) lub weekend → następna sesja
    """
    try:
        dt = datetime.strptime(filing_ts[:19], '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)
        # Po 21:00 UTC (NYSE close) lub weekend → następny dzień roboczy
        if dt.hour >= 21 or dt.weekday() >= 5:
            dt += timedelta(days=1)
            while dt.weekday() >= 5:
                dt += timedelta(days=1)
        return dt.strftime('%Y-%m-%d')
    except Exception:
        return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def process_record(record: Dict) -> Dict:
    ticker    = record.get('ticker')
    filing_ts = record.get('filing_timestamp', '')
    direction = record.get('direction', 'bullish')

    session_date = _next_session_date(filing_ts)
    logger.info(f"   Tracker: {ticker} | sesja: {session_date}")

    snapshots = get_intraday_snapshots(ticker, session_date)

    if snapshots is None:
        record['tracker_status'] = 'no_data'
        return record

    price_open = snapshots['price_at_open']
    price_at_filing = record.get('price_at_filing')

    # Gap przy otwarciu względem kursu w chwili filingu
    if price_at_filing and price_open and float(price_at_filing) > 0:
        record['gap_at_open_pct'] = round((price_open - float(price_at_filing)) / float(price_at_filing) * 100, 2)
    record['price_at_open'] = price_open

    # Snapshoty
    record['intraday'] = {
        't_15m': snapshots['t_15m'],
        't_30m': snapshots['t_30m'],
        't_1h':  snapshots['t_1h'],
        't_2h':  snapshots['t_2h'],
        't_4h':  snapshots['t_4h'],
    }

    max_move = snapshots.get('max_move_pct')
    max_ddwn = snapshots.get('max_drawdown_pct')

    # Czy target +1% osiągnięty i kiedy?
    target_hit = False
    hit_at = None
    hit_at_minutes = None
    for label, minutes in [('t_15m', 15), ('t_30m', 30), ('t_1h', 60), ('t_2h', 120), ('t_4h', 240)]:
        snap = snapshots.get(label, {})
        if snap and snap.get('change_from_open_pct') is not None:
            chg = snap['change_from_open_pct']
            move = chg if direction == 'bullish' else -chg
            if move >= TARGET_PCT and not target_hit:
                target_hit = True
                hit_at = label
                hit_at_minutes = minutes

    # Kierunek — czy cena poszła w predyktowanym kierunku (oceniamy po 1h)
    snap_1h = snapshots.get('t_1h', {})
    if snap_1h and snap_1h.get('change_from_open_pct') is not None:
        chg_1h = snap_1h['change_from_open_pct']
        direction_correct = (chg_1h > 0) if direction == 'bullish' else (chg_1h < 0)
    else:
        direction_correct = None

    record['trade_result'] = {
        'target_hit':       target_hit,
        'target_pct':       TARGET_PCT,
        'hit_at':           hit_at,
        'hit_at_minutes':   hit_at_minutes,
        'max_move_pct':     max_move,
        'max_drawdown_pct': max_ddwn,
    }
    record['accuracy'] = {
        'direction_correct': direction_correct,
        'target_reached':    target_hit,
    }
    record['tracker_status'] = 'tracked'
    return record

# ============================================
# TYGODNIOWY RAPORT NA DISCORD
# ============================================

def send_weekly_report(history: List[Dict]):
    if not DISCORD_WEBHOOK:
        return

    tracked = [r for r in history if r.get('tracker_status') == 'tracked']
    if not tracked:
        return

    total   = len(tracked)
    correct = sum(1 for r in tracked if r.get('accuracy', {}).get('direction_correct') is True)
    targets = sum(1 for r in tracked if r.get('accuracy', {}).get('target_reached') is True)
    acc_pct = round(correct / total * 100) if total else 0
    tgt_pct = round(targets / total * 100) if total else 0

    # Średni czas do targetu
    times = [r['trade_result']['hit_at_minutes'] for r in tracked
             if r.get('trade_result', {}).get('hit_at_minutes')]
    avg_time = round(sum(times) / len(times)) if times else None

    # Breakdown wg deal_structure
    structures = {}
    for r in tracked:
        s = r.get('deal_structure', 'unknown')
        if s not in structures:
            structures[s] = {'total': 0, 'correct': 0}
        structures[s]['total'] += 1
        if r.get('accuracy', {}).get('direction_correct'):
            structures[s]['correct'] += 1

    struct_lines = '\n'.join(
        f"• {s}: {d['correct']}/{d['total']} trafnych"
        for s, d in structures.items()
    )

    desc = (
        f"**Łącznie przeanalizowanych:** {total}\n"
        f"**Kierunek trafny (1h):** {correct}/{total} = **{acc_pct}%**\n"
        f"**Target +{TARGET_PCT}% osiągnięty:** {targets}/{total} = **{tgt_pct}%**\n"
        + (f"**Średni czas do targetu:** {avg_time} min\n" if avg_time else '')
        + f"\n**Wg struktury dealu:**\n{struct_lines}"
    )

    payload = {'embeds': [{'title': '📊 Tygodniowy raport trafności alertów M&A', 'description': desc, 'color': 3447003}]}
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        r.raise_for_status()
        logger.info("✓ Raport tygodniowy wysłany na Discord")
    except Exception as e:
        logger.error(f"✗ Discord raport błąd: {e}")

# ============================================
# MAIN
# ============================================

def run_tracker():
    logger.info("=" * 50)
    logger.info(f"MA Tracker | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 50)

    if not GIST_TOKEN or not GIST_ID:
        logger.error("Brak GIST_TOKEN lub GIST_ID_MA")
        return

    history = load_history()
    pending = [r for r in history if r.get('tracker_status') == 'pending']
    logger.info(f"Rekordów do tracku: {len(pending)} / {len(history)} łącznie")

    updated = 0
    for record in history:
        if record.get('tracker_status') != 'pending':
            continue
        logger.info(f"\n→ {record.get('id')}")
        record = process_record(record)
        updated += 1
        time.sleep(1)

    save_history(history)
    logger.info(f"\n✓ Tracker zakończony: {updated} rekordów zaktualizowanych")

    # Raport w każdy poniedziałek
    if datetime.now(timezone.utc).weekday() == 0:
        send_weekly_report(history)


if __name__ == "__main__":
    run_tracker()
