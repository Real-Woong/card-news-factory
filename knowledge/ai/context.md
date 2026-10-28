# AI 도메인 상시 맥락 (evergreen)

이 파일은 RAG `knowledge` 컬렉션에 색인됩니다. 기사 하나만 보면 놓치는 배경을
에이전트가 참고할 수 있도록, 자주 바뀌지 않는 사실만 적습니다.

## 용어 기준

- **foundation model / frontier model**: 대규모 사전학습 모델. 벤더별 "frontier" 정의가
  다르므로 초안에서는 벤더 표현을 그대로 인용한다.
- **open weights ≠ open source**: 가중치 공개와 라이선스 개방은 별개다. 라이선스 이름
  (Apache 2.0, Llama Community License 등)을 반드시 확인한다.
- **availability 단계**: research preview → private beta → public beta → GA.
  단계가 다르면 사용 가능 범위와 SLA가 다르다.
- **MCP (Model Context Protocol)**: 모델-툴 연결 표준. 커넥터/서버/클라이언트 구분.

## 초안 작성 시 반복되는 판단 기준

- 벤더가 발표한 벤치마크는 "벤더 주장"으로 표기한다. 독립 측정과 구분한다.
- 가격 변경은 단위(1M tokens, per request, per seat)를 반드시 함께 쓴다.
- 컨텍스트 윈도우 수치는 입력/출력 합계인지 별도인지 확인한다.
- "추론 속도 개선"은 하드웨어, 배치 크기, 양자화 여부 없이는 의미가 없다.

## 과거에 반복적으로 다룬 주제

- 추론 서빙 최적화 (KV cache, prefix caching, 양자화)
- 에이전트 신뢰성과 장기 태스크 평가
- RAG 파이프라인 구성 변화 (청킹, 리랭킹, 압축)
- 개발자 도구 가격/한도 변경

<!-- 새 항목은 날짜와 함께 추가하세요. 예: 2026-09 OpenAI structured outputs GA -->
