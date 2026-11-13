-- args: --no-index
-- --no-index 로 시작하면 id 조건도 Seq Scan
SHOW enable_indexscan;
INSERT INTO users (name) VALUES ('a'), ('b'), ('c');
EXPLAIN SELECT * FROM users WHERE id = 2;
SELECT * FROM users WHERE id = 2;
