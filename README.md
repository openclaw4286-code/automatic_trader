# automatic_trader

ICT(Inner Circle Trader) 전략 기반 암호화폐 선물 자동매매 봇.

- 거래소: Gate.io USDT 선물 (ccxt)
- 유니버스: CoinGecko 시총 상위 30 ∩ Gate.io 선물
- 분석: HTF → MTF → LTF 탑다운 (다우이론 스윙 + FVG / OB / BOS / CHoCH / Sweep)
- 리스크: Fixed Fractional (자산 1.5%, 레버리지 ≤ 50, 마진 ≤ 10%, 동시 포지션 ≤ 10)
- LLM: Claude CLI (`claude -p --model opus`)가 10분마다 뉴스를 보고 PASS / WAIT 결정

## 디렉토리

```
config.py        모든 파라미터
exchange/        Gate.io ccxt 래퍼, 유니버스
ict/             ICT 분석 엔진 (HTF/MTF/LTF 탑다운)
risk/            포지션 사이징, 레버리지, 마진 캡
news/            뉴스 & 경제 캘린더 수집기
llm/             Claude CLI 브리지 & 프롬프트
runner/          asyncio 메인 루프
utils/           로거, 세션 판단
logs/            회전 로그 파일
```

## 빠른 시작

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # 키 채우기
python -m runner.main    # DRY_RUN=true 기본
```

## 상태

구현 중. 단계별로 커밋됩니다.
