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
import traceback # 상세 에러 로그용

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

backtester = Backtester(api_keys=GEMINI_KEYS)
live_wallet = None 
is_live_active = False
dashboard_msg = None 

# 바이낸스 객체 생성 (옵션 추가)
binance = ccxt.binanceusdm({
    'options': {
        'defaultType': 'future', # 선물 마켓 강제 지정
    },
    'enableRateLimit': True
})

# ==========================================
# 2. 유틸리티 함수 (AI 관련)
# ==========================================
async def ask_ai_decision(df):
    try:
        if df.empty: return {"decision": "hold", "confidence": 0}
        
        row = df.iloc[-1]
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
        
        key = key_manager.get_key()
        genai.configure(api_key=key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        # 비동기 실행으로 변경
        response = await asyncio.to_thread(model.generate_content, prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ AI Error: {e}")
        return {"decision": "hold", "confidence": 0}

async def translate_reason(text):
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
# 3. 실시간 매매 루프 및 대쉬보드
# ==========================================
async def update_dashboard():
    """대쉬보드 메시지 갱신"""
    global dashboard_msg
    ch_dash = bot.get_channel(DASHBOARD_ID)
    if not ch_dash: return

    try:
        krw_price = pyupbit.get_current_price("KRW-BTC")
    except:
        krw_price = 0

    # 지갑 상태 확인 (지갑이 없으면 대기 모드로 표시)
    if live_wallet:
        bal = int(live_wallet.get_balance())
        initial = live_wallet.initial_balance
        unrealized = live_wallet.get_unrealized_pnl(krw_price) if live_wallet.position else 0
        total_equity = bal + unrealized
        
        # 0으로 나누기 방지
        if initial > 0:
            total_roi = (total_equity - initial) / initial * 100
        else:
            total_roi = 0.0
        
        status_text = "💤 관망 중 (Scanning...)"
        pnl_text = "-"
        entry_text = "-"
        sl_tp_text = "-"
        color = 0x95a5a6 
        
        if live_wallet.position:
            pos = live_wallet.position
            side = pos['type'].upper()
            status_text = f"🔥 {side} 포지션 보유 중"
            color = 0x2ecc71 if total_roi >= 0 else 0xe74c3c
            
            pnl_rate_curr = (unrealized / pos['invested_krw']) * 100
            pnl_text = f"{int(unrealized):,}원 ({pnl_rate_curr:+.2f}%)"
            entry_text = f"{int(pos['entry_price']):,}원"
            
            sl_disp = f"USDT {pos.get('usdt_sl', 0)}"
            tp_disp = f"USDT {pos.get('usdt_tp', 0)}"
            
            # KRW 환산 표시 로직 강화
            usdt_entry = pos.get('usdt_entry')
            usdt_sl = pos.get('usdt_sl')
            usdt_tp = pos.get('usdt_tp')
            krw_entry = pos['entry_price']

            sl_disp_krw = "-"
            tp_disp_krw = "-"

            if usdt_entry and usdt_entry > 0:
                if usdt_sl:
                    sl_krw = krw_entry * (usdt_sl / usdt_entry)
                    sl_disp_krw = f"{int(sl_krw):,}원"
                if usdt_tp:
                    tp_krw = krw_entry * (usdt_tp / usdt_entry)
                    tp_disp_krw = f"{int(tp_krw):,}원"
            
            sl_tp_text = f"SL: {sl_disp_krw} | TP: {tp_disp_krw}"
            
        desc = f"Last Update: {datetime.now().strftime('%H:%M:%S')}"
        
    else:
        # 지갑 미생성 상태 (대기 모드)
        status_text = "⛔ 봇 대기 중 (명령어 대기)"
        color = 0x2f3136 # 어두운 회색
        krw_price = krw_price or 0
        total_roi = 0.0
        total_equity = 0
        pnl_text = "-"
        entry_text = "-"
        sl_tp_text = "-"
        desc = "봇이 준비되었습니다. `!테스트매매시작`을 입력하세요."

    embed = discord.Embed(title="🔴 실시간 AI 트레이딩 대쉬보드", description=desc, color=color)
    embed.add_field(name="현재가 (KRW)", value=f"**{int(krw_price):,}원**", inline=True)
    embed.add_field(name="누적 수익률", value=f"**{total_roi:+.2f}%**", inline=True)
    embed.add_field(name="총 평가 자산", value=f"{int(total_equity):,}원", inline=True)
    
    embed.add_field(name="상태", value=status_text, inline=False)
    embed.add_field(name="진입가", value=entry_text, inline=True)
    embed.add_field(name="평가 손익", value=pnl_text, inline=True)
    embed.add_field(name="전략 (KRW 환산)", value=sl_tp_text, inline=False)
    
    if live_wallet:
        embed.set_footer(text="10초마다 자동 갱신됩니다.")
    else:
        embed.set_footer(text="매매가 시작되지 않았습니다.")

    try:
        if dashboard_msg:
            await dashboard_msg.edit(embed=embed)
        else:
            async for msg in ch_dash.history(limit=5):
                if msg.author == bot.user:
                    await msg.delete()
            dashboard_msg = await ch_dash.send(embed=embed)
    except Exception as e:
        print(f"Dashboard Update Error: {e}")
        try:
            dashboard_msg = await ch_dash.send(embed=embed)
        except:
            pass

@tasks.loop(seconds=10)
async def live_trading_loop():
    global is_live_active, live_wallet
    
    if not is_live_active or not live_wallet:
        return

    # [FIX] 전체 로직을 try-except로 감싸서 루프 중단 방지
    try:
        # 1. 대쉬보드 갱신
        await update_dashboard()

        # 2. 데이터 수집
        krw_price = pyupbit.get_current_price("KRW-BTC")
        
        # [FIX] fetch_ohlcv 실패 시 루프 중단되지 않도록 처리
        try:
            ohlcv = await asyncio.to_thread(binance.fetch_ohlcv, "BTC/USDT", "5m", limit=50)
            if not ohlcv: # 데이터 없으면 이번 루프 스킵
                return 
            
            df_binance = pd.DataFrame(ohlcv, columns=['dt', 'open', 'high', 'low', 'close', 'vol'])
            df_binance = brain.calculate_indicators(df_binance)
            
            if df_binance.empty: # 데이터프레임 비었으면 스킵
                return
                
            current_usdt_price = df_binance['close'].iloc[-1]
        except Exception as e:
            print(f"Data Fetch Error: {e}")
            return

        # 3. 포지션 관리
        if live_wallet.position:
            pos = live_wallet.position
            if pos['type'] == 'long':
                pnl_rate = (krw_price - pos['entry_price']) / pos['entry_price']
            else:
                pnl_rate = (pos['entry_price'] - krw_price) / pos['entry_price']
                
            sl_rate = -0.02
            tp_rate = 0.04
            
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

            close_reason = None
            if pnl_rate <= sl_rate: close_reason = "Stop Loss 🔵"
            elif pnl_rate >= tp_rate: close_reason = "Take Profit 🔴"
            
            if close_reason:
                trade_result = live_wallet.close_position(krw_price, reason=close_reason)
                ch = bot.get_channel(EXPLAIN_ID)
                if ch:
                    color = 0x00ff00 if trade_result['pnl'] > 0 else 0xff0000
                    embed = discord.Embed(title=f"⚡ 포지션 종료: {close_reason}", color=color)
                    embed.add_field(name="수익금", value=f"{int(trade_result['pnl']):,}원", inline=True)
                    embed.add_field(name="수익률", value=f"{trade_result['profit_rate']:.2f}%", inline=True)
                    await ch.send(embed=embed)
                    
                    if trade_result['pnl'] < 0:
                        feedback = await analyze_failure(trade_result, df_binance)
                        embed_fail = discord.Embed(title="😭 전문 트레이더의 팩트 폭격", description=feedback, color=0x000000)
                        await ch.send(embed=embed_fail)

        # 4. 신규 진입
        if live_wallet.position is None:
            if datetime.now().second <= 15: 
                decision = await ask_ai_decision(df_binance)
                
                if decision['confidence'] >= 70 and decision['decision'] in ['long', 'short']:
                    side = decision['decision']
                    reason_kr = await translate_reason(decision.get('reason', 'No reason'))
                    
                    invest = live_wallet.get_balance() * 0.98
                    live_wallet.enter_position(side, krw_price, invest, sl=0, tp=0)
                    
                    live_wallet.position['usdt_entry'] = current_usdt_price
                    live_wallet.position['usdt_sl'] = decision.get('sl')
                    live_wallet.position['usdt_tp'] = decision.get('tp')

                    ch = bot.get_channel(EXPLAIN_ID)
                    if ch:
                        embed = discord.Embed(title=f"🚀 AI 진입 신호: {side.upper()}", color=0x0000ff)
                        embed.add_field(name="확신도", value=f"{decision['confidence']}%", inline=True)
                        embed.add_field(name="진입가(KRW)", value=f"{int(krw_price):,}원", inline=True)
                        embed.add_field(name="판단 이유", value=reason_kr, inline=False)
                        await ch.send(embed=embed)
                    
                    await update_dashboard()

    except Exception as e:
        print(f"🔥 Live Loop Critical Error: {e}")
        traceback.print_exc() # 상세 에러 출력
        # 에러 발생해도 루프는 계속 돌도록 pass (혹은 잠시 대기)
        await asyncio.sleep(5)

# ==========================================
# 4. 명령어 처리
# ==========================================
@bot.command(name="테스트매매시작")
async def start_live_trading(ctx):
    global is_live_active, live_wallet, dashboard_msg
    
    if is_live_active:
        await ctx.send("⚠️ 이미 매매가 진행 중입니다.")
        return
    
    live_wallet = FuturesWallet(initial_balance=1000000)
    is_live_active = True
    dashboard_msg = None 
    
    await ctx.send("🚀 **AI 실전 모의투자**를 시작합니다! (초기자금: 100만원)")
    
    # [FIX] 루프 시작 전 대쉬보드 갱신
    try:
        await update_dashboard()
    except Exception as e:
        print(f"Initial Dashboard Error: {e}")

    live_trading_loop.start()

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

@bot.command(name="백테스트")
async def start_backtest(ctx, arg1: str, arg2: str = None):
    await ctx.send(f"⏳ 백테스트 요청 확인... (병렬 엔진 가동)")
    # 실제 백테스트 호출 로직은 parallel_backtester 사용

@bot.event
async def on_ready():
    print(f"✅ {bot.user} 접속 성공!")
    
    # [FIX] 바이낸스 마켓 정보 미리 로드
    try:
        print("⏳ 바이낸스 마켓 데이터 로딩 중...")
        await asyncio.to_thread(binance.load_markets)
        print("✅ 바이낸스 로딩 완료")
    except Exception as e:
        print(f"❌ 바이낸스 로딩 실패: {e}")
        
    # [FIX] 봇 켜지자마자 대쉬보드 출력
    await update_dashboard()

bot.run(TOKEN)
