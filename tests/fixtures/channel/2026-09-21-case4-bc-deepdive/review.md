# R_A_Glance 검수 — 2026-09-21-case4-bc-deepdive

- 판정: **게시 가능** (publishable = true)
- 토픽: Solana Changelog: September 3, 2026
- topic_id: `246f2af5ef898b11fc03128c1d1b74483adfd7738de5d4b1e4d8adc8b0cae582`
- fact check: **partially_verified**
- 템플릿: `deep_dive` (7 slides)
- 렌더 백엔드: `chrome_cli`
- LLM: {'calls': 3, 'failures': 0, 'credits': 0.62, 'seconds': 35.0, 'calls_per_task': {'ig_write': 1, 'ig_edit': 1, 'ig_caption': 1}}

## 템플릿 선택 근거

- planner: CLI --template deep_dive 로 지정
- 독자: -

## 품질 게이트

- [확인] (single_source) 출처가 1개입니다 — 교차 확인 권장 — `slide 7`
- [확인] (partially_verified) verdict=partially_verified — 게시 전 원문과 수치/범위를 대조하세요

검사 항목: slide_count, slide_indexes, slide_types, headline, sources, caption, hashtags, text_density, closing_fill, internal_language, banned_phrases, slide_urls, fact_check, render

## 원문 대조용 정보 (공개 출력에 포함하지 않음)

출처:

- Solana News: https://solana.com/news/solana-changelog-september-3-2026

엔진 research 요약:

- what_changed: The September 3, 2026 Solana Changelog reports three mainnet feature gates (lamports per byte set to 6333, relaxation of the post-execution min_balance check, BPF Stake Program upgraded to v5.1.0) plus new client releases including Agave v4.4.0-alpha.2, Web3.js v3.0.0-rc.3, and Solana Kit v8.2.0. Firedancer adopted three RPC calls (getSlotLeader, getSlotLeaders, getLeaderSchedule) and added ARM validator support.
- why_it_matters: Operators must note the mainnet lamports-per-byte change to 6333 and the BPF Stake Program v5.1.0 upgrade as they affect storage cost accounting and staking behavior. Firedancer's ARM support and new RPC-spec compliance broaden deployment options and improve client interoperability for validator operators and RPC consumers.

- key facts:
  - Mainnet feature gate sets lamports per byte to 6333
  - Mainnet feature gate relaxes the post-execution min_balance check
  - Mainnet feature gate upgrades the BPF Stake Program to v5.1.0
  - Devnet and Testnet add Transaction V1 and Custom Commission Collector feature gates; Testnet sets lamports per byte to 5080
  - New Agave versions: v4.4.0-alpha.2, v4.3.0-beta.3, v4.2.2
  - New Solana Web3.js versions: v3.0.0-rc.3 and v1.99.0-beta.0
  - New Solana Kit versions: v8.2.0 and v8.1.0
  - Superbank v0.6.0-rc1 released
  - Firedancer adopted three RPC calls: getSlotLeader, getSlotLeaders, and getLeaderSchedule, all part of the Solana RPC spec
  - Firedancer will support running its validator client on ARM systems by finding alternative instructions to x86_64-only ones
  - Firedancer will use a class-based memory allocator for its program cache instead of heap-based allocation
  - Agave will order same-priority transactions based on arrival time
  - Agave now disallows private or local addresses from being used by the TPU vote service, TPU forwards service, and Votor
  - Issue is dated September 3, 2026 and was published September 10, 2026 by the Solana Foundation

## 에디터 변경 내역

- slide 2: '신규 Release가 함께 공개'를 '신규 Release 동시 공개'로 압축
- slide 5: 마지막 문장 '사용을 차단'을 '차단'으로 압축(사실 유지)
- 기타 슬라이드: 검증된 숫자·버전·날짜 유지, banned/never_write 표현 없어 변경 없음

## 렌더 결과

- slide 01: 01.png (1080x1350, fit=ok, scale=1, 1086/1086 slide 1350/1350)
- slide 02: 02.png (1080x1350, fit=ok, scale=1, 1086/1086 slide 1350/1350)
- slide 03: 03.png (1080x1350, fit=ok, scale=1, 1086/1086 slide 1350/1350)
- slide 04: 04.png (1080x1350, fit=ok, scale=1, 1086/1086 slide 1350/1350)
- slide 05: 05.png (1080x1350, fit=ok, scale=1, 1086/1086 slide 1350/1350)
- slide 06: 06.png (1080x1350, fit=ok, scale=1, 1086/1086 slide 1350/1350)
- slide 07: 07.png (1080x1350, fit=ok, scale=1, 931/931 slide 1350/1350)

## 사람이 할 일

1. `[차단]` 항목 해결 (없으면 게시 가능)
2. `preview.html` 로 5장 흐름·줄바꿈 확인
3. 숫자·버전·날짜를 위 출처 원문과 대조
4. `caption.md` 본문만 복사해 게시 (metadata 주석 제외)

생성 2026-09-21T03:37:09.568100+00:00
