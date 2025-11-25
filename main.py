import discord
from discord.ext import commands, tasks
import os
import sys
import asyncio
import pyupbit
import pandas as pd
import json
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

# 모듈 임포트
from brain import get_market_data 
from paper_exchange import FuturesWallet 
import google.generativeai as genai

# ==========================================
# 0. 전역 변수 및 설정
# ==========================================
backtest_status = {}
dashboard_msg = None  
last_ai_analysis_time = 0 

CONFIG_FILE = 'config.json'

if not os.path.exists(CONFIG_FILE):
    print(f"❌ 오류: '{CONFIG_FILE}' 파일이 없습니다.")
    sys.exit()

try:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
        
    TOKEN = config['DISCORD_TOKEN']
    DASHBOARD_ID = int(config.get('DISCORD_DASHBOARD_ID', config.get('DISCORD_CHANNEL_ID', 0)))
    SIGNAL_ID = int(config.get('DISCORD_SIGNAL_ID', DASHBOARD_ID))
    EXPLAIN_ID = int(config.get('DISCORD_EXPLAIN_ID', DASHBOARD_ID))
    TARGET_CHANNEL_ID = DASHBOARD_ID
    API_KEYS = config['GEMINI_API_KEYS'] 
except Exception as e:
    print(f"❌ 설정 로드 실패: {e}")
    sys.exit()

# ==========================================
# 1. 봇 & 지갑 초기화
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

real_wallet = FuturesWallet() # 선물 지갑
auto_active = False

# ==========================================
# 2. 유틸리티 함수
# ==========================================
def calculate_indicators(df):
    """보조지표 계산"""
    df['MA5'] = df['close'].rolling(5).mean()
    df['MA20'] = df['close'].rolling(20).mean()
    
    delta = df['close'].diff()
    up = delta.clip(lower=0)
    down = -1 * delta.clip(upper=0)
    rs = up.ewm(com=13).mean() / down.ewm(com=13).mean()
    df['RSI'] = 100 - (100 / (1 + rs))
    
    exp12 = df['close'].ewm(span=12).mean()
    exp26 = df['close'].ewm(span=26).mean()
    df['MACD'] = exp12 - exp26
    
    df['BB_Mid'] = df['close'].rolling(20).mean()
    std = df['close'].rolling(20).std()
    df['BB_Up'] = df['BB_Mid'] + (std * 2)
    df['BB_Low'] = df['BB_Mid'] - (std * 2)
    return df

def translate_to_korean(text):
    """휴식 중인 봇(마지막 키)을 사용하여 한글 번역"""
    if not text: return ""
    try:
        genai.configure(api_key=API_KEYS[-1])
        model = genai.GenerativeModel('gemini-2.5-flash')
        response = model.generate_content(f"Translate this trading analysis into natural Korean:\n\n{text}")
        return response.text.strip()
    except Exception as e:
        print(f"⚠️ Translation Failed: {e}")
        return text 

def analyze_loss_reason(df, trade_info):
    """손실 원인 분석 (Reflection)"""
    cols = ['open', 'high', 'low', 'close', 'volume', 'MA5', 'MA20', 'RSI', 'MACD', 'BB_Up', 'BB_Low']
    txt = df[cols].tail(10).round(1).to_string()
    
    prompt = f"""
    Role: Senior Crypto Analyst.
    Task: Analyze WHY this trade failed (Loss).
    [Trade] {trade_info['side'].upper()} | Entry: {trade_info['entry']} | Exit: {trade_info['exit']} | Reason: {trade_info['reason']}
    [Chart]
    {txt}
    Output: One critical sentence on what went wrong (e.g., "Entered Long at BB resistance").
    """
    try:
        genai.configure(api_key=API_KEYS[-1]) # 마지막 키 사용
        model = genai.GenerativeModel('gemini-2.5-flash')
        res = model.generate_content(prompt)
        analysis = res.text.strip()
        print(f"😭 [Loss Analysis] {analysis}", flush=True)
        return analysis
    except Exception as e:
        print(f"⚠️ Analysis Error: {e}")
        return "Analysis failed."

def ask_ai_realtime(df_5m):
    """
    [실시간] 멀티 타임프레임 + 피드백 루프 AI 분석
    """
    # 1. 멀티 타임프레임 데이터 수집
    try:
        df_1h = pyupbit.get_ohlcv("KRW-BTC", interval="minute60", count=20)
        df_1h = calculate_indicators(df_1h)
        
        df_4h = pyupbit.get_ohlcv("KRW-BTC", interval="minute240", count=20)
        df_4h = calculate_indicators(df_4h)

        df_1d = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=20)
        df_1d = calculate_indicators(df_1d)
    except:
        print("⚠️ 추가 데이터 수집 실패, 5분봉만 사용")
        df_1h, df_4h, df_1d = df_5m, df_5m, df_5m

    # 2. 데이터 포맷팅
    cols = ['open', 'high', 'low', 'close', 'MA20', 'RSI', 'MACD', 'BB_Up', 'BB_Low']
    txt_5m = df_5m[cols].tail(10).round(1).to_string()
    txt_1h = df_1h[cols].tail(10).round(1).to_string()
    txt_4h = df_4h[cols].tail(10).round(1).to_string()
    txt_1d = df_1d[cols].tail(10).round(1).to_string()
    
    # 3. 과거 기록 및 교훈 호출 (DB)
    past_trades = real_wallet.db.get_recent_trades_str(limit=5)
    lessons = real_wallet.db.get_recent_losses_feedback(limit=3)
    worst_lessons = real_wallet.db.get_worst_losses_feedback(limit=3)

    prompt = f"""
    Role: Bitcoin Futures Trading AI (Professional Analyst).
    
    [EXPERIENCE & FEEDBACK]
    1. Recent Trades: {past_trades}
    2. Recent Lessons: {lessons}
    3. 💀 HALL OF SHAME (Never Repeat): {worst_lessons}
    
    [MULTI-TIMEFRAME ANALYSIS]
    1. Daily (Trend): {txt_1d}
    2. 4-Hour (Support/Res): {txt_4h}
    3. 1-Hour (Momentum): {txt_1h}
    4. 5-Min (Entry): {txt_5m}
    
    [RULES]
    1. Trend Filter: Do not trade against Daily/4H trend.
    2. RSI Divergence: Look for divergences on 1H/5M.
    3. Risk-Reward: TP distance must be >= 1.5x SL distance.
    
    Task: Decide Long/Short/Hold.
    Output (JSON): {{"decision": "long/short/hold", "sl": price, "tp": price, "reason": "...", "confidence": 0-100}}
    """
    
    # 키 로테이션 (마지막 키 제외)
    for i, key in enumerate(API_KEYS[:-1]): 
        try:
            genai.configure(api_key=key)
            model = genai.GenerativeModel('gemini-2.5-flash')
            res = model.generate_content(prompt)
            if not res.text: continue
            
            print(f"📩 [AI Raw] {res.text[:50]}...", flush=True)
            js = json.loads(res.text.replace("```json", "").replace("```", "").strip())
            print(f"🤖 [AI Decision] {js['decision'].upper()} ({js['confidence']}%)", flush=True)
            return js
        except Exception as e:
            print(f"⚠️ Key({i}) Error: {e}")
            time.sleep(1)
            continue
            
    return {"decision": "hold", "reason": "API Error", "confidence": 0}

def analyze_chunk_for_bot(chunk, api_key, idx):
    """백테스트용 분석 함수"""
    global backtest_status
    backtest_status[idx] = {"current": 0, "total": len(chunk)}
    
    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
    except Exception as e:
        print(f"❌ [Bot-{idx}] Init Error: {e}")
        return []

    logs = []
    
    for i in range(len(chunk)):
        backtest_status[idx]["current"] = i + 1
        if i < 15: continue 
        
        slc = chunk.iloc[i-10:i+1]
        cols = ['open', 'high', 'low', 'close', 'volume', 'MA5', 'MA20', 'RSI', 'MACD', 'BB_Up', 'BB_Low']
        txt = slc[cols].tail(10).round(1).to_string()
        
        prompt = f"""
        Role: Bitcoin Futures Trading AI.
        Task: Analyze 5m chart. Decide Long/Short/Hold with SL/TP.
        Data: {txt}
        Output (JSON): {{"decision": "long/short/hold", "sl": price, "tp": price, "reason": "...", "confidence": 0-100}}
        """
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                res = model.generate_content(prompt)
                if not res.text: raise ValueError("Empty")
                js = json.loads(res.text.replace("```json", "").replace("```", "").strip())
                
                logs.append({
                    "time": slc.index[-1], 
                    "price": slc['close'].iloc[-1],
                    "high": slc['high'].iloc[-1],
                    "low": slc['low'].iloc[-1],
                    **js
                })
                time.sleep(6) 
                break 
            except Exception as e:
                err = str(e)
                if "429" in err or "quota" in err:
                    print(f"🚦 [Bot-{idx}] 429 Rate Limit. Sleep 30s.")
                    time.sleep(30)
                else:
                    time.sleep(5)
                    break 
    
    return logs

# ==========================================
# 3. 봇 이벤트 & 커맨드
# ==========================================
@bot.event
async def on_ready():
    print(f"✅ {bot.user} 가동 | 채널: Dash({DASHBOARD_ID}) Sig({SIGNAL_ID}) Exp({EXPLAIN_ID})")

@bot.command(name="백테스트")
async def start_backtest(ctx, arg1: str, arg2: str = None):
    target_date_str = None
    fetch_minutes = 0
    
    try:
        days = float(arg1)
        fetch_minutes = int(days * 24 * 60)
        duration_text = f"최근 {fetch_minutes}분"
    except ValueError:
        if arg2 is None:
            await ctx.send("❌ 사용법: `!백테스트 0.02` 또는 `!백테스트 2024-01-01 60`")
            return
        try:
            target_date = datetime.strptime(arg1, "%Y-%m-%d")
            end_dt = target_date + timedelta(minutes=int(arg2))
            target_to = end_dt.strftime("%Y-%m-%d %H:%M:%S")
            fetch_minutes = int(arg2)
            duration_text = f"{arg1} 00:00부터 {int(arg2)}분간"
        except ValueError:
            await ctx.send("❌ 날짜 형식 오류")
            return

    status_msg = await ctx.send(f"⏳ **{duration_text}** 백테스트 시작... (진행률 모니터링 중)")
    
    # 데이터 수집 (비동기)
    def fetch_data():
        count = int(fetch_minutes / 5) + 50
        df = pyupbit.get_ohlcv("KRW-BTC", interval="minute5", count=count, to=target_to)
        return calculate_indicators(df) if df is not None else None

    df = await asyncio.to_thread(fetch_data)
    if df is None:
        await status_msg.edit(content="❌ 데이터 수집 실패")
        return

    # 청크 분할 및 실행
    workers = min(len(API_KEYS), len(df) // 20 + 1)
    chunk_size = len(df) // workers
    chunks = [df.iloc[i*chunk_size : (i+1)*chunk_size] for i in range(workers)]

    loop = asyncio.get_running_loop()
    tasks_list = []
    global backtest_status
    backtest_status = {i: {"current": 0, "total": len(chunks[i])} for i in range(workers)}

    for i in range(workers):
        tasks_list.append(loop.run_in_executor(None, analyze_chunk_for_bot, chunks[i], API_KEYS[i], i))
        await asyncio.sleep(1)

    # 진행률 모니터링
    while not all([t.done() for t in tasks_list]):
        total = sum(s["total"] for s in backtest_status.values())
        curr = sum(s["current"] for s in backtest_status.values())
        percent = (curr / total * 100) if total > 0 else 0
        
        bar = "█" * int(percent / 5) + "░" * (20 - int(percent / 5))
        try: await status_msg.edit(content=f"🔄 진행 중: `{bar}` {percent:.1f}% ({curr}/{total})")
        except: pass
        await asyncio.sleep(3)

    # 결과 취합
    results = []
    for t in tasks_list:
        results.extend(await t)
    results.sort(key=lambda x: x['time'])

    if not results:
        await status_msg.edit(content="❌ 결과 없음")
        return

    # 시뮬레이션
    fw = FuturesWallet()
    log_txt = ""
    for log in results:
        # SL/TP 체크
        exit_res = fw.update(log['high'], log['low'])
        if exit_res:
            icon = "🟢" if exit_res['pnl'] > 0 else "🔴"
            log_txt += f"[{log['time'].strftime('%H:%M')}] {icon} {exit_res['reason']} 청산! {int(exit_res['pnl']):,}원\n"
        
        # 진입 체크
        if fw.position is None:
            decision = log['decision'].lower()
            if log.get('confidence', 0) >= 70 and decision in ['long', 'short']:
                invest = fw.get_balance() * 0.98
                entry = fw.enter_position(decision, log['price'], invest, log.get('sl'), log.get('tp'))
                if entry['status'] == 'success':
                    icon = "📈" if decision == 'long' else "📉"
                    log_txt += f"[{log['time'].strftime('%H:%M')}] {icon} {decision.upper()} 진입 ({log['price']:,.0f})\n"

    # 결과 리포트
    roi = ((fw.get_balance() / 10000000) - 1) * 100
    embed = discord.Embed(title=f"📊 백테스트 결과 ({duration_text})", color=0x9b59b6)
    embed.add_field(name="수익률", value=f"**{roi:.2f}%**", inline=True)
    embed.add_field(name="최종 자산", value=f"{int(fw.get_balance()):,}원", inline=True)
    
    if len(log_txt) > 1000: log_txt = log_txt[-1000:]
    embed.add_field(name="로그 (최근)", value=f"```\n{log_txt or '거래 없음'}\n```", inline=False)
    
    await status_msg.delete()
    await ctx.send(embed=embed)

@bot.command(name="자산")
async def check_balance(ctx):
    bal = int(real_wallet.get_balance())
    pos = real_wallet.position
    embed = discord.Embed(title="💰 선물 지갑 현황", color=0x9b59b6)
    embed.add_field(name="현금 잔고", value=f"{bal:,}원", inline=False)
    if pos:
        pnl = ((pyupbit.get_current_price("KRW-BTC") - pos['entry_price']) / pos['entry_price'] * 100) * (1 if pos['type']=='long' else -1)
        embed.add_field(name="포지션", value=f"{pos['type'].upper()} ({pnl:.2f}%)", inline=True)
    else:
        embed.add_field(name="포지션", value="대기 중", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="테스트매매시작")
async def start_trading(ctx):
    global auto_active
    if not auto_active:
        auto_active = True
        auto_trade.start()
        await ctx.send("🚀 매매 시작 (15초 주기)")

@bot.command(name="테스트매매중지")
async def stop_trading(ctx):
    global auto_active
    auto_active = False
    auto_trade.cancel()
    await ctx.send("⏸️ 매매 중지")

@bot.command(name="종료")
async def shutdown_bot(ctx):
    await ctx.send("bye.")
    await bot.close()

# ==========================================
# 6. 실시간 매매 루프 (15초)
# ==========================================
@tasks.loop(seconds=15)
async def auto_trade():
    global dashboard_msg, last_ai_analysis_time
    if not auto_active: return
    
    # 채널 가져오기 (실패 시 메인 채널 fallback)
    dash_ch = bot.get_channel(DASHBOARD_ID) or bot.get_channel(TARGET_CHANNEL_ID)
    sig_ch = bot.get_channel(SIGNAL_ID) or bot.get_channel(TARGET_CHANNEL_ID)
    exp_ch = bot.get_channel(EXPLAIN_ID) or bot.get_channel(TARGET_CHANNEL_ID)
    if not dash_ch: return

    # 1. 현재가 조회 (빠른 응답)
    try:
        curr_price = pyupbit.get_current_price("KRW-BTC")
    except:
        return

    # 2. 포지션 감시 및 청산 (비동기 DB 쓰기)
    exit_res = await asyncio.to_thread(real_wallet.update, curr_price, curr_price)
    
    if exit_res:
        # 손실 발생 시 반성문 작성
        if exit_res['pnl'] < 0:
            def run_analysis():
                df = pyupbit.get_ohlcv("KRW-BTC", interval="minute5", count=30)
                df = calculate_indicators(df)
                return analyze_loss_reason(df, exit_res)
            
            analysis_en = await asyncio.to_thread(run_analysis)
            analysis_kr = await asyncio.to_thread(translate_to_korean, analysis_en)
            
            # DB 업데이트
            if real_wallet.last_trade_id:
                await asyncio.to_thread(real_wallet.db.update_analysis, real_wallet.last_trade_id, analysis_en)
            
            await exp_ch.send(embed=discord.Embed(title="😭 AI 반성문", description=analysis_kr, color=0xff0000))

        # 청산 알림
        color = 0x00ff00 if exit_res['pnl'] > 0 else 0xff0000
        embed = discord.Embed(title=f"⚡ 포지션 종료 ({exit_res['reason']})", color=color)
        embed.add_field(name="실현 손익", value=f"{int(exit_res['pnl']):,}원 ({exit_res['profit_rate']:.2f}%)", inline=True)
        await sig_ch.send(embed=embed)

    # 3. 대시보드 업데이트
    pos = real_wallet.position
    bal = int(real_wallet.get_balance())
    total_equity = bal
    pnl_str, status_str, color = "-", "💤 관망", 0x95a5a6
    
    if pos:
        side = pos['type'].upper()
        pnl = ((curr_price - pos['entry_price']) / pos['entry_price'] * 100) * (1 if side=='LONG' else -1)
        unrealized = (curr_price - pos['entry_price']) * pos['amount'] * (1 if side=='LONG' else -1)
        total_equity += unrealized
        pnl_str = f"{pnl:+.2f}%"
        status_str = f"🔥 {side} 포지션 보유 중"
        color = 0x3498db if side == 'LONG' else 0xe74c3c

    roi = ((total_equity / 10000000) - 1) * 100
    
    embed = discord.Embed(title="📊 실시간 트레이딩 대시보드", color=color)
    embed.add_field(name="현재가", value=f"{curr_price:,.0f}원", inline=True)
    embed.add_field(name="상태", value=status_str, inline=True)
    embed.add_field(name="미실현 손익", value=pnl_str, inline=True)
    embed.add_field(name="누적 수익률 (Total ROI)", value=f"{'🔴' if roi<0 else '🟢'} {roi:.2f}%", inline=True)
    embed.add_field(name="총 자산 가치", value=f"{int(total_equity):,}원", inline=True)
    embed.set_footer(text=f"마지막 업데이트: {datetime.now().strftime('%H:%M:%S')}")
    
    try:
        if dashboard_msg: await dashboard_msg.edit(embed=embed)
        else: dashboard_msg = await dash_ch.send(embed=embed)
    except:
        dashboard_msg = await dash_ch.send(embed=embed)

    # 4. AI 진입 판단 (5분 주기 & 포지션 없을 때)
    if (time.time() - last_ai_analysis_time > 300) and real_wallet.position is None:
        
        def fetch_and_analyze():
            df = pyupbit.get_ohlcv("KRW-BTC", interval="minute5", count=50)
            if df is None: return None
            return calculate_indicators(df)

        df = await asyncio.to_thread(fetch_and_analyze)
        if df is not None:
            res = await asyncio.to_thread(ask_ai_realtime, df)
            last_ai_analysis_time = time.time()
            
            decision = res['decision'].lower()
            if res.get('confidence', 0) >= 70 and decision in ['long', 'short']:
                entry = await asyncio.to_thread(real_wallet.enter_position, decision, curr_price, real_wallet.get_balance()*0.98, res.get('sl'), res.get('tp'))
                
                if entry['status'] == 'success':
                    await sig_ch.send(embed=discord.Embed(title=f"🚀 {decision.upper()} 포지션 진입!", color=0x3498db))
                    
                    reason_kr = await asyncio.to_thread(translate_to_korean, res['reason'])
                    await exp_ch.send(embed=discord.Embed(title="🧠 판단 근거", description=reason_kr, color=0x9b59b6))

bot.run(TOKEN)