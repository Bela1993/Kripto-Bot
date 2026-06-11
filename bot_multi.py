BREAKEVEN_TRIGGER = 0.003
import os
import time
import logging
import math
import threading
import json
from binance.client import Client
from binance.enums import *
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
API_KEY        = os.getenv('BINANCE_API_KEY')
API_SECRET     = os.getenv('BINANCE_API_SECRET')
TESTNET        = os.getenv('TESTNET', 'true').lower() == 'true'
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '')

SYMBOLS = ['BTCUSDT', 'SOLUSDT', 'XRPUSDT', 'AVAXUSDT', 'SUIUSDT']
LEVERAGE       = 10
RISK_PCT       = 0.02
TRAIL_PHASE1   = 0.005
TRAIL_PHASE2   = 0.002
PHASE2_TRIGGER = 0.005
MAX_CANDLES    = 8
CANDLE_SEC     = 15 * 60

if TESTNET:
    client       = Client(API_KEY, API_SECRET, testnet=True)
    price_client = Client('', '')
    log.info("TESTNET modban fut! (valos arakkal)")
else:
    client       = Client(API_KEY, API_SECRET)
    price_client = client
    log.info("ELES modban fut!")

def _empty_position():
    return {
        'active': False, 'side': None, 'entry_price': 0.0,
        'quantity': 0.0, 'highest': 0.0, 'lowest': 999999.0,
        'trail_sl': 0.0, 'phase': 1, 'open_time': 0,
        'candles': 0, 'tp': None
    }

positions = {sym: _empty_position() for sym in SYMBOLS}
locks     = {sym: threading.Lock() for sym in SYMBOLS}

# ── Segédfüggvények ───────────────────────────────────────────

def get_balance():
    try:
        for b in client.futures_account_balance():
            if b['asset'] == 'USDT':
                return float(b['balance'])
    except Exception as e:
        log.error(f"Egyenleg hiba: {e}")
    return 0.0

def get_price(symbol):
    try:
        ticker = price_client.futures_symbol_ticker(symbol=symbol)
        return float(ticker['price'])
    except Exception as e:
        log.error(f"Ar hiba [{symbol}]: {e}")
    return 0.0

def set_leverage_all():
    for sym in SYMBOLS:
        try:
            client.futures_change_leverage(symbol=sym, leverage=LEVERAGE)
            log.info(f"Leverage {LEVERAGE}x beallitva: {sym}")
        except Exception as e:
            log.error(f"Leverage hiba [{sym}]: {e}")

_lot_size_cache = {}

def get_lot_size(symbol):
    if symbol in _lot_size_cache:
        return _lot_size_cache[symbol]
    try:
        info = client.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        step = float(f['stepSize'])
                        _lot_size_cache[symbol] = step
                        return step
    except Exception as e:
        log.error(f"LOT_SIZE hiba [{symbol}]: {e}")
    return 0.001

def round_to_lot_size(quantity, step_size):
    precision = max(0, round(-math.log10(step_size)))
    return round(math.floor(quantity / step_size) * step_size, precision)

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

def open_long(symbol, price, sl_price, tp_price=None):
    try:
        balance  = get_balance()
        raw_qty  = calc_quantity(price, sl_price, balance)
        step     = get_lot_size(symbol)
        qty      = round_to_lot_size(raw_qty, step)
        if qty <= 0:
            log.error(f"Hibas pozicio meret [{symbol}]!")
            return False
        client.futures_create_order(symbol=symbol, side=SIDE_BUY,
                                    type=ORDER_TYPE_MARKET, quantity=qty)
        with locks[symbol]:
            p = positions[symbol]
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
        log.info(f"LONG nyitva [{symbol}]: {price} | Qty: {qty} | SL: {sl_price} | TP: {tp_price}")
        save_positions()
        return True
    except Exception as e:
        log.error(f"Long hiba [{symbol}]: {e}")
        return False

def open_short(symbol, price, sl_price, tp_price=None):
    try:
        balance  = get_balance()
        raw_qty  = calc_quantity(price, sl_price, balance)
        step     = get_lot_size(symbol)
        qty      = round_to_lot_size(raw_qty, step)
        if qty <= 0:
            log.error(f"Hibas pozicio meret [{symbol}]!")
            return False
        client.futures_create_order(symbol=symbol, side=SIDE_SELL,
                                    type=ORDER_TYPE_MARKET, quantity=qty)
        with locks[symbol]:
            p = positions[symbol]
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
        log.info(f"SHORT nyitva [{symbol}]: {price} | Qty: {qty} | SL: {sl_price} | TP: {tp_price}")
        save_positions()
        return True
    except Exception as e:
        log.error(f"Short hiba [{symbol}]: {e}")
        return False

def close_position(symbol, reason=""):
    lock = locks[symbol]
    with lock:
        p = positions[symbol]
        if not p['active']:
            return
        p['active']   = False
        side_close    = SIDE_SELL if p['side'] == 'long' else SIDE_BUY
        entry         = p['entry_price']
        qty           = p['quantity']
        side          = p['side']
    try:
        client.futures_create_order(symbol=symbol, side=side_close,
                                    type=ORDER_TYPE_MARKET, quantity=qty)
    except Exception as e:
        log.error(f"Zaras API hiba [{symbol}]: {e}")
        with lock:
            positions[symbol]['active'] = True
        return
    exit_price = get_price(symbol)
    pnl = (exit_price - entry) * qty if side == 'long' else (entry - exit_price) * qty
    log.info(f"Pozicio zarva [{symbol}] ({reason}) | PnL: {pnl:.2f} USDT")
    db.log_trade(side, entry, exit_price, qty, round(pnl, 4), reason, symbol=symbol)
    with lock:
        positions[symbol] = _empty_position()
    save_positions()

# ── Trailing stop loop ────────────────────────────────────────

def update_trailing(symbol):
    with locks[symbol]:
        p = positions[symbol]
        if not p['active']:
            return
        price_entry = p['entry_price']
        side        = p['side']
        phase       = p['phase']

    price = get_price(symbol)
    if price <= 0:
        return

    with locks[symbol]:
        elapsed             = time.time() - positions[symbol]['open_time']
        positions[symbol]['candles'] = int(elapsed / CANDLE_SEC)
        candles             = positions[symbol]['candles']

    if candles >= MAX_CANDLES:
        log.info(f"[{symbol}] {MAX_CANDLES} gyertya timeout - zaras")
        close_position(symbol, f"{MAX_CANDLES} gyertya timeout")
        return

    should_close  = False
    close_reason  = ""

    with locks[symbol]:
        p = positions[symbol]
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
        close_position(symbol, close_reason)

# ── Webhook ───────────────────────────────────────────────────

last_webhook = {'time': None}
app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    token = request.headers.get('X-Webhook-Secret', '')
    if WEBHOOK_SECRET and token != WEBHOOK_SECRET:
        log.warning("Jogosulatlan webhook!")
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    if not data or 'signal' not in data:
        return jsonify({'error': 'Hibas adat'}), 400

    raw_sym = data.get('symbol', 'BTCUSDT').upper().replace('/', '').replace('-', '')
    # BTCUSDT / BTC/USDT / BTCPERP mind elfogadott
    symbol = raw_sym if raw_sym in SYMBOLS else raw_sym + 'USDT'
    if symbol not in SYMBOLS:
        log.warning(f"Ismeretlen symbol: {symbol}")
        return jsonify({'error': f'Ismeretlen symbol: {symbol}'}), 400

    signal = data['signal'].lower()
    last_webhook['time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if signal == 'close':
        close_position(symbol, "Manualis zaras")
        return jsonify({'status': 'ok'})

    if signal in ('bounce_long',):
        signal = 'long'
    if signal in ('bounce_short',):
        signal = 'short'

    price = get_price(symbol)
    if price <= 0:
        return jsonify({'error': 'Nem sikerult az ar lekerdezese'}), 500

    with locks[symbol]:
        pos_active = positions[symbol]['active']
        pos_side   = positions[symbol]['side']
        pos_entry  = positions[symbol]['entry_price']

    if signal == 'long':
        if pos_active and pos_side == 'long':
            current_pnl = (price - pos_entry) / pos_entry
            if current_pnl > 0:
                close_position(symbol, "Ujrapozicionajas")
                open_long(symbol, price, price * (1 - TRAIL_PHASE1 * 2))
        elif not pos_active:
            sl = float(data.get('sl', price * (1 - TRAIL_PHASE1 * 2)))
            tp = float(data.get('tp', price * (1 + TRAIL_PHASE1 * 4)))
            log.info(f"LONG jel [{symbol}] | SL: {sl:.4f} | TP: {tp:.4f}")
            open_long(symbol, price, sl, tp)

    elif signal == 'short':
        if pos_active and pos_side == 'short':
            current_pnl = (pos_entry - price) / pos_entry
            if current_pnl > 0:
                close_position(symbol, "Ujrapozicionajas")
                open_short(symbol, price, price * (1 + TRAIL_PHASE1 * 2))
        elif not pos_active:
            sl = float(data.get('sl', price * (1 + TRAIL_PHASE1 * 2)))
            tp = float(data.get('tp', price * (1 - TRAIL_PHASE1 * 4)))
            log.info(f"SHORT jel [{symbol}] | SL: {sl:.4f} | TP: {tp:.4f}")
            open_short(symbol, price, sl, tp)

    return jsonify({'status': 'ok'}), 200

@app.route('/status')
def status():
    result = {}
    for sym in SYMBOLS:
        price = get_price(sym)
        with locks[sym]:
            p = dict(positions[sym])
        p['current_price'] = price
        result[sym] = p
    result['balance'] = get_balance()
    return jsonify(result)

@app.route('/dashboard')
def dashboard():
    from flask import render_template
    balance = get_balance()
    pos_list = []
    for sym in SYMBOLS:
        price = get_price(sym)
        with locks[sym]:
            p = dict(positions[sym])
        if p['active']:
            if p['side'] == 'long':
                unrealized = (price - p['entry_price']) * p['quantity']
            else:
                unrealized = (p['entry_price'] - price) * p['quantity']
        else:
            unrealized = 0.0
        pos_list.append({
            'symbol':        sym,
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
            try:
                update_trailing(sym)
            except Exception as e:
                log.error(f"Monitor hiba [{sym}]: {e}")
        time.sleep(10)

if __name__ == '__main__':
    db.init_db()
    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()
    log.info("Webhook szerver indul - port 80")
    app.run(host='0.0.0.0', port=80)
