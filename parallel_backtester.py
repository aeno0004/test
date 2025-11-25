import ccxt
import pandas as pd
import google.generativeai as genai
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import brain  # 기존 brain.py 활용 (지표 계산)

class Backtester:
    def __init__(self, api_keys, initial_balance=10000000):
        self.api_keys = api_keys
        self.initial_balance = initial_balance
        self.key_idx = 0
        self.lock = threading.Lock()
        
        # 바이낸스 퍼블릭 API (키 불필요)
        self.exchange = ccxt.binanceusdm() 

    def get_key(self):
        """API 키 라운드 로빈"""
        with self.lock:
            key = self.api_keys[self.key_idx]
            self.key_idx = (self.key_idx + 1) % len(self.api_keys)
            return key

    def fetch_data(self, days, start_date=None):
        """
        바이낸스 선물 데이터(BTC/USDT) 수집
        """
        # 바이낸스는 한 번에 최대 1500개 캔들 제공 (5분봉 기준 약 5일치)
        # 따라서 days가 길면 반복 호출 필요. 여기서는 단순화를 위해 최대치(1500) 혹은 반복 호출 로직 구현
        
        symbol = "BTC/USDT"
        timeframe = "5m"
        limit = 1500 # Max limit for Binance
        
        all_ohlcv = []
        
        # 시작 시간 계산 (밀리초)
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
                since = ohlcv[-1][0] + 300000 # 마지막 시간 + 5분
                time.sleep(0.1) # Rate limit 방지
                
                # 요청한 기간만큼만 수집하고 종료 (최적화)
                if start_date and len(all_ohlcv) * 5 > days * 1440: # 대략적인 개수 체크
                     break
            except Exception as e:
                print(f"⚠️ 데이터 수집 중 에러: {e}")
                break
                
        df = pd.DataFrame(all_ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        # 기존 brain.py의 지표 계산 로직 재사용
        # brain.py는 업비트 포맷(컬럼명)을 가정하므로 호환됨
        df = brain.calculate_indicators(df)
        
        # 결측치 제거
        df.dropna(inplace=True)
        return df

    def calculate_tech_score(self, row):
        """
        1차 필터링: AI에게 물어볼 가치가 있는 자리인지 점수 매기기
        """
        score = 0
        # 예시 알고리즘 (추후 사용자 정의 가능)
        # 1. 볼린저 밴드 이탈
        if row['close'] > row['BB_Up'] or row['close'] < row['BB_Low']: score += 30
        # 2. RSI 과매수/과매도
        if row['RSI'] > 70 or row['RSI'] < 30: score += 20
        # 3. 거래량 급증
        # (이전 20개 평균 거래량이 없어서 에러날 수 있으므로 try 처리하거나 미리 계산 필요)
        # 여기서는 단순화
        if row['RSI'] < 25 or row['RSI'] > 75: score += 10 # 극단적 RSI 가중치
        
        return score

    def ask_ai_decision(self, row_data, idx_str):
        """
        AI에게 매매 판단 요청 (기존 알고리즘 유지)
        """
        key = self.get_key()
        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel('gemini-1.5-flash')
            
            prompt = f"""
            Role: Bitcoin Futures Trading AI.
            Task: Analyze this 5m candle data and decide whether to enter a trade.
            
            Current Data: {row_data}
            
            Output Format (JSON):
            {{"decision": "long" or "short" or "hold", "confidence": 0-100, "sl": price, "tp": price, "reason": "short reason"}}
            """
            
            res = model.generate_content(prompt)
            text = res.text.replace("```json", "").replace("```", "").strip()
            return json.loads(text)
        except:
            return {"decision": "hold", "confidence": 0, "reason": "error"}

    def run(self, days, start_date=None, duration_minutes=None):
        # 1. 데이터 준비
        df = self.fetch_data(days, start_date)
        if duration_minutes:
            # 특정 기간으로 자르기 (start_date 기준)
            end_dt = df.index[0] + timedelta(minutes=duration_minutes)
            df = df[df.index <= end_dt]
            
        print(f"📊 총 {len(df)}개 캔들 분석 시작...")
        
        # 2. 필터링 및 AI 분석 (Parallel)
        ai_results = {}
        
        # AI 호출 대상 선정 (Tech Score 40점 이상만)
        # 람다 함수나 별도 함수로 apply 적용
        df['tech_score'] = df.apply(self.calculate_tech_score, axis=1)
        candidates = df[df['tech_score'] >= 40]
        
        print(f"🤖 AI 분석 대상: {len(candidates)}개 (전체 대비 {len(candidates)/len(df)*100:.1f}%)")
        
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            for idx, row in candidates.iterrows():
                # 데이터 포맷팅
                data_str = f"Time: {idx}, Close: {row['close']}, RSI: {row['RSI']:.1f}, BB_Up: {row['BB_Up']:.1f}, BB_Low: {row['BB_Low']:.1f}"
                futures[executor.submit(self.ask_ai_decision, data_str, str(idx))] = idx
            
            for f in as_completed(futures):
                timestamp = futures[f]
                try:
                    ai_results[timestamp] = f.result()
                except:
                    pass

        # 3. 순차 시뮬레이션 (Sequential Simulation)
        balance = self.initial_balance
        position = None
        trades = []
        logs = []
        
        wins = 0
        total_trades = 0
        
        # 수수료/슬리피지 설정
        FEE_RATE = 0.0004 # 0.04%
        
        for idx, row in df.iterrows():
            curr_price = row['close']
            
            # (1) 포지션 관리 (청산)
            if position:
                side = position['side']
                entry_price = position['entry_price']
                amount = position['amount']
                
                # PnL 계산 (롱: 오르면 이득, 숏: 내리면 이득)
                if side == 'long':
                    pnl_pct = (curr_price - entry_price) / entry_price
                else:
                    pnl_pct = (entry_price - curr_price) / entry_price
                
                # SL/TP 체크 (AI가 준 값 or 고정값)
                # 여기서는 기존 알고리즘의 유연성을 위해 고정 %로 단순화 (사용자 요청 시 AI값 연동 가능)
                # AI가 준 SL/TP가 있으면 쓰고, 없으면 기본값 적용 로직
                sl_price = position.get('sl')
                tp_price = position.get('tp')
                
                is_closed = False
                close_reason = ""
                
                # 조건부 청산
                if side == 'long':
                    if sl_price and curr_price <= sl_price: is_closed, close_reason = True, "SL"
                    elif tp_price and curr_price >= tp_price: is_closed, close_reason = True, "TP"
                else: # short
                    if sl_price and curr_price >= sl_price: is_closed, close_reason = True, "SL"
                    elif tp_price and curr_price <= tp_price: is_closed, close_reason = True, "TP"
                
                # 강제 트레일링 스탑 (수익 보존 로직 추가 시 여기 구현)
                # ...

                if is_closed:
                    # 정산
                    pnl_amount = (balance * pnl_pct) # 단순화된 계산 (레버리지 1배 가정)
                    # 실제로는 (진입금액 * pnl_pct) - 수수료
                    
                    # 정확한 정산 로직
                    trade_val = amount * curr_price
                    fee = trade_val * FEE_RATE
                    pnl_raw = (curr_price - entry_price) * amount if side == 'long' else (entry_price - curr_price) * amount
                    
                    net_pnl = pnl_raw - fee
                    balance += net_pnl + (amount * entry_price) # 원금 + 손익 회수 (마진 거래 방식에 따라 다름)
                    # 여기서는 현물기반 선물 시뮬레이션으로 잔고 갱신
                    # 진입 시 잔고 차감 방식이었다면:
                    # balance (보유 현금)는 진입 시 줄었으므로, 청산 시 판 돈이 들어옴
                    
                    roi_trade = (net_pnl / (amount * entry_price)) * 100
                    
                    trades.append({'time': idx, 'pnl': net_pnl, 'roi': roi_trade, 'reason': close_reason})
                    logs.append(f"[{idx}] ⚡ {side.upper()} 청산 ({close_reason}): {roi_trade:.2f}% ({int(net_pnl)}원)")
                    
                    if net_pnl > 0: wins += 1
                    total_trades += 1
                    position = None
            
            # (2) 신규 진입 (AI 결과 확인)
            if position is None and idx in ai_results:
                ai_res = ai_results[idx]
                decision = ai_res.get('decision', 'hold').lower()
                conf = ai_res.get('confidence', 0)
                
                if decision in ['long', 'short'] and conf >= 70:
                    # 진입 실행
                    invest_amount = balance * 0.98 # 몰빵 방지하려면 여기서 0.2 등으로 수정
                    # 사용자가 '기존 알고리즘 유지'라 했으므로 0.98 유지하되, 리스크 관리를 위해 조절 가능
                    
                    entry_amount = invest_amount / curr_price
                    balance -= invest_amount # 현금 투입
                    
                    # AI가 제안한 SL/TP가 없으면 기본값 (ATR 등) 적용 가능하나 여기서는 AI 값 신뢰
                    sl = ai_res.get('sl')
                    tp = ai_res.get('tp')
                    
                    # AI가 값을 안 줬을 경우 대비 안전장치 (기본 2% 손절)
                    if not sl:
                        sl = curr_price * 0.98 if decision == 'long' else curr_price * 1.02
                    
                    position = {
                        'side': decision,
                        'entry_price': curr_price,
                        'amount': entry_amount,
                        'sl': sl,
                        'tp': tp
                    }
                    logs.append(f"[{idx}] 🚀 {decision.upper()} 진입 (Conf: {conf}%)")

        # 최종 결과 반환
        final_roi = ((balance / self.initial_balance) - 1) * 100
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
        
        return {
            "final_balance": balance,
            "roi": final_roi,
            "win_rate": win_rate,
            "trades": trades,
            "logs": logs
        }
