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
        # 바이낸스 퍼블릭 API
        self.exchange = ccxt.binanceusdm({
            'enableRateLimit': True,
            'options': {'defaultType': 'future'}
        })

    def fetch_data(self, days, start_date=None):
        """바이낸스 선물 데이터 수집"""
        symbol = "BTC/USDT"
        timeframe = "5m"
        limit = 1500 
        
        all_ohlcv = []
        
        if start_date:
            try:
                dt_obj = datetime.strptime(start_date, "%Y-%m-%d")
                since = int(dt_obj.timestamp() * 1000)
            except ValueError:
                print("❌ 날짜 형식이 잘못되었습니다. (YYYY-MM-DD)")
                return pd.DataFrame()
        else:
            since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
        
        now = int(datetime.now().timestamp() * 1000)
        print(f"📥 데이터 수집 시작... Target: {datetime.fromtimestamp(since/1000)}")
        
        while since < now:
            try:
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit, since=since)
                if not ohlcv: break
                
                all_ohlcv.extend(ohlcv)
                last_timestamp = ohlcv[-1][0]
                since = last_timestamp + 300000 
                
                print(f"   -> {len(ohlcv)}개 수집 완료 (Last: {datetime.fromtimestamp(last_timestamp/1000)})")
                time.sleep(0.1)
                
                if start_date and len(all_ohlcv) * 5 > days * 1440: break

            except Exception as e:
                print(f"❌ 데이터 수집 오류: {e}")
                break
                
        df = pd.DataFrame(all_ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        if not df.empty:
            df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
            df.set_index('datetime', inplace=True)
            try:
                df = brain.calculate_indicators(df)
                df.dropna(inplace=True)
            except Exception as e:
                print(f"❌ 지표 계산 오류: {e}")
        
        return df

    def call_with_retry(self, model, prompt, worker_id):
        """
        [스마트 재시도 로직]
        429 오류(Resource Exhausted)가 발생하면 대기 시간을 늘려가며 재시도합니다.
        """
        max_retries = 5
        base_wait = 20  # 초기 대기 시간 20초
        
        for attempt in range(max_retries):
            try:
                response = model.generate_content(prompt)
                return response
            except Exception as e:
                err_msg = str(e)
                # 429 또는 Quota 관련 오류 체크
                if "429" in err_msg or "Resource has been exhausted" in err_msg or "quota" in err_msg.lower():
                    wait_time = base_wait * (2 ** attempt) # 20s, 40s, 80s... 점진적 증가
                    print(f"⚠️ Worker-{worker_id}: 할당량 초과(429). {wait_time}초 대기 후 재시도... (시도 {attempt+1}/{max_retries})")
                    time.sleep(wait_time)
                else:
                    # 그 외 오류(500 등)는 짧게 대기 후 재시도
                    print(f"⚠️ Worker-{worker_id} API Error: {err_msg}")
                    time.sleep(5)
                    if attempt == max_retries - 1: return None
        return None

    def analyze_chunk_strict(self, chunk, api_key, worker_id):
        # [사용자 요청] gemini-2.5-flash 유지
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        results = {}
        request_count = 0
        
        print(f"🧵 Worker-{worker_id} 시작 ({len(chunk)}개 처리 예정)")
        
        for idx, row in chunk.iterrows():
            if request_count >= 250:
                print(f"🛑 Worker-{worker_id} 안전을 위해 종료 (250회 도달)")
                break
            
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
            
            # [변경] 스마트 재시도 함수 사용
            response = self.call_with_retry(model, prompt, worker_id)
            
            if response:
                try:
                    text = response.text.replace("```json", "").replace("```", "").strip()
                    results[idx] = json.loads(text)
                    request_count += 1
                except:
                    pass # 파싱 에러 무시
            
            # 기본 대기: 너무 빠른 연속 요청 방지
            time.sleep(2) 
                
        return results

    def run(self, days, start_date=None, duration_minutes=None):
        # 1. 데이터 수집
        df = self.fetch_data(days, start_date)
        
        if df.empty:
            print("❌ 데이터 없음")
            return {"final_balance": self.initial_balance, "roi": 0, "win_rate": 0, "trades": [], "logs": []}

        if duration_minutes:
            end_dt = df.index[0] + timedelta(minutes=duration_minutes)
            df = df[df.index <= end_dt]
        
        print(f"📊 총 {len(df)}개 캔들 분석 시작 (Worker {len(self.api_keys)}명 투입)")
        
        # 2. 데이터 분할
        num_keys = len(self.api_keys)
        if num_keys == 0: return {}

        chunk_size = len(df) // num_keys + 1
        chunks = [df.iloc[i*chunk_size : (i+1)*chunk_size] for i in range(num_keys)]
        
        # 3. 병렬 실행
        ai_results = {}
        with ThreadPoolExecutor(max_workers=num_keys) as executor:
            futures = []
            for i in range(num_keys):
                if len(chunks[i]) > 0:
                    # [수정된 부분] 괄호와 인자가 정확히 들어간 라인
                    futures.append(executor.submit(self.analyze_chunk_strict, chunks[i], self.api_keys[i], i+1))
                    
                    # [변경] 쓰레드 시작 간격을 5초로 설정 (부하 분산)
                    print(f"⏳ Worker-{i+1} 준비 중... (5초 대기)")
                    time.sleep(5)
            
            for future in futures:
                try:
                    res = future.result()
                    ai_results.update(res)
                except Exception as e:
                    print(f"Worker Exception: {e}")

        # 4. 시뮬레이션 (기존과 동일)
        print("\n🚀 시뮬레이션 정산 시작...")
        balance = self.initial_balance
        position = None
        trades = []
        logs = []
        wins = 0
        total_trades = 0
        FEE_RATE = 0.0004
        
        for idx, row in df.iterrows():
            curr_price = row['close']
            
            # 청산
            if position:
                side = position['side']
                entry_price = position['entry_price']
                amount = position['amount']
                sl = position.get('sl')
                tp = position.get('tp')
                
                is_closed, reason = False, ""
                
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
                    balance += net_pnl + (amount * entry_price) 
                    
                    roi_trade = (net_pnl / (amount * entry_price)) * 100
                    trades.append({'time': idx, 'roi': roi_trade, 'pnl': net_pnl, 'reason': reason})
                    logs.append(f"[{idx}] ⚡ {side.upper()} 청산 ({reason}): {roi_trade:.2f}%")
                    
                    if net_pnl > 0: wins += 1
                    total_trades += 1
                    position = None
            
            # 진입
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
