# automatic_trader

ICT (Inner Circle Trader) 전략 기반 암호화폐 선물 자동매매 봇.

- 거래소: Gate.io USDT 선물 (ccxt)
- 유니버스: CoinGecko 시총 상위 30 ∩ Gate.io 선물
- 분석: HTF → MTF → LTF 탑다운 (다우이론 스윙 + FVG / OB / BOS / CHoCH / Sweep)
- 리스크: Fixed Fractional (자산 1.5%, 레버리지 ≤ 50, 마진 ≤ 10%, 동시 포지션 ≤ 10)
- 포지션 관리: SL/TP 보호 가드, 부분 익절(1R, 50%), 손익분기(BE) 이동, 트레일링(1.5R 활성/0.8R 추적), 보호 실패 시 시장가 즉시 청산
- LLM: Claude CLI (`claude -p --model opus`)가 10분마다 뉴스/경제 캘린더를 보고 PASS / WAIT 결정

## 디렉토리

```
config.py                모든 파라미터 (세션, 타임프레임, 리스크, 익절/트레일, LLM, 로깅)
exchange/                Gate.io ccxt 래퍼, CoinGecko 유니버스
ict/                     ICT 분석 엔진 (HTF/MTF/LTF 탑다운, FVG/OB/BOS/CHoCH/Sweep)
risk/                    포지션 사이징, 포트폴리오 게이트, 익절 계산
news/                    CryptoPanic + RSS(Coindesk/Cointelegraph) + ForexFactory + Investing
llm/                     프롬프트 빌더, Claude CLI 브리지, PASS/WAIT 게이트
runner/                  scanner, executor, position_manager, manager, main(asyncio)
utils/                   로거, 세션 판단
scripts/                 setup.sh, doctor.py, test.sh
tests/                   오프라인 단위테스트 (34개)
logs/                    회전 로그 + last_llm_prompt.txt / last_llm_response.txt / last_llm_verdict.json
```

## 빠른 시작 (로컬 — 복사-붙여넣기)

### 0. 사전 요구사항
- **Python 3.10+**
- **Node.js 18+** (Claude CLI 설치용)
- **Gate.io 선물 API 키** (권한: 읽기 + 선물 주문)
- **Claude Max 구독** — `claude -p --model opus` 호출에 필요

### 1. Claude CLI 설치 & 로그인
```bash
npm i -g @anthropic-ai/claude-code
claude login              # 브라우저에서 Max 계정 로그인
claude --version
```

### 2. 저장소 클론 & 브랜치 체크아웃
```bash
git clone https://github.com/openclaw4286-code/automatic_trader.git
cd automatic_trader
git checkout claude/ict-trading-bot-fbgs8
```

### 3. 대화형 세팅 (venv + 의존성 + API 키 입력)
```bash
./scripts/setup.sh
```
스크립트가 터미널에서 순서대로 물어봅니다:
- `Gate.io API key` (입력 시 가려짐)
- `Gate.io API secret` (가려짐)
- `CryptoPanic token` *(선택)*
- `NewsAPI key` *(선택)*
- `claude CLI binary` *(기본 `claude`)*
- `start in DRY_RUN mode` *(기본 Y — 처음엔 반드시 Y)*
- `log level` *(기본 INFO)*

입력한 값은 `.env` (퍼미션 600)로 저장됩니다. 다시 실행해도 기존 `.env`는 보존되며, 덮어쓸지 묻습니다.

### 4. 프리플라이트 점검
```bash
source .venv/bin/activate
python scripts/doctor.py              # Gate / CoinGecko / Claude / 세션 확인
# Claude 왕복 테스트를 생략하려면
python scripts/doctor.py --skip-claude
```
모든 체크가 초록이어야 합니다.

### 5. 오프라인 테스트 34개
```bash
./scripts/test.sh
```

### 6. 드라이런 실행
```bash
python -m runner.main
```
`DRY_RUN=true`이면 실제 주문이 나가지 않고 로그에만 기록됩니다 (`logs/executor.log`, `logs/manager.log`, `logs/position.log`). 전체 플로우(스캔 → 게이트 → 사이징 → 주문 → 보호)를 눈으로 확인할 수 있습니다.

### 7. 실전 전환

실전은 반드시 `./scripts/go_live.sh`로만 시작. `DRY_RUN=false`만 켜고
`python -m runner.main`을 쳐도 런타임 가드가 막습니다
(`LIVE_CONFIRMED` env 필요).

```bash
./scripts/go_live.sh                 # 대화형 (보수 오버라이드 입력)
./scripts/go_live.sh --defaults      # config.py 원값 (1.5% / x50 / 10 pos) 그대로
```

스크립트가 하는 일:
1. 현재 Gate.io 잔고·API 키·DRY_RUN 상태 표시
2. 보수적 오버라이드 입력받음 (첫 실거래 권장):
   - `RISK_PER_TRADE` = 0.5%
   - `MAX_CONCURRENT_POSITIONS` = 1
   - `LEVERAGE_MAX` = 10
3. `I UNDERSTAND` 입력해야 진행
4. `DRY_RUN=false LIVE_CONFIRMED=true`로 봇 기동
5. 핵심 4개 로그(`executor`, `position`, `manager`, `llm_gate`) 실시간 tail

Ctrl+C = graceful shutdown. SL/TP 부착이 거부되면 PositionManager가
자동 시장가 청산하므로 보호 없는 포지션은 남지 않습니다.

**Gate.io 계정 체크리스트:**
1. 선물 **isolated 마진** 모드 확인
2. 잔고 소액(20~50 USDT)부터
3. 첫 시그널 체결 시 Gate 웹 UI "Active Orders"에 SL + TP 두 개 모두 보이는지 확인
4. `logs/last_llm_verdict.json`에서 PASS/WAIT 주기 확인
5. 문제 없으면 `.env`에 영구 반영:
   ```bash
   sed -i '' 's/^DRY_RUN=.*/DRY_RUN=false/' .env
   LIVE_CONFIRMED=true python -m runner.main
   ```

## 주요 주기

| 루프 | 간격 | config |
| --- | --- | --- |
| ICT 스캔 (HTF/MTF/LTF 탑다운) | **60초** | `SCAN_INTERVAL_SEC` |
| LLM PASS/WAIT 판단 | **600초 (10분)** | `LLM_GATE_INTERVAL_SEC` |
| 포지션 모니터 (보호/부분익절/트레일) | **15초** | `POSITION_POLL_SEC` |
| 미체결 entry 주문 TTL | **600초** | `ORDER_TTL_SEC` |

## 세션 윈도우 (KST)
```
09:00-12:00, 13:00-15:00
16:00-20:00, 21:00-00:30
22:30-01:00, 02:00-05:00
```
세션 밖이면 스캔 스킵 (LLM/모니터 루프는 계속 돎).

## LLM 프롬프트·판단 기록
`logs/` 하위 3개 파일은 **매 사이클마다 덮어쓰기** — 과거 이력 보관 없음:
- `last_llm_prompt.txt` — 실제로 보낸 프롬프트
- `last_llm_response.txt` — Claude 원응답
- `last_llm_verdict.json` — 파싱된 판단 (`status`, `reason`, `allow`, ISO 타임스탬프, `news_count`)

## 포지션 관리 로직
1. **보호 가드**: 진입 체결 직후 SL + TP 부착. 미부착 시 `PROTECTION_MAX_RETRY=2`회 재시도 후 실패면 시장가 **즉시 청산**
2. **주기적 재검증**: 15초마다 `open_orders`에서 SL·TP id가 살아있는지 확인, 사라졌으면 재부착, 그것도 실패하면 청산
3. **부분 익절 (1R, 50%)**: 가격이 TP1(=진입±1R) 도달 시 50% 청산 → SL을 진입가(BE)로 이동
4. **트레일링**: 1.5R 이상 유리하게 움직이면 best_price에서 0.8R 뒤로 SL 이동. 타이트해지기만 하고 느슨해지지 않음

## 주의
- 본 코드는 교육/연구용. 실거래 손실에 대한 책임은 사용자 본인에게 있음
- Gate.io 선물 규칙(tick/lot size, 레버리지 상한, 파생상품 가능 국가) 준수 필요
- 드라이런에서 충분히 관찰 후 실전 전환 권장
