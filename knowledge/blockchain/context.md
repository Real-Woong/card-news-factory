# Blockchain 도메인 상시 맥락 (evergreen)

RAG `knowledge` 컬렉션에 색인됩니다. 자주 바뀌지 않는 배경 지식만 적습니다.

## 용어 기준

- **L1 / L2 / rollup**: rollup은 실행을 L2에서, 데이터/정산을 L1에서 처리한다.
  optimistic(사기 증명)과 zk(유효성 증명)는 신뢰 가정이 다르다.
- **blob (EIP-4844)**: rollup 데이터 전용 공간. 실행 gas와 별도 수수료 시장을 가진다.
- **client diversity**: execution client와 consensus client는 별개이며, 특정 클라이언트
  점유율이 높으면 네트워크 리스크가 된다.
- **upgrade 명칭**: devnet → testnet → mainnet 순서. devnet 결과는 확정 스펙이 아니다.
- **account abstraction (ERC-4337)**: bundler, paymaster, entrypoint 역할 구분.

## 초안 작성 시 반복되는 판단 기준

- "빨라졌다"는 finality, block time, TPS 중 무엇인지 명시한다.
- 테스트넷 수치를 메인넷 수치처럼 쓰지 않는다.
- 토큰 가격, 시세 전망, 투자 판단은 다루지 않는다. 프로토콜/도구 변화만 다룬다.
- 노드 운영자에게 영향이 있는 변경(디스크, 대역폭, 강제 업그레이드 기한)을 우선한다.
- 감사(audit) 미완료 코드는 그 사실을 함께 적는다.

## 과거에 반복적으로 다룬 주제

- blob 처리량과 L2 데이터 비용
- 클라이언트 릴리스와 강제 업그레이드 일정
- zk 증명 비용 절감
- 수수료 시장 구조 변경

<!-- 새 항목은 날짜와 함께 추가하세요. -->
