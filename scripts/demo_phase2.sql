-- Phase 2 시연: id 인덱스 (users_pkey) 사용/미사용 비교
-- 전제: scripts/gen_insert_sql.py --seed 42 로 만든 100만 건을 빈 DB 에 적재 (README "Phase 2 시연")
-- 실행: python -m minidb run scripts/demo_phase2.sql --db ./db --echo
-- 모든 문장이 같은 세션에서 실행되므로 SET 이 이후 문장에 적용된다.

-- 1) 인덱스 점 검색: B+ 트리에서 id → offset 을 찾아 레코드 하나만 읽는다
SHOW enable_indexscan;
SELECT * FROM users WHERE id = 777777;
EXPLAIN ANALYZE SELECT * FROM users WHERE id = 777777;

-- 2) 인덱스를 끄면 같은 쿼리가 100만 건 전체 스캔이 된다
SET enable_indexscan = off;
EXPLAIN ANALYZE SELECT * FROM users WHERE id = 777777;

-- 3) 다시 켜기
SET enable_indexscan = on;
EXPLAIN ANALYZE SELECT * FROM users WHERE id = 777777;

-- 4) 범위 검색: 시작 리프를 한 번 찾고 리프 연결 리스트를 따라간다 (1,000건)
EXPLAIN ANALYZE SELECT * FROM users WHERE id >= 500000 AND id < 501000;
-- id 외 조건은 같은 노드의 Filter 로 붙는다
EXPLAIN ANALYZE SELECT * FROM users WHERE id >= 500000 AND id < 501000 AND age > 90;
-- LIMIT 이 있으면 범위 스캔도 필요한 만큼만 읽고 멈춘다
EXPLAIN ANALYZE SELECT * FROM users WHERE id > 500000 LIMIT 5;

-- 5) id 가 아닌 컬럼(name) 조건은 인덱스가 없으므로 Seq Scan (선형 탐색)
SELECT * FROM users WHERE name = 'N궻쿓긷퉵';
EXPLAIN ANALYZE SELECT * FROM users WHERE name = 'N궻쿓긷퉵';
