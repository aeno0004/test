import discord
from discord.ext import commands
import os
import sys
import json
import asyncio
from datetime import datetime
from parallel_backtester import Backtester  # 새로 만든 백테스터 모듈

# ==========================================
# 0. 전역 변수 및 설정
# ==========================================
CONFIG_FILE = 'config.json'

if not os.path.exists(CONFIG_FILE):
    print(f"❌ 오류: '{CONFIG_FILE}' 파일이 없습니다.")
    sys.exit()

try:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
        
    TOKEN = config['DISCORD_TOKEN']
    # API 키 리스트 가져오기
    GEMINI_KEYS = config.get('GEMINI_API_KEYS', [])
    if not GEMINI_KEYS:
        # 환경 변수 등 다른 곳에서 가져오는 로직이 없다면 경고
        print("⚠️ 설정 파일에 GEMINI_API_KEYS가 없습니다.")
except Exception as e:
    print(f"❌ 설정 로드 실패: {e}")
    sys.exit()

# ==========================================
# 1. 봇 초기화
# ==========================================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# 백테스터 인스턴스 생성
backtester = Backtester(api_keys=GEMINI_KEYS)

@bot.event
async def on_ready():
    print(f"✅ {bot.user} 백테스팅 봇 가동 준비 완료")
    print("사용법: !백테스트 [기간(일)] 또는 !백테스트 [시작일] [기간(분)]")

# ==========================================
# 2. 백테스트 명령어
# ==========================================
@bot.command(name="백테스트")
async def start_backtest(ctx, arg1: str, arg2: str = None):
    """
    사용법:
    1. !백테스트 30  (최근 30일 데이터로 테스트)
    2. !백테스트 2024-01-01 1440 (2024년 1월 1일부터 1440분(24시간) 동안 테스트)
    """
    # 입력 파싱
    target_date_str = None
    days_to_fetch = 0
    duration_minutes = 0
    
    try:
        # Case 1: !백테스트 30 (최근 N일)
        days = float(arg1)
        days_to_fetch = days
        duration_text = f"최근 {days}일 ({int(days*24)}시간)"
        mode = "recent"
    except ValueError:
        # Case 2: !백테스트 2024-01-01 60 (특정 날짜)
        if arg2 is None:
            await ctx.send("❌ 사용법 오류: `!백테스트 7` 또는 `!백테스트 2024-01-01 1440`")
            return
        try:
            target_date = datetime.strptime(arg1, "%Y-%m-%d")
            duration_minutes = int(arg2)
            days_to_fetch = (duration_minutes / 1440) + 1 # 넉넉하게 데이터 수집
            target_date_str = arg1
            duration_text = f"{arg1}부터 {duration_minutes}분간"
            mode = "fixed"
        except ValueError:
            await ctx.send("❌ 날짜 형식 오류 (YYYY-MM-DD)")
            return

    status_msg = await ctx.send(f"⏳ **{duration_text}** 백테스트 준비 중...\n(데이터 수집 및 AI 분석에 시간이 소요됩니다)")

    # ---------------------------------------------------------
    # 백테스트 실행 (비동기 래핑)
    # ---------------------------------------------------------
    try:
        # 1. 실행
        # run 메서드는 (최종자산, 로그리스트, 승률, 총거래수) 등을 반환한다고 가정
        result = await asyncio.to_thread(
            backtester.run, 
            days=days_to_fetch, 
            start_date=target_date_str, 
            duration_minutes=duration_minutes if mode == "fixed" else None
        )
        
        # 2. 결과 언패킹
        final_balance = result['final_balance']
        trades = result['trades']
        roi = result['roi']
        win_rate = result['win_rate']
        logs = result['logs']

        # 3. 리포트 작성
        embed = discord.Embed(title=f"📊 백테스트 결과 리포트", description=f"기간: {duration_text}", color=0x9b59b6)
        
        # 주요 지표
        embed.add_field(name="💰 최종 자산", value=f"{int(final_balance):,}원", inline=True)
        embed.add_field(name="📈 수익률 (ROI)", value=f"**{roi:.2f}%**", inline=True)
        embed.add_field(name="🎯 승률", value=f"{win_rate:.1f}% ({len(trades)}전)", inline=True)
        
        # 상세 로그 (최근 5개만 표시)
        log_text = ""
        if logs:
            for log in logs[-10:]: # 최근 10줄
                log_text += log + "\n"
        else:
            log_text = "거래 기록이 없습니다."
            
        if len(log_text) > 1000:
            log_text = log_text[:990] + "..."
            
        embed.add_field(name="📝 최근 매매 로그", value=f"```\n{log_text}\n```", inline=False)
        
        await status_msg.edit(content="✅ 분석 완료!", embed=embed)

    except Exception as e:
        import traceback
        traceback.print_exc()
        await status_msg.edit(content=f"❌ 백테스트 중 오류 발생: {str(e)}")

@bot.command(name="종료")
async def shutdown_bot(ctx):
    await ctx.send("백테스팅 봇을 종료합니다.")
    await bot.close()

bot.run(TOKEN)
