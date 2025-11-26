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
import traceback
import re

# ==========================================
# 0. 설정 및 키 관리
# ==========================================
CONFIG_FILE = 'config.json'

if not os.path.exists(CONFIG_FILE):
    print(f"❌ 오류: '{CONFIG_FILE}' 파일이 없습니다.")
    sys.exit()

def load_sanitized_json(filepath):
    """JSON 파일에서 제어 문자 제거 후 로드"""
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        # 제어 문자 제거 (줄바꿈, 탭 제외)
        sanitized_content = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', content)
        
        try:
            return json.loads(sanitized_content)
        except json.JSONDecodeError as e:
            print(f"❌ JSON 파싱 오류: {e}")
            sys.exit()
        except Exception as e:
            print(f"❌ 설정 파일 로드 중 알 수 없는 오류: {e}")
            sys.exit()

config = load_sanitized_json(CONFIG_FILE)
    
TOKEN = config['DISCORD_TOKEN']
DASHBOARD_ID = int(config.get('DISCORD_DASHBOARD_ID', 0))
EXPLAIN_ID = int(config.get('DISCORD_EXPLAIN_ID', 0))
KEY_MANAGER_ID = int(config.get('DISCORD_KEY_MANAGER_ID', 0)) 
GEMINI_KEYS_RAW = config.get('GEMINI_API_KEYS', [])

class KeyManager:
    def __init__(self, keys_raw):
        self.keys = []
        self.key_names = {}
        self.error_counts = {} 
        self.last_errors = {} 
        self.idx = 0
        
        for item in keys_raw:
            if ':' in item:
                k, name = item.split(':', 1)
                k = k.strip()
                name = name.strip()
            else:
                k = item.strip()
                name = f"Key-{len(self.keys)+1}"
            
            self.keys.append(k)
            self.key_names[k] = name
            self.error_counts[k] = 0
            self.last_errors[k] = "None"

    def get_key(self):
        if not self.keys: return None
        k = self.keys[self.idx]
        self.idx = (self.idx + 1) % len(self.keys)
        return k
    
    def report_error(self, key, error):
        if key in self.error_counts:
            self.error_counts[key] += 1
            self.last_errors[key] = str(error)
            
    def get_status_embed(self):
        embed = discord.Embed(title="🔑 API Key 상태 모니터링", color=0x9b59b6)
        embed.description = f"총 {len(self.keys)}개의 키가 로드되었습니다."
        embed.set_footer(text=f"Last Update: {datetime.now().strftime('%H:%M:%S')} | 10초 주기 갱신")
        
        for k in self.keys:
            name = self.key_names[k]
            count = self.error_counts[k]
            last_err = self.last_errors[k]
            
            if count == 0:
                status = "🟢 정상"
            elif count < 5:
                status = f"🟡 불안정 ({count}회)"
            else:
                status = f"🔴 오류 다수 ({count}회)"
            
            err_msg = last_err if last_err == "None" else f"⚠️ {last_err[:40]}..."
            
            embed.add_field(
                name=f"🏷️ {name}", 
                value=f"**상태:** {status}\n**로그:** {err_msg}", 
                inline=False
            )
        return embed

key_manager = KeyManager(GEMINI_KEYS_RAW)

# ==========================================
# 1. 봇 및 변수 초기화
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

backtester = Backtester(api_keys=key_manager.keys)
live_wallet = None 
is_live_active = False
dashboard_msg = None 
key_dashboard_msg = None 

binance = ccxt.binanceusdm({
    'options': {
        'defaultType': 'future',
    },
    'enableRateLimit': True
})

# ==========================================
# 2. 헬퍼 함수
# ==========================================
async def send_split_field_embed(channel, base_embed, field_name, long_text):
    limit = 1000 
    if not long_text: long_text = "내용 없음"
    chunks = [long_text[i:i+limit] for i in range(0, len(long_text), limit)]
    
    if chunks:
        base_embed.add_field(name=field_name, value=chunks[0], inline=False)
    await channel.send(embed=base_embed)
    
    for i, chunk in enumerate(chunks[1:], start=2):
        follow_up = discord.Embed(
            title=f"📄 {field_name} (이어짐 {i}/{len(chunks)})", 
            description=chunk, 
            color=base_embed.color
        )
        await channel.send(embed=follow_up)

async def send_split_description_embed(channel, title, long_text, color):
    limit = 4000 
    if not long_text: long_text = "내용 없음"
    chunks = [long_text[i:i+limit] for i in range(0, len(long_text), limit)]
    
    for i, chunk in enumerate(chunks):
        current_title = title if i == 0 else f"{title} (이어짐 {i+1}/{len(chunks)})"
        embed = discord.Embed(title=current_title, description=chunk, color=color)
        await channel.send(embed=embed)

# ==========================================
# 3. 유틸리티 함수 (AI 관련)
# ==========================================
async def ask_ai_decision(df):
    used_key = None
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
        
        used_key = key_manager.get_key()
        if not used_key: raise Exception("No API Keys available")
        
        genai.configure(api_key=used_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        
        response = await asyncio.to_thread(model.generate_content, prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ AI Error: {e}")
        if used_key: key_manager.report_error(used_key, e)
        return {"decision": "hold", "confidence": 0}

async def translate_reason(text):
    used_key = None
    try:
        used_key = key_manager.get_key()
        if not used_key: return text
        
        genai.configure(api_key=used_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        prompt = f"Translate this trading reasoning into natural Korean:\n'{text}'"
        response = await asyncio.to_thread(model.generate_content, prompt)
        return response.text.strip()
    except Exception as e:
        if used_key: key_manager.report_error(used_key, e)
        return text

async def analyze_failure(trade_info, df_context):
    used_key = None
    try:
        used_key = key_manager.get_key()
        if not used_key: return "API 키 없음"
        
        genai.configure(api_key=used_key)
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
        
        Output: A harsh, constructive feedback in Korean. (반말 모드)
        """
        response = await asyncio.to_thread(model.generate_content, prompt)
        return response.text.strip()
    except Exception as e:
        if used_key: key_manager.report_error(used_key, e)
        return "분석 실패 (API 오류)"

# ==========================================
# 4. 실시간 루프 (매매 & 키 모니터링)
# ==========================================
async def update_dashboard():
    global dashboard_msg
    ch_dash = bot.get_channel(DASHBOARD_ID)
    if not ch_dash: return

    try:
        krw_price = pyupbit.get_current_price("KRW-BTC")
    except:
        krw_price = 0

    if live_wallet:
        bal = int(live_wallet.get_balance())
        initial = live_wallet.initial_balance
        unrealized = live_wallet.get_unrealized_pnl(krw_price) if live_wallet.position else 0
        total_equity = bal + unrealized
        
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
        status_text = "⛔ 봇 대기 중"
        color = 0x2f3136
        krw_price = krw_price or 0
        total_roi = 0.0
        total_equity = 0
        pnl_text = "-"
        entry_text = "-"
        sl_tp_text = "-"
        desc = "봇 준비 완료. `!테스트매매시작`을 입력하세요."

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
        embed.set_footer(text="매매 미진행 상태")

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
async def key_monitoring_loop():
    global key_dashboard_msg
    ch = bot.get_channel(KEY_MANAGER_ID)
    if not ch: return
    
    embed = key_manager.get_status_embed()
    
    try:
        if key_dashboard_msg:
            await key_dashboard_msg.edit(embed=embed)
        else:
            async for msg in ch.history(limit=10):
                if msg.author == bot.user:
                    await msg.delete()
            key_dashboard_msg = await ch.send(embed=embed)
    except Exception as e:
        print(f"Key Dashboard Error: {e}")
        try:
            key_dashboard_msg = await ch.send(embed=embed)
        except: pass

@tasks.loop(seconds=10)
async def live_trading_loop():
    global is_live_active, live_wallet
    if not is_live_active or not live_wallet: return

    try:
        await update_dashboard()
        krw_price = pyupbit.get_current_price("KRW-BTC")
        
        try:
            ohlcv = await asyncio.to_thread(binance.fetch_ohlcv, "BTC/USDT", "5m", limit=50)
            if not ohlcv: return
            df_binance = pd.DataFrame(ohlcv, columns=['dt', 'open', 'high', 'low', 'close', 'vol'])
            df_binance = brain.calculate_indicators(df_binance)
            if df_binance.empty: return
            current_usdt_price = df_binance['close'].iloc[-1]
        except Exception as e:
            print(f"Data Fetch Error: {e}")
            return

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
                        await send_split_description_embed(ch, "😭 전문 트레이더의 팩트 폭격", feedback, 0x000000)

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
                        await send_split_field_embed(ch, embed, "판단 이유", reason_kr)
                    
                    await update_dashboard()

    except Exception as e:
        print(f"🔥 Live Loop Critical Error: {e}")
        traceback.print_exc()
        await asyncio.sleep(5)

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
    try: await update_dashboard()
    except: pass
    live_trading_loop.start()

@bot.command(name="테스트매매종료")
async def stop_live_trading(ctx):
    global is_live_active
    is_live_active = False
    live_trading_loop.stop()
    await ctx.send("⏸️ 매매를 중지했습니다.")

@bot.command(name="종료")
async def shutdown(ctx):
    global dashboard_msg, key_dashboard_msg
    if dashboard_msg:
        try: await dashboard_msg.delete()
        except: pass
    if key_dashboard_msg:
        try: await key_dashboard_msg.delete()
        except: pass
        
    await ctx.send("🤖 봇을 종료합니다. 안녕히 계세요!")
    await bot.close()

@bot.command(name="백테스트")
async def start_backtest(ctx, arg1: str, arg2: str = None):
    """
    사용법:
    1. !백테스트 30  (최근 30일)
    2. !백테스트 2024-01-01 1440 (특정 날짜)
    """
    try:
        # Case 1: 실수형(일수) 입력 시
        days = float(arg1)
        await ctx.send(f"⏳ 최근 {days}일 백테스트 시작...")
        result = await asyncio.to_thread(backtester.run, days=days)
    except ValueError:
        # Case 2: 날짜형 입력 시
        if arg2 is None:
            await ctx.send("❌ 사용법 오류: `!백테스트 7` 또는 `!백테스트 2024-01-01 1440`")
            return
        
        try:
            # 날짜 형식 체크
            datetime.strptime(arg1, "%Y-%m-%d")
            duration = int(arg2)
            
            # 날짜가 지정되면 days는 기간 계산용으로만 사용됨 (Backtester 내부 로직)
            # 안전하게 넉넉한 days 전달
            days_needed = (duration / 1440) + 2
            
            await ctx.send(f"⏳ {arg1}부터 {duration}분간 백테스트 시작...")
            result = await asyncio.to_thread(backtester.run, days=days_needed, start_date=arg1, duration_minutes=duration)
            
        except ValueError:
             await ctx.send("❌ 날짜 형식(YYYY-MM-DD) 또는 기간(분)이 잘못되었습니다.")
             return

    # 결과 출력
    if result:
        embed = discord.Embed(title="📊 백테스트 결과", color=0x9b59b6)
        embed.add_field(name="최종 자산", value=f"{int(result['final_balance']):,}원", inline=True)
        embed.add_field(name="수익률", value=f"{result['roi']:.2f}%", inline=True)
        embed.add_field(name="승률", value=f"{result['win_rate']:.1f}%", inline=True)
        
        logs = result.get('logs', [])
        if logs:
            log_txt = "\n".join(logs[-5:])
            if len(log_txt) > 1000: log_txt = log_txt[:1000] + "..."
            embed.add_field(name="최근 로그", value=f"```\n{log_txt}\n```", inline=False)
        
        await ctx.send(embed=embed)
    else:
        await ctx.send("❌ 백테스트 실패 (결과 없음)")

@bot.event
async def on_ready():
    print(f"✅ {bot.user} 접속 성공!")
    
    try:
        print("⏳ 바이낸스 마켓 데이터 로딩 중...")
        await asyncio.to_thread(binance.load_markets)
        print("✅ 바이낸스 로딩 완료")
    except Exception as e:
        print(f"❌ 바이낸스 로딩 실패: {e}")
        
    await update_dashboard()
    key_monitoring_loop.start()

bot.run(TOKEN)
