-- EXPLAIN (ANALYZE 는 시간이 매번 달라 golden 에 넣지 않는다)
EXPLAIN SELECT * FROM users;
EXPLAIN SELECT name FROM users WHERE age > 30 AND name = 'kim' LIMIT 3;
EXPLAIN SELECT * FROM users WHERE age = '25' AND name = NULL;
EXPLAIN SELECT * FROM users WHERE 5 < age;
EXPLAIN INSERT INTO users (name) VALUES ('a');
EXPLAIN INSERT INTO users (name) VALUES ('a'), ('b');
SELECT * FROM users;
