import pyupbit
import pandas as pd
import google.generativeai as genai
import os
from dotenv import load_dotenv
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

load_dotenv()
API_KEYS = [os.getenv(f"GEMINI_API_KEY{i}") for i in range(1, 10) if os.getenv(f"GEMINI_API_KEY{i}")]
TICKER = "KRW-BTC"

# 백테스팅 설정 (5분봉, 3일치)
INTERVAL = "minute5"
TOTAL_DAYS = 3 

def calculate_indicators(df):
    """ brain.py와 동일한 지표 계산 로직 """
    df['MA5'] = df['close'].rolling(5).mean()
    df['MA20'] = df['close'].rolling(20).mean()
    df['BB_Mid'] = df['close'].rolling(20).mean()
    std = df['close'].rolling(20).std()
    df['BB_Up'] = df['BB_Mid'] + (std * 2)
    df['BB_Low'] = df['BB_Mid'] - (std * 2)
    
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    rs = up.ewm(com=13).mean() / down.ewm(com=13).mean()
    df['RSI'] = 100 - (100 / (1 + rs))
    
    exp12 = df['close'].ewm(span=12).mean()
    exp26 = df['close'].ewm(span=26).mean()
    df['MACD'] = exp12 - exp26
    return df

def analyze_chunk(chunk, api_key, idx):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
    logs = []
    
    print(f"▶️ 봇-{idx} 시작 ({len(chunk)}개 처리)")
    for i in range(len(chunk)):
        if i < 20: continue # 지표 계산 여유분 스킵

        slc = chunk.iloc[i-10:i+1]
        cols = ['open', 'high', 'low', 'close', 'volume', 'MA5', 'MA20', 'RSI', 'MACD', 'BB_Up', 'BB_Low']
        txt = slc[cols].tail(10).round(1).to_string()
        
        prompt = f"""
        너는 비트코인 단타 AI야. 5분봉 데이터와 지표를 보고 판단해.
        상황에 맞는 지표를 골라 써.
        [데이터] {txt}
        [출력] JSON: {{"decision": "buy/sell/hold", "reason": "...", "confidence": 0~100}}
        """
        try:
            res = model.generate_content(prompt)
            js = json.loads(res.text.replace("```json", "").replace("```", "").strip())
            logs.append({"time": slc.index[-1], "price": slc['close'].iloc[-1], **js})
            time.sleep(2) # 무료 키 속도 조절
        except:
            time.sleep(5)
    return logs

def run():
    print("🚀 백테스팅 데이터 수집 중...")
    df = pyupbit.get_ohlcv(TICKER, interval=INTERVAL, count=TOTAL_DAYS*24*12)
    df = calculate_indicators(df)
    
    # 데이터 분할 및 병렬 처리
    chunk_size = len(df) // len(API_KEYS)
    chunks = [df.iloc[i*chunk_size : (i+1)*chunk_size] for i in range(len(API_KEYS))]
    
    results = []
    with ThreadPoolExecutor(max_workers=len(API_KEYS)) as exe:
        futs = [exe.submit(analyze_chunk, chunks[i], API_KEYS[i], i+1) for i in range(len(API_KEYS))]
        for f in as_completed(futs): results.extend(f.result())
        
    results.sort(key=lambda x: x['time'])
    
    # 수익률 계산
    bal = 10000000
    coin = 0
    tx_cnt = 0
    
    print("\n📊 시뮬레이션 결과 집계 중...")
    for r in results:
        if r['decision'] == 'buy' and r['confidence'] >= 70 and bal > 5000:
            coin = (bal * 0.9995) / r['price']
            bal = 0
            tx_cnt += 1
            print(f"🔴 매수: {r['price']:,.0f} | {r['reason']}")
        elif r['decision'] == 'sell' and r['confidence'] >= 70 and coin > 0:
            bal = coin * r['price'] * 0.9995
            coin = 0
            tx_cnt += 1
            print(f"🔵 매도: {r['price']:,.0f} | {r['reason']}")
            
    final = bal + (coin * df['close'].iloc[-1])
    print(f"\n💰 최종 자산: {int(final):,}원 (수익률: {((final/10000000)-1)*100:.2f}%) / 매매 {tx_cnt}회")

if __name__ == "__main__":
    run()