BREAKEVEN_TRIGGER = 0.005
import os
import time
import logging
import math
import threading
import json
import ccxt
from dotenv import load_dotenv
from flask import Flask, request, jsonify
import db
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

load_dotenv()
API_KEY        = os.getenv('OKX_API_KEY')
API_SECRET     = os.getenv('OKX_API_SECRET')
API_PASSPHRASE = os.getenv('OKX_PASSPHRASE')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')

SYMBOLS = ['BTC/USD:USD-310404']
STRATEGIES = ['v22', 'ct', 'wr', 'bm']
SIGNAL_MAP = {
    'bm_long': 'bm',
    'bm_short': 'bm',
    'long':    'v22', 'short':    'v22',
    'ct_long': 'ct',  'ct_short': 'ct',
    'wr_long': 'wr',  'wr_short': 'wr',
    'bounce_long': 'v22', 'bounce_short': 'v22',
}
LEVERAGE       = 10
RISK_PCT       = 0.05
TRAIL_PHASE1   = 0.003
TRAIL_PHASE2   = 0.003
PHASE2_TRIGGER = 0.005
MAX_CANDLES    = 12
CANDLE_SEC     = 15 * 60

client = ccxt.okx({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'password': API_PASSPHRASE,
    'hostname': 'eea.okx.com',
})
price_client = client
log.info("ELES modban fut! (OKX X-Perp)")

def _empty_position():
    return {
        'active': False, 'side': None, 'entry_price': 0.0,
        'quantity': 0.0, 'highest': 0.0, 'lowest': 999999.0,
        'trail_sl': 0.0, 'phase': 1, 'open_time': 0,
        'candles': 0, 'tp': None
    }

positions = {sym: {s: _empty_position() for s in STRATEGIES} for sym in SYMBOLS}
locks     = {sym: {s: threading.Lock() for s in STRATEGIES} for sym in SYMBOLS}

# ── Segédfüggvények ───────────────────────────────────────────

def get_balance():
    try:
        balance = client.fetch_balance()
        usdc = balance.get('USDC', {}).get('free', 0) or 0
        return float(usdc)
    except Exception as e:
        log.error(f"Egyenleg hiba: {e}")
    return 0.0

def get_price(symbol):
    try:
        ticker = price_client.fetch_ticker(symbol)
        return float(ticker['last'])
    except Exception as e:
        log.error(f"Ar hiba [{symbol}]: {e}")
    return 0.0

def set_leverage_all():
    for sym in SYMBOLS:
        try:
            client.set_leverage(LEVERAGE, sym, params={'mgnMode': 'isolated'})
            log.info(f"Leverage {LEVERAGE}x beallitva: {sym}")
        except Exception as e:
            log.error(f"Leverage hiba [{sym}]: {e}")

_min_amount_cache = {}

def get_min_amount(symbol):
    if symbol in _min_amount_cache:
        return _min_amount_cache[symbol]
    try:
        market = client.market(symbol)
        min_amt = (market.get('limits', {}).get('amount', {}) or {}).get('min') or 1.0
        _min_amount_cache[symbol] = min_amt
        return min_amt
    except Exception as e:
        log.error(f"MIN_AMOUNT hiba [{symbol}]: {e}")
    return 1.0

_contract_size_cache = {}

def get_contract_size(symbol):
    if symbol in _contract_size_cache:
        return _contract_size_cache[symbol]
    try:
        market = client.market(symbol)
        size = market.get('contractSize') or 0.0001
        _contract_size_cache[symbol] = size
        return size
    except Exception as e:
        log.error(f"CONTRACT_SIZE hiba [{symbol}]: {e}")
    return 0.0001

def calc_quantity(price, sl_price, balance):
    risk_amount  = balance * RISK_PCT
    sl_distance  = abs(price - sl_price) / price
    if sl_distance == 0:
        return 0
    position_size = risk_amount / sl_distance
    return position_size / price

# ── Pozíció mentés / betöltés ─────────────────────────────────

POSITION_FILE = 'positions.json'

def save_positions():
    with open(POSITION_FILE, 'w') as f:
        json.dump(positions, f)

def load_positions():
    global positions
    try:
        if os.path.exists(POSITION_FILE):
            with open(POSITION_FILE, 'r') as f:
                data = json.load(f)
            for sym in SYMBOLS:
                if sym in data:
                    positions[sym].update(data[sym])
            log.info("Poziciok visszatoltve!")
    except Exception as e:
        log.error(f"Pozicio betoltes hiba: {e}")

# ── Pozíció nyitás / zárás ────────────────────────────────────

def open_long(symbol, strategy, price, sl_price, tp_price=None):
    try:
        balance  = get_balance()
        btc_qty  = calc_quantity(price, sl_price, balance)
        ct_size  = get_contract_size(symbol)
        raw_qty  = btc_qty / ct_size
        min_qty  = get_min_amount(symbol)
        if min_qty and raw_qty < min_qty:
            raw_qty = min_qty
        qty      = float(client.amount_to_precision(symbol, raw_qty))
        if qty <= 0:
            log.error(f"Hibas pozicio meret [{symbol}/{strategy}]!")
            return False
        client.create_order(symbol, 'market', 'buy', qty, params={'tdMode': 'isolated', 'ccy': 'USDC'})
        with locks[symbol][strategy]:
            p = positions[symbol][strategy]
            p['active']      = True
            p['side']        = 'long'
            p['entry_price'] = price
            p['quantity']    = qty
            p['highest']     = price
            p['trail_sl']    = sl_price
            p['phase']       = 1
            p['open_time']   = time.time()
            p['candles']     = 0
            p['tp']          = tp_price
        log.info(f"LONG nyitva [{symbol}/{strategy}]: {price} | Qty: {qty} | SL: {sl_price} | TP: {tp_price}")
        save_positions()
        return True
    except Exception as e:
        log.error(f"Long hiba [{symbol}/{strategy}]: {e}")
        return False

def open_short(symbol, strategy, price, sl_price, tp_price=None):
    try:
        balance  = get_balance()
        btc_qty  = calc_quantity(price, sl_price, balance)
        ct_size  = get_contract_size(symbol)
        raw_qty  = btc_qty / ct_size
        min_qty  = get_min_amount(symbol)
        if min_qty and raw_qty < min_qty:
            raw_qty = min_qty
        qty      = float(client.amount_to_precision(symbol, raw_qty))
        if qty <= 0:
            log.error(f"Hibas pozicio meret [{symbol}/{strategy}]!")
            return False
        client.create_order(symbol, 'market', 'sell', qty, params={'tdMode': 'isolated', 'ccy': 'USDC'})
        with locks[symbol][strategy]:
            p = positions[symbol][strategy]
            p['active']      = True
            p['side']        = 'short'
            p['entry_price'] = price
            p['quantity']    = qty
            p['lowest']      = price
            p['trail_sl']    = sl_price
            p['phase']       = 1
            p['open_time']   = time.time()
            p['candles']     = 0
            p['tp']          = tp_price
        log.info(f"SHORT nyitva [{symbol}/{strategy}]: {price} | Qty: {qty} | SL: {sl_price} | TP: {tp_price}")
        save_positions()
        return True
    except Exception as e:
        log.error(f"Short hiba [{symbol}/{strategy}]: {e}")
        return False

def close_position(symbol, strategy, reason=""):
    lock = locks[symbol][strategy]
    with lock:
        p = positions[symbol][strategy]
        if not p['active']:
            return
        p['active']   = False
        entry         = p['entry_price']
        qty           = p['quantity']
        side          = p['side']
    try:
        side_str = 'sell' if side == 'long' else 'buy'
        client.create_order(symbol, 'market', side_str, qty,
                             params={'tdMode': 'isolated', 'ccy': 'USDC', 'reduceOnly': True})
    except Exception as e:
        log.error(f"Zaras API hiba [{symbol}]: {e}")
        with lock:
            positions[symbol][strategy]['active'] = True
        return
    exit_price = get_price(symbol)
    ct_size = get_contract_size(symbol)
    btc_qty = qty * ct_size
    pnl = (exit_price - entry) * btc_qty if side == 'long' else (entry - exit_price) * btc_qty
    log.info(f"Pozicio zarva [{symbol}/{strategy}] ({reason}) | PnL: {pnl:.2f} USDC")
    db.log_trade(side, entry, exit_price, qty, round(pnl, 4), reason, symbol=symbol)
    with lock:
        positions[symbol][strategy] = _empty_position()
    save_positions()

# ── Trailing stop loop ────────────────────────────────────────

def update_trailing(symbol, strategy):
    with locks[symbol][strategy]:
        p = positions[symbol][strategy]
        if not p['active']:
            return
        price_entry = p['entry_price']
        side        = p['side']
        phase       = p['phase']

    price = get_price(symbol)
    if price <= 0:
        return

    with locks[symbol][strategy]:
        elapsed             = time.time() - positions[symbol][strategy]['open_time']
        positions[symbol][strategy]['candles'] = int(elapsed / CANDLE_SEC)
        candles             = positions[symbol][strategy]['candles']

    if candles >= MAX_CANDLES:
        p = positions[symbol][strategy]
        price = get_price(symbol)
        tp = p.get('tp')
        entry = p.get('price_entry', price)
        direction = p.get('direction', 'long')
        near_tp = False
        if tp and entry and tp != entry:
            if direction == 'long':
                progress = (price - entry) / (tp - entry) if tp > entry else 0
            else:
                progress = (entry - price) / (entry - tp) if entry > tp else 0
            near_tp = progress >= 0.70
        if near_tp:
            log.info(f"[{symbol}/{strategy}] TP kozel ({progress*100:.0f}%) - trailing 0.5% aktivalva, timeout kihagyva")
            with locks[symbol][strategy]:
                if direction == 'long':
                    new_trail = price * (1 - 0.005)
                    if new_trail > p['trail_sl']:
                        p['trail_sl'] = new_trail
                else:
                    new_trail = price * (1 + 0.005)
                    if new_trail < p['trail_sl'] or p['trail_sl'] == 0:
                        p['trail_sl'] = new_trail
        else:
            log.info(f"[{symbol}/{strategy}] {MAX_CANDLES} gyertya timeout - zaras")
            close_position(symbol, strategy, f"{MAX_CANDLES} gyertya timeout")
            return

    should_close  = False
    close_reason  = ""

    with locks[symbol][strategy]:
        p = positions[symbol][strategy]
        if side == 'long':
            profit_pct = (price - price_entry) / price_entry
            if profit_pct >= BREAKEVEN_TRIGGER and p['trail_sl'] < price_entry:
                p['trail_sl'] = price_entry
                log.info(f"Break-even LONG [{symbol}] | SL -> {price_entry:.4f}")
            if profit_pct >= PHASE2_TRIGGER and phase == 1:
                p['phase'] = 2
                phase = 2
            if price > p['highest']:
                p['highest'] = price
                callback = TRAIL_PHASE2 if phase == 2 else TRAIL_PHASE1
                new_sl = price * (1 - callback)
                if new_sl > p['trail_sl']:
                    p['trail_sl'] = new_sl
            if price <= p['trail_sl']:
                should_close = True
                close_reason = "Trailing SL"
            if p['tp'] and price >= p['tp']:
                should_close = True
                close_reason = "Take Profit"
        elif side == 'short':
            profit_pct = (price_entry - price) / price_entry
            if profit_pct >= BREAKEVEN_TRIGGER and p['trail_sl'] > price_entry:
                p['trail_sl'] = price_entry
                log.info(f"Break-even SHORT [{symbol}] | SL -> {price_entry:.4f}")
            if profit_pct >= PHASE2_TRIGGER and phase == 1:
                p['phase'] = 2
                phase = 2
            if price < p['lowest']:
                p['lowest'] = price
                callback = TRAIL_PHASE2 if phase == 2 else TRAIL_PHASE1
                new_sl = price * (1 + callback)
                if new_sl < p['trail_sl']:
                    p['trail_sl'] = new_sl
            if p['tp'] and price <= p['tp']:
                should_close = True
                close_reason = "Take Profit"
            if price >= p['trail_sl']:
                should_close = True
                close_reason = "Trailing SL"

    if should_close:
        close_position(symbol, strategy, close_reason)

# ── Webhook ───────────────────────────────────────────────────

last_webhook = {'time': None}
app = Flask(__name__)

def process_signal(data):
    if not data or 'signal' not in data:
        return {'error': 'Hibas adat'}, 400

    symbol = SYMBOLS[0]

    signal = data['signal'].lower()
    last_webhook['time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if signal == 'close':
        strat = data.get('strategy', None)
        if strat and strat in STRATEGIES:
            close_position(symbol, strat, "Manualis zaras")
        else:
            for s in STRATEGIES:
                close_position(symbol, s, "Manualis zaras")
        return {'status': 'ok'}, 200

    strategy = SIGNAL_MAP.get(signal)
    if not strategy:
        log.warning(f"Ismeretlen signal: {signal}")
        return {'error': f'Ismeretlen signal: {signal}'}, 400

    is_long = signal in ('long', 'bounce_long', 'ct_long', 'wr_long', 'bm_long')

    price = get_price(symbol)
    if price <= 0:
        return {'error': 'Nem sikerult az ar lekerdezese'}, 500

    with locks[symbol][strategy]:
        pos_active = positions[symbol][strategy]['active']
        pos_side   = positions[symbol][strategy]['side']
        pos_entry  = positions[symbol][strategy]['entry_price']

    if is_long:
        if pos_active and pos_side == 'long':
            current_pnl = (price - pos_entry) / pos_entry
            if current_pnl > 0:
                close_position(symbol, strategy, "Ujrapozicionajas")
                open_long(symbol, strategy, price, price * (1 - TRAIL_PHASE1 * 2))
            else:
                log.info(f"LONG mar nyitva [{symbol}/{strategy}], kihagyva")
        elif not pos_active:
            sl = float(data.get('sl', price * (1 - TRAIL_PHASE1 * 2)))
            tp = float(data.get('tp', price * (1 + TRAIL_PHASE1 * 4)))
            log.info(f"LONG jel [{symbol}/{strategy}] | SL: {sl:.4f} | TP: {tp:.4f}")
            open_long(symbol, strategy, price, sl, tp)
    else:
        if pos_active and pos_side == 'short':
            current_pnl = (pos_entry - price) / pos_entry
            if current_pnl > 0:
                close_position(symbol, strategy, "Ujrapozicionajas")
                open_short(symbol, strategy, price, price * (1 + TRAIL_PHASE1 * 2))
            else:
                log.info(f"SHORT mar nyitva [{symbol}/{strategy}], kihagyva")
        elif not pos_active:
            sl = float(data.get('sl', price * (1 + TRAIL_PHASE1 * 2)))
            tp = float(data.get('tp', price * (1 - TRAIL_PHASE1 * 4)))
            log.info(f"SHORT jel [{symbol}/{strategy}] | SL: {sl:.4f} | TP: {tp:.4f}")
            open_short(symbol, strategy, price, sl, tp)

    return {'status': 'ok'}, 200

@app.route('/webhook', methods=['POST'])
def webhook():
    token = request.headers.get('X-Webhook-Secret', '')
    if WEBHOOK_SECRET and token != WEBHOOK_SECRET:
        log.warning("Jogosulatlan webhook!")
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    result, code = process_signal(data)
    return jsonify(result), code

@app.route('/status')
def status():
    result = {}
    for sym in SYMBOLS:
        price = get_price(sym)
        result[sym] = {}
        for strat in STRATEGIES:
            with locks[sym][strat]:
                p = dict(positions[sym][strat])
            p['current_price'] = price
            result[sym][strat] = p
    result['balance'] = get_balance()
    return jsonify(result)

@app.route('/dashboard')
def dashboard():
    from flask import render_template
    balance = get_balance()
    pos_list = []
    for sym in SYMBOLS:
        price = get_price(sym)
        for strat in STRATEGIES:
            with locks[sym][strat]:
                p = dict(positions[sym][strat])
            if p['active']:
                ct_size = get_contract_size(sym)
                btc_qty = p['quantity'] * ct_size
                if p['side'] == 'long':
                    unrealized = (price - p['entry_price']) * btc_qty
                else:
                    unrealized = (p['entry_price'] - price) * btc_qty
            else:
                unrealized = 0.0
            pos_list.append({
                'symbol':        sym,
                'strategy':      strat,
                'active':        p['active'],
                'side':          p['side'],
                'entry':         p['entry_price'],
                'current_price': price,
                'trail_sl':      p['trail_sl'],
                'phase':         p['phase'],
                'candles':       p['candles'],
                'unrealized':    round(unrealized, 4),
            })
    stats = db.get_stats()
    status_data = {
        'positions':    pos_list,
        'balance':      balance,
        'last_webhook': last_webhook['time'],
        'win_rate':     stats['win_rate'],
        'total_pnl':    stats['total_pnl'],
        'daily_pnl':    stats['daily_pnl'],
        'total_trades': stats['total_trades'],
        'bot_status':   'running'
    }
    trades = db.get_trades(limit=50)
    return render_template('dashboard.html', status=status_data, trades=trades)

# ── Monitor loop ──────────────────────────────────────────────

def monitor_loop():
    set_leverage_all()
    load_positions()
    log.info("Bot elindult - 5 symbol")
    while True:
        for sym in SYMBOLS:
            for strat in STRATEGIES:
                try:
                    update_trailing(sym, strat)
                except Exception as e:
                    log.error(f"Monitor hiba [{sym}/{strat}]: {e}")
        time.sleep(10)

if __name__ == '__main__':
    db.init_db()
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    log.info("Webhook szerver indul - port 80")
    app.run(host='0.0.0.0', port=80, threaded=True)
