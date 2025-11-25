import sqlite3
import os
import threading 
from datetime import datetime

class TradeDB:
    """매매 기록 및 AI 분석을 저장하는 SQLite 핸들러"""
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

    def update_analysis(self, trade_id, analysis_text):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('UPDATE trades SET ai_analysis = ? WHERE id = ?', (analysis_text, trade_id))
            self.conn.commit()

    def get_recent_losses_feedback(self, limit=3):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT entry_time, side, reason, ai_analysis 
                FROM trades 
                WHERE pnl < 0 AND ai_analysis IS NOT NULL 
                ORDER BY id DESC LIMIT ?
            ''', (limit,))
            rows = cursor.fetchall()
            if not rows: return "No recent analyzed losses."
            feedback = ""
            for row in rows:
                feedback += f"- [RECENT] {row[0]} {row[1].upper()} Loss: {row[3]}\n"
            return feedback

    def get_worst_losses_feedback(self, limit=3):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('''
                SELECT entry_time, side, reason, ai_analysis, pnl
                FROM trades 
                WHERE pnl < 0 AND ai_analysis IS NOT NULL 
                ORDER BY pnl ASC LIMIT ?
            ''', (limit,))
            rows = cursor.fetchall()
            if not rows: return "No major losses recorded yet."
            feedback = ""
            for row in rows:
                feedback += f"- [WORST] {row[0]} {row[1].upper()} (PnL: {int(row[4])}): {row[3]}\n"
            return feedback

    def get_recent_trades_str(self, limit=5):
        with self.lock:
            cursor = self.conn.cursor()
            cursor.execute('SELECT side, entry_price, exit_price, profit_rate, reason, pnl FROM trades ORDER BY id DESC LIMIT ?', (limit,))
            rows = cursor.fetchall()
            if not rows: return "No previous trades."
            summary = ""
            for row in rows:
                result = "WIN" if row[5] > 0 else "LOSS"
                summary += f"[{result}] {row[0].upper()} | PnL: {row[3]:.2f}% | Reason: {row[4]}\n"
            return summary

class FuturesWallet:
    def __init__(self, initial_balance=10000000):
        self.balance = initial_balance
        self.position = None 
        self.db = TradeDB() 
        self.last_trade_id = None

    def get_balance(self):
        return self.balance
    
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

    def update(self, high, low):
        if self.position is None: return None
        pos = self.position
        sl = pos['sl']
        tp = pos['tp']
        
        if pos['type'] == 'long' and low <= sl:
            return self.close_position(sl, reason="Stop Loss")
        elif pos['type'] == 'short' and high >= sl:
            return self.close_position(sl, reason="Stop Loss")
        if pos['type'] == 'long' and high >= tp:
            return self.close_position(tp, reason="Take Profit")
        elif pos['type'] == 'short' and low <= tp:
            return self.close_position(tp, reason="Take Profit")
        return None