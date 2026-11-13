"""PostgreSQL 프론트엔드/백엔드 프로토콜 v3 (psql, pg8000, pgbench 가 그대로 접속).

- :mod:`.messages`  메시지 인코딩/디코딩 (바이트만 다룬다)
- :mod:`.protocol`  연결 하나의 상태 기계 (시작 단계, 단순 쿼리, 확장 쿼리, 취소 플래그)
- :mod:`.service`   Phase 3 서버의 디스패처·워커 풀에 붙이는 처리기 (취소 요청은 디스패처에서 바로 처리)

설계와 메시지 흐름은 docs/pg-protocol.md.
"""
