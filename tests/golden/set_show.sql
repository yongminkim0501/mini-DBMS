-- args: --on-error continue
-- SET 으로 같은 쿼리의 계획이 바뀌는지, SET/SHOW 오류
SHOW enable_indexscan;
EXPLAIN SELECT * FROM users WHERE id = 1;
SET enable_indexscan = off;
SHOW enable_indexscan;
EXPLAIN SELECT * FROM users WHERE id = 1;
SET enable_indexscan TO on;
EXPLAIN SELECT * FROM users WHERE id = 1;
SET enable_indexscan = 'false';
SHOW ENABLE_INDEXSCAN;
SET enable_indexscan TO DEFAULT;
SHOW enable_indexscan;
SET enable_seqscan = off;
SET enable_indexscan = maybe;
SET enable_indexscan = 2;
SHOW work_mem;
SET enable_indexscan = on, off;
SHOW ALL;
