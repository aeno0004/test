import pyupbit
import pandas as pd

def calculate_indicators(df):
    """
    [공통 함수] 보조지표 계산 (RSI, MACD, BB, MA)
    모든 모듈에서 이 함수를 가져다 쓰면 됩니다.
    """
    # 1. 이동평균선
    df['MA5'] = df['close'].rolling(5).mean()
    df['MA20'] = df['close'].rolling(20).mean()
    
    # 2. RSI (상대강도지수)
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    rs = up.ewm(com=13).mean() / down.ewm(com=13).mean()
    df['RSI'] = 100 - (100 / (1 + rs))

    # 3. MACD
    exp12 = df['close'].ewm(span=12).mean()
    exp26 = df['close'].ewm(span=26).mean()
    df['MACD'] = exp12 - exp26
    
    # 4. 볼린저 밴드
    df['BB_Mid'] = df['close'].rolling(20).mean()
    std = df['close'].rolling(20).std()
    df['BB_Up'] = df['BB_Mid'] + (std * 2)
    df['BB_Low'] = df['BB_Mid'] - (std * 2)
    
    return df

def get_ohlcv_data(ticker="KRW-BTC", interval="minute5", count=50):
    """캔들 데이터 조회 및 지표 계산"""
    try:
        df = pyupbit.get_ohlcv(ticker, interval=interval, count=count)
        if df is None: return None
        return calculate_indicators(df)
    except Exception as e:
        print(f"❌ 데이터 조회 실패: {e}")
        return None

def get_market_data(ticker="KRW-BTC", interval="minute5", count=50):
    """
    (구형 호환용) 텍스트 포맷 데이터 반환
    main.py의 '자산' 명령어 등에서 사용
    """
    df = get_ohlcv_data(ticker, interval, count)
    if df is None: return None, None

    # 출력용 데이터 정리
    cols = ['open', 'high', 'low', 'close', 'volume', 'MA5', 'MA20', 'RSI', 'MACD', 'BB_Up', 'BB_Low']
    recent_df = df[cols].tail(10).round(1)
    
    return recent_df.to_string(), df['close'].iloc[-1]