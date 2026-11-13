-- 인덱스 접근 경로별 EXPLAIN 과 결과 (ANALYZE 는 시간이 매번 달라 넣지 않는다)
INSERT INTO users (name, age) VALUES ('kim', 25), ('lee', 31), ('park', NULL), (NULL, 40), ('choi', 31), ('한글', 7);
EXPLAIN SELECT * FROM users WHERE id = 3;
SELECT * FROM users WHERE id = 3;
EXPLAIN SELECT * FROM users WHERE id >= 2 AND id < 5;
SELECT * FROM users WHERE id >= 2 AND id < 5;
EXPLAIN SELECT name FROM users WHERE id > 1 AND id <= 6 AND age > 30 LIMIT 1;
SELECT name FROM users WHERE id > 1 AND id <= 6 AND age > 30 LIMIT 1;
EXPLAIN SELECT * FROM users WHERE id > 5 AND id < 3;
SELECT * FROM users WHERE id > 5 AND id < 3;
EXPLAIN SELECT * FROM users WHERE id > 1180591620717411303424;
EXPLAIN SELECT * FROM users WHERE id < 1180591620717411303424;
EXPLAIN SELECT * FROM users WHERE id <> 3;
EXPLAIN SELECT * FROM users WHERE name = 'kim';
EXPLAIN SELECT * FROM users WHERE '4' = id;
SELECT * FROM users WHERE '4' = id;
