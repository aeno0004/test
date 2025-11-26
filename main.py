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
        sanitized_content = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', content)
        try:
            return json.loads(sanitized_content)
        except Exception as e:
            print(f"❌ 설정 파일 로드 중 알 수 없는 오류: {e}")
            sys.exit()

config = load_sanitized_json(CONFIG_FILE)
    
TOKEN = config['DISCORD_TOKEN']
DASHBOARD_ID = int(config.get('DISCORD_DASHBOARD_ID', 0))
EXPLAIN_ID = int(config.get('DISCORD_EXPLAIN_ID', 0))
KEY_MANAGER_ID = int(config.get('DISCORD_KEY_MANAGER_ID', 0)) 
GEMINI_KEYS_RAW = config.get('GEMINI_API_KEYS', [])

# 환율 (단순 표시용)
USD_KRW_RATE = 1450 

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
            
            if count == 0: status = "🟢 정상"
            elif count < 5: status = f"🟡 불안정 ({count}회)"
            else: status = f"🔴 오류 다수 ({count}회)"
            
            err_msg = last_err if last_err == "None" else f"⚠️ {last_err[:40]}..."
            embed.add_field(name=f"🏷️ {name}", value=f"**상태:** {status}\n**로그:** {err_msg}", inline=False)
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

# [핵심] 바이낸스 선물 API 사용
binance = ccxt.binanceusdm({
    'options': {'defaultType': 'future'},
    'enableRateLimit': True
})

# ==========================================
# 2. 헬퍼 함수
# ==========================================
def usdt_to_krw(usdt):
    """USDT -> KRW 변환 (단순 표시용)"""
    return int(usdt * USD_KRW_RATE)

async def send_split_field_embed(channel, base_embed, field_name, long_text):
    limit = 1000 
    if not long_text: long_text = "내용 없음"
    chunks = [long_text[i:i+limit] for i in range(0, len(long_text), limit)]
    
    if chunks:
        base_embed.add_field(name=field_name, value=chunks[0], inline=False)
    await channel.send(embed=base_embed)
    
    for i, chunk in enumerate(chunks[1:], start=2):
        follow_up = discord.Embed(title=f"📄 {field_name} ({i}/{len(chunks)})", description=chunk, color=base_embed.color)
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
# 3. AI 관련 함수
# ==========================================
async def ask_ai_decision(df):
    used_key = None
    try:
        if df.empty: return {"decision": "hold", "confidence": 0}
        
        recent_trend = df.tail(5)[['close', 'volume', 'RSI', 'MACD']].to_string()
        row = df.iloc[-1]
        
        # [전문가용 프롬프트 데이터]
        data_str = f"""
        [Current Market Data (5m Candle)]
        - Timestamp: {row.name}
        - Close Price: {row['close']}
        - Volume Ratio: {row['vol_ratio']:.2f} (vs 20-period Avg)
        
        [Trend Indicators]
        - EMA_50: {row['EMA50']:.2f}
        - EMA_200: {row['EMA200']:.2f}
        - Trend Status: {'Bullish (Up)' if row['EMA50'] > row['EMA200'] else 'Bearish (Down)'}
        
        [Momentum & Volatility]
        - RSI(14): {row['RSI']:.1f} (Overbought > 70, Oversold < 30)
        - MACD: {row['MACD']:.2f} (Signal: {row['MACD_Signal']:.2f})
        - ATR(14): {row['ATR']:.2f} (Use this for SL/TP calculation)
        - BB Position: {(row['close'] - row['BB_Low']) / (row['BB_Up'] - row['BB_Low']):.2f}
        
        [Recent 5 Candles History]
        {recent_trend}
        """
        
        # [월스트리트 트레이더 페르소나]
        prompt = f"""
        Act as a World-Class Bitcoin Futures Trader (Scalper).
        Your goal is to maximize profit while strictly managing risk.
        
        Based on the provided 5-minute chart data:
        1. Analyze the **Trend** using EMA and recent price action.
        2. Analyze **Momentum** using RSI and MACD.
        3. Confirm trade validity with **Volume Ratio** (High volume = Stronger signal).
        4. Determine entry direction (LONG/SHORT) or stay neutral (HOLD).
        
        **Risk Management Rules:**
        - Set Stop Loss (SL) at 1.5 * ATR from entry price.
        - Set Take Profit (TP) at 2.0 * ATR from entry price (Risk:Reward = 1:1.3+).
        - If the trend is ambiguous or signals conflict, choose "HOLD".
        
        Data:
        {data_str}
        
        Strict Output JSON:
        {{"decision": "long/short/hold", "confidence": 0-100, "sl": price, "tp": price, "reason": "Brief logic in Korean"}}
        """
        
        used_key = key_manager.get_key()
        if not used_key: raise Exception("No API Keys available")
        
        genai.configure(api_key=used_key)
        model = genai.GenerativeModel('gemini-2.5-flash') # [유지] 2.5 Flash
        
        response = await asyncio.to_thread(model.generate_content, prompt)
        text = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        print(f"⚠️ AI Error: {e}")
        if used_key: key_manager.report_error(used_key, e)
        return {"decision": "hold", "confidence": 0}

async def translate_reason(text):
    used_key = key_manager.get_key()
    if not used_key: return text
    try:
        genai.configure(api_key=used_key)
        model = genai.GenerativeModel('gemini-2.5-flash')
        prompt = f"Translate this trading reasoning into natural Korean:\n'{text}'"
        response = await asyncio.to_thread(model.generate_content, prompt)
        return response.text.strip()
    except: return text

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
# 4. 실시간 루프 (Binance 기준)
# ==========================================
async def update_dashboard():
    global dashboard_msg
    ch_dash = bot.get_channel(DASHBOARD_ID)
    if not ch_dash: return

    try:
        ticker = await asyncio.to_thread(binance.fetch_ticker, "BTC/USDT")
        current_usdt_price = ticker['last']
    except:
        current_usdt_price = 0

    if live_wallet:
        bal_usdt = live_wallet.get_balance()
        initial_usdt = live_wallet.initial_balance
        unrealized_usdt = live_wallet.get_unrealized_pnl(current_usdt_price) if live_wallet.position else 0
        total_equity_usdt = bal_usdt + unrealized_usdt
        
        total_roi = ((total_equity_usdt - initial_usdt) / initial_usdt * 100) if initial_usdt > 0 else 0
        
        status_text = "💤 관망 중 (Binance USDT)"
        pnl_text = "-"
        entry_text = "-"
        sl_tp_text = "-"
        color = 0x95a5a6 
        
        if live_wallet.position:
            pos = live_wallet.position
            side = pos['type'].upper()
            status_text = f"🔥 {side} 포지션 (Binance)"
            color = 0x2ecc71 if total_roi >= 0 else 0xe74c3c
            
            # [USDT / KRW 병기]
            pnl_krw = usdt_to_krw(unrealized_usdt)
            pnl_rate_curr = (unrealized_usdt / pos['invested_krw']) * 100 
            
            pnl_text = f"${unrealized_usdt:.2f} ({pnl_rate_curr:+.2f}%)\n≈ {pnl_krw:,}원"
            entry_text = f"${pos['entry_price']:.2f}"
            
            sl = pos.get('sl')
            tp = pos.get('tp')
            sl_text = f"${sl:.2f}" if sl else "-"
            tp_text = f"${tp:.2f}" if tp else "-"
            sl_tp_text = f"SL: {sl_text} | TP: {tp_text}"
            
        desc = f"Last Update: {datetime.now().strftime('%H:%M:%S')}\nMarket: Binance Futures (USDT)"
    else:
        status_text = "⛔ 봇 대기 중"
        color = 0x2f3136
        current_usdt_price = 0
        total_roi = 0.0
        total_equity_usdt = 0
        pnl_text = "-"
        entry_text = "-"
        sl_tp_text = "-"
        desc = "봇 준비 완료. `!테스트매매시작`을 입력하세요."

    equity_krw = usdt_to_krw(total_equity_usdt)
    curr_price_krw = usdt_to_krw(current_usdt_price)

    embed = discord.Embed(title="🔴 실시간 AI 트레이딩 (Binance)", description=desc, color=color)
    embed.add_field(name="BTC 현재가", value=f"**${current_usdt_price:,.2f}**\n(≈{curr_price_krw:,}원)", inline=True)
    embed.add_field(name="누적 수익률", value=f"**{total_roi:+.2f}%**", inline=True)
    embed.add_field(name="총 평가 자산", value=f"${total_equity_usdt:,.2f}\n(≈{equity_krw:,}원)", inline=True)
    
    embed.add_field(name="상태", value=status_text, inline=False)
    embed.add_field(name="진입가 (USDT)", value=entry_text, inline=True)
    embed.add_field(name="평가 손익", value=pnl_text, inline=True)
    embed.add_field(name="전략 (USDT)", value=sl_tp_text, inline=False)
    
    embed.set_footer(text="Binance USDT 마켓 기준 (10초 갱신)")

    try:
        if dashboard_msg: await dashboard_msg.edit(embed=embed)
        else: 
            async for msg in ch_dash.history(limit=5):
                if msg.author == bot.user: await msg.delete()
            dashboard_msg = await ch_dash.send(embed=embed)
    except: pass

@tasks.loop(seconds=10)
async def key_monitoring_loop():
    global key_dashboard_msg
    ch = bot.get_channel(KEY_MANAGER_ID)
    if not ch: return
    try:
        if key_dashboard_msg: await key_dashboard_msg.edit(embed=key_manager.get_status_embed())
        else: key_dashboard_msg = await ch.send(embed=key_manager.get_status_embed())
    except: pass

@tasks.loop(seconds=10)
async def live_trading_loop():
    global is_live_active, live_wallet
    if not is_live_active or not live_wallet: return

    try:
        await update_dashboard()
        
        try:
            ohlcv = await asyncio.to_thread(binance.fetch_ohlcv, "BTC/USDT", "5m", limit=200)
            if not ohlcv: return
            df_binance = pd.DataFrame(ohlcv, columns=['datetime', 'open', 'high', 'low', 'close', 'volume'])
            df_binance['datetime'] = pd.to_datetime(df_binance['datetime'], unit='ms')
            df_binance.set_index('datetime', inplace=True)
            
            df_binance = brain.calculate_indicators(df_binance)
            if df_binance.empty: return
            current_price = df_binance['close'].iloc[-1]
        except Exception as e:
            print(f"Data Fetch Error: {e}")
            return

        if live_wallet.position:
            pos = live_wallet.position
            sl_price = pos['sl']
            tp_price = pos['tp']
            close_reason = None
            
            if pos['type'] == 'long':
                if sl_price and current_price <= sl_price: close_reason = "Stop Loss 🔵"
                elif tp_price and current_price >= tp_price: close_reason = "Take Profit 🔴"
            elif pos['type'] == 'short':
                if sl_price and current_price >= sl_price: close_reason = "Stop Loss 🔵"
                elif tp_price and current_price <= tp_price: close_reason = "Take Profit 🔴"
            
            if close_reason:
                trade_result = live_wallet.close_position(current_price, reason=close_reason)
                ch = bot.get_channel(EXPLAIN_ID)
                if ch:
                    pnl_krw = usdt_to_krw(trade_result['pnl'])
                    color = 0x00ff00 if trade_result['pnl'] > 0 else 0xff0000
                    
                    embed = discord.Embed(title=f"⚡ 포지션 종료: {close_reason}", color=color)
                    embed.add_field(name="수익금", value=f"${trade_result['pnl']:.2f} (≈{pnl_krw:,}원)", inline=True)
                    embed.add_field(name="수익률", value=f"{trade_result['profit_rate']:.2f}%", inline=True)
                    await ch.send(embed=embed)
                    
                    if trade_result['pnl'] < 0:
                        feedback = await analyze_failure(trade_result, df_binance)
                        await send_split_description_embed(ch, "😭 전문 트레이더의 팩트 폭격", feedback, 0x000000)

        if live_wallet.position is None:
            if 10 <= datetime.now().second <= 20: 
                decision = await ask_ai_decision(df_binance)
                
                if decision['confidence'] >= 70 and decision['decision'] in ['long', 'short']:
                    side = decision['decision']
                    reason_kr = await translate_reason(decision.get('reason', 'No reason'))
                    
                    # [요청반영] 잔고의 99% (수수료 제외 풀배팅)
                    balance = live_wallet.get_balance()
                    invest_amount = balance * 0.99 
                    
                    sl = decision.get('sl')
                    tp = decision.get('tp')
                    if not sl or sl == 0:
                        sl = current_price * 0.98 if side == 'long' else current_price * 1.02
                    if not tp or tp == 0:
                        tp = current_price * 1.04 if side == 'long' else current_price * 0.96

                    live_wallet.enter_position(side, current_price, invest_amount, sl=sl, tp=tp)
                    
                    ch = bot.get_channel(EXPLAIN_ID)
                    if ch:
                        embed = discord.Embed(title=f"🚀 AI 진입 신호: {side.upper()}", color=0x0000ff)
                        embed.add_field(name="확신도", value=f"{decision['confidence']}%", inline=True)
                        embed.add_field(name="진입가", value=f"${current_price:,.2f}", inline=True)
                        await send_split_field_embed(ch, embed, "판단 이유", reason_kr)
                    
                    await update_dashboard()
                    await asyncio.sleep(10)

    except Exception as e:
        print(f"🔥 Live Loop Error: {e}")
        traceback.print_exc()
        await asyncio.sleep(5)

@bot.command(name="테스트매매시작")
async def start_live_trading(ctx):
    global is_live_active, live_wallet, dashboard_msg
    if is_live_active:
        await ctx.send("⚠️ 이미 실행 중입니다.")
        return
    
    # [변경] 1000 USDT 시작
    live_wallet = FuturesWallet(initial_balance=1000)
    is_live_active = True
    dashboard_msg = None 
    
    await ctx.send("🚀 **Binance 실전 모의투자** 시작! (초기자금: 1,000 USDT)")
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
    await ctx.send("🤖 봇을 종료합니다.")
    await bot.close()

@bot.command(name="백테스트")
async def start_backtest(ctx, arg1: str, arg2: str = None):
    try:
        days = float(arg1)
        await ctx.send(f"⏳ 최근 {days}일 백테스트 시작...")
        result = await asyncio.to_thread(backtester.run, days=days)
    except ValueError:
        if arg2 is None:
            await ctx.send("❌ 사용법 오류: `!백테스트 7` 또는 `!백테스트 2024-01-01 1440`")
            return
        try:
            datetime.strptime(arg1, "%Y-%m-%d")
            duration = int(arg2)
            days_needed = (duration / 1440) + 2
            await ctx.send(f"⏳ {arg1}부터 {duration}분간 백테스트 시작...")
            result = await asyncio.to_thread(backtester.run, days=days_needed, start_date=arg1, duration_minutes=duration)
        except ValueError:
             await ctx.send("❌ 날짜 형식(YYYY-MM-DD) 또는 기간(분)이 잘못되었습니다.")
             return

    if result:
        embed = discord.Embed(title="📊 백테스트 결과", color=0x9b59b6)
        embed.add_field(name="최종 자산", value=f"${int(result['final_balance']):,} (USDT)", inline=True)
        embed.add_field(name="수익률", value=f"{result['roi']:.2f}%", inline=True)
        embed.add_field(name="승률", value=f"{result['win_rate']:.1f}%", inline=True)
        
        logs = result.get('logs', [])
        if logs:
            # [수정] 전체 로그 출력 시도 + 파일 첨부 기능
            all_logs_txt = "\n".join(logs)
            if len(all_logs_txt) > 1000:
                # 파일로 저장하여 보내기
                with open("backtest_logs.txt", "w", encoding="utf-8") as f:
                    f.write(all_logs_txt)
                file = discord.File("backtest_logs.txt")
                embed.add_field(name="전체 로그", value="📄 내용이 많아 파일로 첨부합니다.", inline=False)
                await ctx.send(embed=embed, file=file)
                os.remove("backtest_logs.txt") # 전송 후 삭제
            else:
                embed.add_field(name="전체 로그", value=f"```\n{all_logs_txt}\n```", inline=False)
                await ctx.send(embed=embed)
        else:
            await ctx.send(embed=embed)
    else:
        await ctx.send("❌ 백테스트 실패 (결과 없음)")

@bot.event
async def on_ready():
    print(f"✅ {bot.user} 접속 성공! (Binance Mode)")
    try:
        print("⏳ 바이낸스 마켓 데이터 로딩 중...")
        await asyncio.to_thread(binance.load_markets)
        print("✅ 바이낸스 로딩 완료")
    except Exception as e:
        print(f"❌ 바이낸스 로딩 실패: {e}")
    await update_dashboard()
    key_monitoring_loop.start()

bot.run(TOKEN)
