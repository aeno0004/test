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
        self.exchange = ccxt.binanceusdm({
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
            }
        })

    def fetch_data(self, days, start_date=None):
        """바이낸스 선물 데이터 수집 (CCXT 사용)"""
        symbol = "BTC/USDT"
        timeframe = "5m"
        limit = 1500 # Binance Max
        
        all_ohlcv = []
        
        # 시작 시간 계산
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
                # 데이터 조회
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit, since=since)
                
                if not ohlcv:
                    print("⚠️ 더 이상 가져올 데이터가 없습니다 (Empty response).")
                    break
                
                all_ohlcv.extend(ohlcv)
                
                # 다음 조회 시점 갱신 (마지막 데이터 시간 + 5분)
                last_timestamp = ohlcv[-1][0]
                since = last_timestamp + 300000 
                
                print(f"   -> {len(ohlcv)}개 수집 완료 (Last: {datetime.fromtimestamp(last_timestamp/1000)})")
                time.sleep(0.1)
                
                # 요청 기간 충족 시 조기 종료
                if start_date and len(all_ohlcv) * 5 > days * 1440:
                     break

            except Exception as e:
                print(f"❌ 데이터 수집 중 치명적 오류: {e}")
                print("💡 팁: 한국에서는 VPN을 켜야 바이낸스 접속이 될 수 있습니다.")
                break
                
        df = pd.DataFrame(all_ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        if not df.empty:
            df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
            df.set_index('datetime', inplace=True)
            
            # 지표 계산 (brain.py)
            try:
                df = brain.calculate_indicators(df)
                df.dropna(inplace=True)
            except Exception as e:
                print(f"❌ 지표 계산 오류: {e}")
        
        return df

    def analyze_chunk_strict(self, chunk, api_key, worker_id):
        """
        Gemini 2.5 Flash 무료 티어 제한 준수 작업자
        [수정됨] 6개 요청 후 1분 휴식 로직 적용
        """
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        results = {}
        request_count = 0   # 일일 총 요청 수
        batch_count = 0     # 배지(6개) 카운트
        
        print(f"🧵 Worker-{worker_id} 시작 ({len(chunk)}개)")
        
        for idx, row in chunk.iterrows():
            if request_count >= 250:
                print(f"🛑 Worker-{worker_id} 일일 제한(250회) 도달.")
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
                # API 요청
                response = model.generate_content(prompt)
                request_count += 1
                batch_count += 1
                
                text = response.text.replace("```json", "").replace("```", "").strip()
                results[idx] = json.loads(text)
                
                # ---------------------------------------------------------
                # [로직 변경] 6개 요청(Batch) 처리 후 1분 대기
                # ---------------------------------------------------------
                if batch_count >= 6:
                    print(f"⏳ Worker-{worker_id}: 6개 처리 완료 -> 1분 휴식 (Rate Limit 준수)")
                    time.sleep(60)
                    batch_count = 0  # 카운터 리셋
                else:
                    # 6개가 안 찼더라도, 연속 요청 간 최소 1초 대기 (순간 과부하 방지)
                    time.sleep(1)
                
            except Exception as e:
                print(f"⚠️ Worker-{worker_id} API Error: {e}")
                # 429 에러 등이 발생했을 때도 안전하게 1분 대기
                time.sleep(60)
                
        return results

    def run(self, days, start_date=None, duration_minutes=None):
        # 1. 데이터 수집
        df = self.fetch_data(days, start_date)
        
        if df.empty:
            print("❌ 분석할 데이터가 없습니다. (수집 실패)")
            return {
                "final_balance": self.initial_balance,
                "roi": 0,
                "win_rate": 0,
                "trades": [],
                "logs": ["❌ 데이터 수집 실패: VPN을 확인하거나 날짜를 다시 확인해주세요."]
            }

        if duration_minutes:
            end_dt = df.index[0] + timedelta(minutes=duration_minutes)
            df = df[df.index <= end_dt]
        
        print(f"📊 총 {len(df)}개 캔들 분석 시작 (전수 조사)")
        
        # 2. 데이터 청크 분할
        num_keys = len(self.api_keys)
        if num_keys == 0:
            print("❌ API 키가 없습니다.")
            return {"final_balance": 0, "roi": 0, "win_rate": 0, "trades": [], "logs": ["API 키 없음"]}

        chunk_size = len(df) // num_keys + 1
        chunks = [df.iloc[i*chunk_size : (i+1)*chunk_size] for i in range(num_keys)]
        
        # 3. 병렬 실행
        ai_results = {}
        with ThreadPoolExecutor(max_workers=num_keys) as executor:
            futures = []
            print(f"🚀 {num_keys}개의 키로 병렬 분석 시작 (시차 적용)")
            
            for i in range(num_keys):
                if len(chunks[i]) > 0:
                    futures.append(executor.submit(self.analyze_chunk_strict, chunks[i], self.api_keys[i], i+1))
                    # [추가] Worker들이 동시에 시작해서 API를 폭격하는 것을 방지 (2초 시차)
                    time.sleep(2)
            
            for future in futures:
                try:
                    res = future.result()
                    ai_results.update(res)
                except Exception as e:
                    print(f"Worker Exception: {e}")

        # 4. 순차 시뮬레이션
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
            
            # (1) 청산 로직
            if position:
                side = position['side']
                entry_price = position['entry_price']
                amount = position['amount']
                
                # 조건 확인
                sl = position.get('sl')
                tp = position.get('tp')
                
                is_closed = False
                reason = ""
                
                if side == 'long':
                    if sl and curr_price <= sl: is_closed, reason = True, "SL"
                    elif tp and curr_price >= tp: is_closed, reason = True, "TP"
                else: # short
                    if sl and curr_price >= sl: is_closed, reason = True, "SL"
                    elif tp and curr_price <= tp: is_closed, reason = True, "TP"
                
                if is_closed:
                    pnl_money = (curr_price - entry_price) * amount if side == 'long' else (entry_price - curr_price) * amount
                    fee = curr_price * amount * FEE_RATE
                    net_pnl = pnl_money - fee
                    balance += net_pnl + (amount * entry_price) 
                    
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
