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

## 빠른 시작 (로컬)

### 1. 사전 요구사항
- Python 3.10+
- Node.js (Claude CLI 설치용)
- Gate.io 선물 API 키 (권한: 읽기 + 선물 주문)
- Claude Max 구독 (`claude -p --model opus` 호출용)

### 2. Claude CLI 설치
```bash
npm i -g @anthropic-ai/claude-code
claude login     # 브라우저에서 Max 계정 로그인
claude --version
```

### 3. 프로젝트 설치
```bash
git clone <이 저장소>
cd automatic_trader
./scripts/setup.sh           # venv + 의존성 + .env 복사
```

### 4. `.env` 설정
```dotenv
GATE_API_KEY=...
GATE_API_SECRET=...
DRY_RUN=true                 # 꼭 처음에는 true로 시작
CRYPTOPANIC_TOKEN=           # 선택 — 없으면 RSS/캘린더로 자동 대체
LOG_LEVEL=INFO
```

### 5. 프리플라이트 점검
```bash
source .venv/bin/activate
python scripts/doctor.py              # Gate / CoinGecko / Claude / 세션 확인
python scripts/doctor.py --skip-claude # Claude 왕복 테스트 생략
```
모든 체크가 초록이어야 합니다.

### 6. 테스트
```bash
./scripts/test.sh    # 34개 단위테스트 (오프라인)
```

### 7. 드라이런 실행
```bash
python -m runner.main
```
`DRY_RUN=true`이면 실제 주문이 나가지 않고 로그에만 기록됩니다 (`logs/executor.log`, `logs/manager.log`, `logs/position.log` 등). 전체 플로우(스캔 → 게이트 → 사이징 → 주문 → 보호)를 볼 수 있습니다.

### 8. 실전 전환
1. `.env`에서 `DRY_RUN=false`
2. Gate.io에서 선물 계정 마진 모드 (보통 **isolated**) 및 레버리지 상한 확인
3. 작은 잔고 (예: 50 USDT)로 1~2일 관찰
4. `logs/last_llm_verdict.json`의 PASS/WAIT 주기 확인, `logs/position.log`에서 SL/TP 부착 여부 확인

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
