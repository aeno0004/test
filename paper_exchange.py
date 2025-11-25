import sqlite3
import os
import threading 
from datetime import datetime

class TradeDB:
    # (기존 코드와 동일)
    def __init__(self, db_name="trading_bot.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.lock = threading.Lock()
        self.create_tables()

    def create_tables(self):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    side TEXT,
                    entry_price REAL,
                    exit_price REAL,
                    amount REAL,
                    pnl REAL,
                    profit_rate REAL,
                    fee REAL,
                    reason TEXT,
                    entry_time TEXT,
                    exit_time TEXT,
                    ai_analysis TEXT
                )
            ''')
            self.conn.commit()

    def log_trade(self, trade_data):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                INSERT INTO trades (side, entry_price, exit_price, amount, pnl, profit_rate, fee, reason, entry_time, exit_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                trade_data['side'], trade_data['entry'], trade_data['exit'], trade_data['amount'],
                trade_data['pnl'], trade_data['profit_rate'], trade_data['fee'], trade_data['reason'],
                trade_data['entry_time'], trade_data['exit_time']
            ))
            self.conn.commit()
            return cursor.lastrowid 

    # (나머지 DB 메서드들 기존 유지...)

class FuturesWallet:
    def __init__(self, initial_balance=10000000):
        self.initial_balance = initial_balance # ROI 계산용 원금 저장
        self.balance = initial_balance
        self.position = None 
        self.db = TradeDB() 
        self.last_trade_id = None

    def get_balance(self):
        return self.balance
    
    # [NEW] 대쉬보드용 미실현 손익 계산 함수
    def get_unrealized_pnl(self, current_price):
        if not self.position:
            return 0
        pos = self.position
        if pos['type'] == 'long':
            return (current_price - pos['entry_price']) * pos['amount']
        else:
            return (pos['entry_price'] - current_price) * pos['amount']

    def enter_position(self, side, entry_price, amount_krw, sl, tp):
        if self.position is not None:
            return {"status": "fail", "msg": "Position already open"}
        if self.balance < amount_krw:
            return {"status": "fail", "msg": "Insufficient balance"}

        # Binance Futures Taker Fee 0.04%
        fee = amount_krw * 0.0004
        self.balance -= fee 
        coin_amount = amount_krw / entry_price
        
        self.position = {
            'type': side,
            'entry_price': entry_price,
            'amount': coin_amount, 
            'invested_krw': amount_krw,
            'sl': sl,
            'tp': tp,
            'entry_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        return {
            "status": "success",
            "side": side,
            "price": entry_price,
            "sl": sl,
            "tp": tp,
            "fee": fee
        }

    def close_position(self, exit_price, reason="signal"):
        if self.position is None: return None
        pos = self.position
        side = pos['type']
        amount = pos['amount']
        entry = pos['entry_price']
        
        if side == 'long': pnl = (exit_price - entry) * amount
        else: pnl = (entry - exit_price) * amount

        # Exit Fee 0.04%
        position_value = exit_price * amount
        fee = position_value * 0.0004
        final_payout = pnl - fee
        self.balance += final_payout 
        profit_rate = (final_payout / pos['invested_krw']) * 100

        result = {
            "status": "closed",
            "side": side,
            "entry": entry,
            "exit": exit_price,
            "amount": amount,
            "pnl": final_payout,
            "profit_rate": profit_rate,
            "fee": fee,
            "reason": reason,
            "entry_time": pos['entry_time'],
            "exit_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        self.last_trade_id = self.db.log_trade(result)
        self.position = None
        return result

    # (기존 update 함수는 main.py에서 직접 제어하므로 필수 아님, 유지해도 됨)
