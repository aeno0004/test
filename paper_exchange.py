import sqlite3
import os
import threading 
from datetime import datetime

class TradeDB:
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

class FuturesWallet:
    def __init__(self, initial_balance=10000000):
        self.initial_balance = initial_balance 
        self.balance = initial_balance
        self.position = None 
        self.db = TradeDB() 
        self.last_trade_id = None

    def get_balance(self):
        return self.balance
    
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

# ==========================================
# [추가됨] 백테스팅 전용 DB 클래스
# ==========================================
class BacktestDB:
    def __init__(self, db_name="backtest_results.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.lock = threading.Lock()
        self.create_tables()

    def create_tables(self):
        with self.lock:
            cursor = self.conn.cursor()
            
            # 1. 백테스트 실행 기록 (Runs)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    executed_at TEXT,
                    target_days REAL,
                    initial_balance REAL,
                    final_balance REAL,
                    roi REAL,
                    win_rate REAL,
                    total_trades INTEGER
                )
            ''')
            
            # 2. AI 판단 전수 기록 (Decisions)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    timestamp TEXT,
                    decision TEXT,
                    confidence REAL,
                    sl REAL,
                    tp REAL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
            ''')
            
            # 3. 체결된 매매 기록 (Trades)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    trade_time TEXT,
                    roi REAL,
                    pnl REAL,
                    reason TEXT,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
            ''')
            self.conn.commit()

    def save_results(self, summary, ai_results, trades):
        """백테스트 결과 전체를 저장"""
        with self.lock:
            cursor = self.conn.cursor()
            
            # (1) 실행 기록 저장
            cursor.execute('''
                INSERT INTO runs (executed_at, target_days, initial_balance, final_balance, roi, win_rate, total_trades)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                summary.get('days', 0),
                summary['initial_balance'],
                summary['final_balance'],
                summary['roi'],
                summary['win_rate'],
                len(trades)
            ))
            run_id = cursor.lastrowid
            
            # (2) AI 판단 모두 저장 (Bulk Insert)
            # ai_results는 {timestamp: {json}} 형태
            decision_data = []
            for ts, res in ai_results.items():
                decision_data.append((
                    run_id,
                    str(ts), # Timestamp -> String
                    res.get('decision', 'hold'),
                    res.get('confidence', 0),
                    res.get('sl', 0),
                    res.get('tp', 0)
                ))
            
            if decision_data:
                cursor.executemany('''
                    INSERT INTO decisions (run_id, timestamp, decision, confidence, sl, tp)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', decision_data)
                
            # (3) 체결 내역 저장
            trade_data = []
            for t in trades:
                trade_data.append((
                    run_id,
                    str(t['time']),
                    t['roi'],
                    t['pnl'],
                    t['reason']
                ))
            
            if trade_data:
                cursor.executemany('''
                    INSERT INTO trades (run_id, trade_time, roi, pnl, reason)
                    VALUES (?, ?, ?, ?, ?)
                ''', trade_data)
                
            self.conn.commit()
            return run_id
