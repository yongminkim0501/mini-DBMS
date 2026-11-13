INSERT INTO users (name, age) VALUES ('kim', 25), ('lee', 31), ('park', 47);
EXPLAIN SELECT * FROM users WHERE id = 2;
EXPLAIN SELECT name FROM users WHERE id >= 2 AND age > 30;
EXPLAIN SELECT * FROM users WHERE name = 'kim' LIMIT 1;
EXPLAIN INSERT INTO users (name) VALUES ('x');
