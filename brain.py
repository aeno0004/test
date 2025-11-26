import pyupbit
import pandas as pd
import numpy as np

def calculate_indicators(df):
    """
    [전문가용] 보조지표 계산
    추가됨: EMA(50, 200), ATR(14), Volume Ratio, MACD Signal
    """
    # 1. 이동평균선 (SMA & EMA)
    df['MA5'] = df['close'].rolling(5).mean()
    df['MA20'] = df['close'].rolling(20).mean()
    df['EMA50'] = df['close'].ewm(span=50, adjust=False).mean()
    df['EMA200'] = df['close'].ewm(span=200, adjust=False).mean()
    
    # 2. RSI (상대강도지수)
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    rs = up.ewm(com=13).mean() / down.ewm(com=13).mean()
    df['RSI'] = 100 - (100 / (1 + rs))

    # 3. MACD (Signal 추가)
    exp12 = df['close'].ewm(span=12, adjust=False).mean()
    exp26 = df['close'].ewm(span=26, adjust=False).mean()
    df['MACD'] = exp12 - exp26
    df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
    
    # 4. 볼린저 밴드
    df['BB_Mid'] = df['close'].rolling(20).mean()
    std = df['close'].rolling(20).std()
    df['BB_Up'] = df['BB_Mid'] + (std * 2)
    df['BB_Low'] = df['BB_Mid'] - (std * 2)
    
    # 5. ATR (변동성 지표 - 손절/익절 기준용)
    # TR = Max(|High-Low|, |High-PrevClose|, |Low-PrevClose|)
    prev_close = df['close'].shift(1)
    tr1 = df['high'] - df['low']
    tr2 = (df['high'] - prev_close).abs()
    tr3 = (df['low'] - prev_close).abs()
    
    # Pandas Series끼리의 max 계산
    df['TR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df['ATR'] = df['TR'].rolling(window=14).mean()

    # 6. 거래량 비율 (20개 캔들 평균 대비 현재 거래량)
    df['vol_avg'] = df['volume'].rolling(window=20).mean()
    df['vol_ratio'] = df['volume'] / df['vol_avg']
    
    # 지표 계산으로 인한 결측치(NaN) 제거
    return df.dropna()

def get_ohlcv_data(ticker="KRW-BTC", interval="minute5", count=200):
    """캔들 데이터 조회 (구형 호환용)"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
        if df is None: return None
        return calculate_indicators(df)
    except Exception as e:
        print(f"❌ 데이터 조회 실패: {e}")
        return None
