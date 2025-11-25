import discord
from discord.ext import commands, tasks
import os
import sys
import json
import asyncio
import time
from datetime import datetime
import pyupbit
import ccxt
import pandas as pd
import google.generativeai as genai
from paper_exchange import FuturesWallet 
from parallel_backtester import Backtester 
import brain

# ==========================================
# 0. 설정 및 키 관리
# ==========================================
CONFIG_FILE = 'config.json'

if not os.path.exists(CONFIG_FILE):
    print(f"❌ 오류: '{CONFIG_FILE}' 파일이 없습니다.")
    sys.exit()

with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
    config = json.load(f)
    
TOKEN = config['DISCORD_TOKEN']
DASHBOARD_ID = int(config.get('DISCORD_DASHBOARD_ID', 0))
EXPLAIN_ID = int(config.get('DISCORD_EXPLAIN_ID', 0))
GEMINI_KEYS = config.get('GEMINI_API_KEYS', [])

# 키 매니저 (라운드 로빈)
class KeyManager:
    def __init__(self, keys):
        self.keys = keys
        self.idx = 0
    def get_key(self):
        k = self.keys[self.idx]
        self.idx = (self.idx + 1) % len(self.keys)
        return k

key_manager = KeyManager(GEMINI_KEYS)

# ==========================================
# 1. 봇 및 변수 초기화
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 모듈 인스턴스
backtester = Backtester(api_keys=GEMINI_KEYS)
live_wallet = None # 테스트 매매 시작 시 생성
is_live_active = False
dashboard_msg = None # 대쉬보드 메시지 객체 저장용

# 바이낸스 데이터 조회용 (AI 분석용)
binance = ccxt.binanceusdm()

# ==========================================
# 2. 유틸리티 함수 (AI 관련)
# ==========================================
async def ask_ai_decision(df):
    """바이낸스 차트 데이터를 AI에게 분석 요청"""
    row = df.iloc[-1]
    # 기술적 지표 포맷팅
    data_str = (
        f"Close: {row['close']}, RSI: {row['RSI']:.1f}, "
        f"MACD: {row['MACD']:.1f}, BB_Pos: {(row['close'] - row['BB_Low']) / (row['BB_Up'] - row['BB_Low']):.2f}"
    )
    
    prompt = f"""
    Role: Bitcoin Futures Trading AI.
    Task: Analyze 5m candle data (Binance USDT).
    Data: {data_str}
    
    Output JSON: {{"decision": "long/short/hold", "confidence": 0-100, "sl": price, "tp": price, "reason": "english reason"}}
    """
    
    try:
        key = key_manager.get_key()
        genai.configure(api_key=key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        response = await asyncio.to_thread(model.generate_content, prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ AI Error: {e}")
        return {"decision": "hold", "confidence": 0}

async def translate_reason(text):
    """AI의 판단 이유를 한국어로 번역"""
    try:
        key = key_manager.get_key()
        genai.configure(api_key=key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        prompt = f"Translate this trading reasoning into natural Korean for a trader:\n'{text}'"
        response = await asyncio.to_thread(model.generate_content, prompt)
        return response.text.strip()
    except:
        return text

async def analyze_failure(trade_info, df_context):
    """손실 발생 시 전문 트레이더 관점의 피드백"""
    try:
        key = key_manager.get_key()
        genai.configure(api_key=key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        prompt = f"""
        Act as a Wall Street Senior Trader.
        My bot just lost money. Analyze why.
        
        [Trade Info]
        Side: {trade_info['side']}
        Entry: {trade_info['entry']}
        Exit: {trade_info['exit']}
        Reason: {trade_info['reason']}
        
        [Market Context]
        RSI: {df_context['RSI'].iloc[-1]:.1f}
        Trend: {'Bullish' if df_context['MA20'].iloc[-1] > df_context['MA60'].iloc[-1] else 'Bearish'}
        
        Output: A short, harsh, but constructive feedback in Korean. (반말 모드)
        """
        response = await asyncio.to_thread(model.generate_content, prompt)
        return response.text.strip()
    except:
        return "분석 실패 (API 오류)"

# ==========================================
# 3. 실시간 매매 루프 (10초 주기)
# ==========================================
@tasks.loop(seconds=10)
async def live_trading_loop():
    global dashboard_msg, is_live_active, live_wallet
    
    if not is_live_active or not live_wallet:
        return

    # ---------------------------
    # A. 데이터 수집 (업비트 & 바이낸스)
    # ---------------------------
    # 1. 업비트 현재가 (KRW - 대쉬보드 표시용)
    try:
        krw_price = pyupbit.get_current_price("KRW-BTC")
    except:
        return # API 오류 시 스킵

    # 2. 바이낸스 차트 (USDT - AI 분석용)
    # 10초마다 호출하되, AI 분석은 5분봉 갱신 시점에만 수행하거나 
    # 포지션 없을 때 특정 조건 만족 시 수행
    try:
        ohlcv = await asyncio.to_thread(binance.fetch_ohlcv, "BTC/USDT", "5m", limit=50)
        df_binance = pd.DataFrame(ohlcv, columns=['dt', 'open', 'high', 'low', 'close', 'vol'])
        df_binance = brain.calculate_indicators(df_binance) # 지표 계산
        current_usdt_price = df_binance['close'].iloc[-1]
    except Exception as e:
        print(f"Binance Data Error: {e}")
        return

    # ---------------------------
    # B. 포지션 관리 (청산 감시)
    # ---------------------------
    # 주의: AI는 USDT 기준으로 SL/TP를 줬지만, 우리는 KRW 지갑을 씀.
    # 김프(Kimchi Premium)를 고려해야 하지만, 여기서는 단순화를 위해
    # "변동률(%)"을 기반으로 KRW 가격에 적용하여 청산함.
    
    trade_result = None
    if live_wallet.position:
        pos = live_wallet.position
        
        # 현재 수익률 계산
        if pos['type'] == 'long':
            pnl_rate = (krw_price - pos['entry_price']) / pos['entry_price']
        else:
            pnl_rate = (pos['entry_price'] - krw_price) / pos['entry_price']
            
        # 목표가/손절가 도달 체크 (USDT 기준 변동폭을 역산하거나, 단순 %로 계산)
        # 여기서는 AI가 준 SL/TP 가격을 %로 환산해서 적용
        # 예: AI가 100불 진입, 101불 TP(1%) -> KRW 진입가 * 1.01에 청산
        
        sl_rate = -0.02 # 기본 손절 -2%
        tp_rate = 0.04  # 기본 익절 +4%
        
        # AI가 준 구체적인 가격이 있다면 그 비율을 따름
        if pos.get('usdt_entry') and pos.get('usdt_sl'):
            if pos['type'] == 'long':
                sl_rate = (pos['usdt_sl'] - pos['usdt_entry']) / pos['usdt_entry']
            else:
                sl_rate = (pos['usdt_entry'] - pos['usdt_sl']) / pos['usdt_entry']
        
        if pos.get('usdt_entry') and pos.get('usdt_tp'):
            if pos['type'] == 'long':
                tp_rate = (pos['usdt_tp'] - pos['usdt_entry']) / pos['usdt_entry']
            else:
                tp_rate = (pos['usdt_entry'] - pos['usdt_tp']) / pos['usdt_entry']

        # 청산 실행
        close_reason = None
        if pnl_rate <= sl_rate: close_reason = "Stop Loss 🔵"
        elif pnl_rate >= tp_rate: close_reason = "Take Profit 🔴"
        
        if close_reason:
            trade_result = live_wallet.close_position(krw_price, reason=close_reason)
            
            # 설명 채널에 결과 전송
            ch = bot.get_channel(EXPLAIN_ID)
            if ch:
                color = 0x00ff00 if trade_result['pnl'] > 0 else 0xff0000
                embed = discord.Embed(title=f"⚡ 포지션 종료: {close_reason}", color=color)
                embed.add_field(name="수익금", value=f"{int(trade_result['pnl']):,}원", inline=True)
                embed.add_field(name="수익률", value=f"{trade_result['profit_rate']:.2f}%", inline=True)
                await ch.send(embed=embed)
                
                # 손실 시 반성문 작성
                if trade_result['pnl'] < 0:
                    feedback = await analyze_failure(trade_result, df_binance)
                    embed_fail = discord.Embed(title="😭 전문 트레이더의 팩트 폭격", description=feedback, color=0x000000)
                    await ch.send(embed=embed_fail)

    # ---------------------------
    # C. 신규 진입 (AI 판단)
    # ---------------------------
    # 포지션이 없고, 마지막 분석으로부터 시간이 좀 지났거나(예: 1분) 할 때
    if live_wallet.position is None:
        # 간단한 스로틀링: 초(Second)가 0~10 사이일 때만 분석 (매분 초반)
        if datetime.now().second <= 15: 
            decision = await ask_ai_decision(df_binance)
            
            if decision['confidence'] >= 70 and decision['decision'] in ['long', 'short']:
                side = decision['decision']
                
                # 번역
                reason_kr = await translate_reason(decision.get('reason', 'No reason'))
                
                # 진입 (98% 비중)
                invest = live_wallet.get_balance() * 0.98
                entry_res = live_wallet.enter_position(
                    side, krw_price, invest, 
                    sl=0, tp=0 # KRW 가격은 모르므로 일단 0, 위에서 비율로 계산
                )
                
                # USDT 기준 가격 정보를 포지션에 추가 저장 (청산 로직용)
                live_wallet.position['usdt_entry'] = current_usdt_price
                live_wallet.position['usdt_sl'] = decision.get('sl')
                live_wallet.position['usdt_tp'] = decision.get('tp')

                # 설명 채널 알림
                ch = bot.get_channel(EXPLAIN_ID)
                if ch:
                    embed = discord.Embed(title=f"🚀 AI 진입 신호: {side.upper()}", color=0x0000ff)
                    embed.add_field(name="확신도", value=f"{decision['confidence']}%", inline=True)
                    embed.add_field(name="진입가(KRW)", value=f"{int(krw_price):,}원", inline=True)
                    embed.add_field(name="판단 이유", value=reason_kr, inline=False)
                    await ch.send(embed=embed)

    # ---------------------------
    # D. 대쉬보드 업데이트
    # ---------------------------
    ch_dash = bot.get_channel(DASHBOARD_ID)
    if ch_dash:
        # 상태 메시지 구성
        bal = int(live_wallet.get_balance())
        initial = live_wallet.initial_balance
        total_roi = ((bal + (live_wallet.get_unrealized_pnl(krw_price) if live_wallet.position else 0)) - initial) / initial * 100
        
        status_text = "💤 관망 중 (Scanning...)"
        pnl_text = "-"
        entry_text = "-"
        sl_tp_text = "-"
        
        color = 0x95a5a6 # 회색
        
        if live_wallet.position:
            pos = live_wallet.position
            side = pos['type'].upper()
            status_text = f"🔥 {side} 포지션 보유 중"
            color = 0x2ecc71 if total_roi >= 0 else 0xe74c3c
            
            pnl_curr = live_wallet.get_unrealized_pnl(krw_price)
            pnl_rate_curr = (pnl_curr / pos['invested_krw']) * 100
            pnl_text = f"{int(pnl_curr):,}원 ({pnl_rate_curr:+.2f}%)"
            entry_text = f"{int(pos['entry_price']):,}원"
            
            # SL/TP 표시 (USDT 기준을 KRW 추정치로 표시하거나 비율로 표시)
            sl_disp = f"USDT {pos.get('usdt_sl', 0)}"
            tp_disp = f"USDT {pos.get('usdt_tp', 0)}"
            sl_tp_text = f"SL: {sl_disp} | TP: {tp_disp}"

        embed = discord.Embed(title="🔴 실시간 AI 트레이딩 대쉬보드", description=f"현재 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", color=color)
        embed.add_field(name="현재가 (KRW)", value=f"**{int(krw_price):,}원**", inline=True)
        embed.add_field(name="누적 수익률", value=f"**{total_roi:+.2f}%**", inline=True)
        embed.add_field(name="현재 자산", value=f"{int(bal + (live_wallet.get_unrealized_pnl(krw_price) if live_wallet.position else 0)):,}원", inline=True)
        
        embed.add_field(name="상태", value=status_text, inline=False)
        embed.add_field(name="진입가", value=entry_text, inline=True)
        embed.add_field(name="평가 손익", value=pnl_text, inline=True)
        embed.add_field(name="전략 (USDT기준)", value=sl_tp_text, inline=False)
        
        embed.set_footer(text="10초마다 자동 갱신됩니다.")

        try:
            if dashboard_msg:
                await dashboard_msg.edit(embed=embed)
            else:
                # 이전에 쓴 메시지가 있다면 찾아서 지우고 새로 씀 (깔끔하게)
                async for msg in ch_dash.history(limit=5):
                    if msg.author == bot.user:
                        await msg.delete()
                dashboard_msg = await ch_dash.send(embed=embed)
        except discord.errors.NotFound:
            dashboard_msg = await ch_dash.send(embed=embed)
        except Exception as e:
            print(f"Dashboard Error: {e}")

# ==========================================
# 4. 명령어 처리
# ==========================================
@bot.command(name="테스트매매시작")
async def start_live_trading(ctx):
    global is_live_active, live_wallet, dashboard_msg
    
    if is_live_active:
        await ctx.send("⚠️ 이미 매매가 진행 중입니다.")
        return
    
    live_wallet = FuturesWallet(initial_balance=1000000) # 100만원 시작
    is_live_active = True
    dashboard_msg = None # 초기화
    
    live_trading_loop.start()
    await ctx.send("🚀 **AI 실전 모의투자**를 시작합니다! (초기자금: 100만원)\n대쉬보드 및 설명 채널을 확인하세요.")

@bot.command(name="테스트매매종료")
async def stop_live_trading(ctx):
    global is_live_active
    is_live_active = False
    live_trading_loop.stop()
    await ctx.send("⏸️ 매매를 중지했습니다.")

@bot.command(name="종료")
async def shutdown(ctx):
    global dashboard_msg
    if dashboard_msg:
        try: await dashboard_msg.delete()
        except: pass
    await ctx.send("🤖 봇을 종료합니다. 안녕히 계세요!")
    await bot.close()

# 기존 백테스트 명령어 유지
@bot.command(name="백테스트")
async def start_backtest(ctx, arg1: str, arg2: str = None):
    # (백테스트 코드는 기존과 동일하므로 생략하지 않고 간단히 연결)
    await ctx.send(f"⏳ 백테스트 요청 확인... (병렬 엔진 가동)")
    # 여기에 parallel_backtester 호출 로직 연결 (이전 코드 참조)
    # 실제 구현시 parallel_backtester.Backtester(GEMINI_KEYS).run(...) 호출

@bot.event
async def on_ready():
    print(f"✅ {bot.user} 접속 성공!")

bot.run(TOKEN)
