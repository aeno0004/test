import ccxt
import pandas as pd
import google.generativeai as genai
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import brain  # 지표 계산용

class Backtester:
    def __init__(self, api_keys, initial_balance=10000000):
        self.api_keys = api_keys
        self.initial_balance = initial_balance
        # 바이낸스 퍼블릭 API (데이터 수집용, 키 불필요)
        self.exchange = ccxt.binanceusdm() 

    def fetch_data(self, days, start_date=None):
        """바이낸스 선물 데이터 수집 (CCXT 사용)"""
        symbol = "BTC/USDT"
        timeframe = "5m"
        limit = 1500 # Binance Max
        
        all_ohlcv = []
        
        if start_date:
            since = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp() * 1000)
        else:
            since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
        
        now = int(datetime.now().timestamp() * 1000)
        
        print(f"📥 바이낸스 데이터 수집 중... (Start: {datetime.fromtimestamp(since/1000)})")
        
        while since < now:
            try:
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit, since=since)
                if not ohlcv: break
                
                all_ohlcv.extend(ohlcv)
                since = ohlcv[-1][0] + 300000 # +5분
                time.sleep(0.1)
                
                # 요청 기간 충족 시 조기 종료
                if start_date and len(all_ohlcv) * 5 > days * 1440:
                     break
            except Exception as e:
                print(f"⚠️ 데이터 수집 에러: {e}")
                break
                
        df = pd.DataFrame(all_ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        # 지표 계산 (brain.py)
        df = brain.calculate_indicators(df)
        df.dropna(inplace=True)
        return df

    def analyze_chunk_strict(self, chunk, api_key, worker_id):
        """
        Gemini 2.5 Flash 무료 티어 제한 준수 작업자
        - RPM 10 (6초당 1회)
        - RPD 250 (일일 250회 제한)
        """
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash') # 2.5 버전 사용
        
        results = {}
        request_count = 0
        
        print(f"🧵 Worker-{worker_id} 시작 (할당량: {len(chunk)}개)")
        
        for idx, row in chunk.iterrows():
            # RPD(일일 제한) 체크
            if request_count >= 250:
                print(f"🛑 Worker-{worker_id} 일일 제한(250회) 도달로 중단.")
                break
            
            # 데이터 포맷팅
            data_str = (
                f"Time: {idx}, Close: {row['close']}, "
                f"RSI: {row['RSI']:.1f}, MACD: {row['MACD']:.1f}, "
                f"BB_Pos: {(row['close'] - row['BB_Low']) / (row['BB_Up'] - row['BB_Low']):.2f}"
            )
            
            prompt = f"""
            Role: Bitcoin Futures Trading AI.
            Task: Analyze this 5m candle data.
            Current Data: {data_str}
            
            Strict Output JSON:
            {{"decision": "long/short/hold", "confidence": 0-100, "sl": price, "tp": price}}
            """
            
            try:
                # API 호출
                start_time = time.time()
                response = model.generate_content(prompt)
                request_count += 1
                
                text = response.text.replace("```json", "").replace("```", "").strip()
                results[idx] = json.loads(text)
                
                # RPM 10 제한 준수 (6초 대기)
                # 처리 시간을 뺀 나머지만 대기하여 정확히 6초 간격 유지
                elapsed = time.time() - start_time
                sleep_time = max(0, 6.1 - elapsed) 
                time.sleep(sleep_time)
                
            except Exception as e:
                # 에러 발생 시(429 등) 더 길게 대기
                print(f"⚠️ Worker-{worker_id} Error: {e}")
                time.sleep(10)
                
        return results

    def run(self, days, start_date=None, duration_minutes=None):
        # 1. 데이터 수집
        df = self.fetch_data(days, start_date)
        if duration_minutes:
            end_dt = df.index[0] + timedelta(minutes=duration_minutes)
            df = df[df.index <= end_dt]
        
        print(f"📊 총 {len(df)}개 캔들 분석 시작 (필터링 없음)")
        
        # 2. 데이터 청크 분할 (키 개수만큼 등분)
        num_keys = len(self.api_keys)
        chunk_size = len(df) // num_keys + 1
        chunks = [df.iloc[i*chunk_size : (i+1)*chunk_size] for i in range(num_keys)]
        
        # RPD 경고
        max_capacity = num_keys * 250
        if len(df) > max_capacity:
            print(f"⚠️ 경고: 데이터({len(df)}개)가 일일 API 한도({max_capacity}개)를 초과합니다.")
            print(f"    초과분은 분석되지 않고 스킵됩니다.")
        
        # 3. 병렬 실행 (Strict Mode)
        ai_results = {}
        with ThreadPoolExecutor(max_workers=num_keys) as executor:
            futures = []
            for i in range(num_keys):
                if len(chunks[i]) > 0:
                    futures.append(executor.submit(self.analyze_chunk_strict, chunks[i], self.api_keys[i], i+1))
            
            for future in futures:
                try:
                    res = future.result()
                    ai_results.update(res)
                except Exception as e:
                    print(f"Worker Error: {e}")

        # 4. 순차 시뮬레이션
        print("\n🚀 시뮬레이션 정산 시작...")
        balance = self.initial_balance
        position = None
        trades = []
        logs = []
        wins = 0
        total_trades = 0
        FEE_RATE = 0.0004
        
        # 시뮬레이션 루프 (시간순)
        for idx, row in df.iterrows():
            curr_price = row['close']
            
            # (1) 청산 로직
            if position:
                side = position['side']
                entry_price = position['entry_price']
                amount = position['amount']
                
                # 손익 계산
                pnl_pct = (curr_price - entry_price) / entry_price if side == 'long' else (entry_price - curr_price) / entry_price
                
                # 조건 확인
                sl = position.get('sl')
                tp = position.get('tp')
                
                is_closed = False
                reason = ""
                
                if side == 'long':
                    if sl and curr_price <= sl: is_closed, reason = True, "SL"
                    elif tp and curr_price >= tp: is_closed, reason = True, "TP"
                else:
                    if sl and curr_price >= sl: is_closed, reason = True, "SL"
                    elif tp and curr_price <= tp: is_closed, reason = True, "TP"
                
                if is_closed:
                    pnl_money = (curr_price - entry_price) * amount if side == 'long' else (entry_price - curr_price) * amount
                    fee = curr_price * amount * FEE_RATE
                    net_pnl = pnl_money - fee
                    balance += net_pnl + (amount * entry_price) # 원금+손익
                    
                    roi_trade = (net_pnl / (amount * entry_price)) * 100
                    trades.append({'time': idx, 'roi': roi_trade, 'pnl': net_pnl, 'reason': reason})
                    logs.append(f"[{idx}] ⚡ {side.upper()} 청산 ({reason}): {roi_trade:.2f}% ({int(net_pnl):,}원)")
                    
                    if net_pnl > 0: wins += 1
                    total_trades += 1
                    position = None
            
            # (2) 진입 로직
            if position is None and idx in ai_results:
                res = ai_results[idx]
                decision = res.get('decision', 'hold').lower()
                conf = res.get('confidence', 0)
                
                if decision in ['long', 'short'] and conf >= 70:
                    invest = balance * 0.98
                    amount = invest / curr_price
                    balance -= invest
                    
                    sl = res.get('sl')
                    tp = res.get('tp')
                    # 안전장치: AI가 SL 안주면 2%
                    if not sl:
                        sl = curr_price * 0.98 if decision == 'long' else curr_price * 1.02
                    
                    position = {
                        'side': decision,
                        'entry_price': curr_price,
                        'amount': amount,
                        'sl': sl,
                        'tp': tp
                    }
                    logs.append(f"[{idx}] 🚀 {decision.upper()} 진입 (Conf: {conf}%)")

        final_roi = ((balance / self.initial_balance) - 1) * 100
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        
        return {
            "final_balance": balance,
            "roi": final_roi,
            "win_rate": win_rate,
            "trades": trades,
            "logs": logs
        }
            "trades": trades,
            "logs": logs
        }

