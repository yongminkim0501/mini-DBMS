-- 기본 INSERT / SELECT
INSERT INTO users (name, age) VALUES ('kim', 25);
INSERT INTO users (name, age) VALUES ('lee', 31);
SELECT * FROM users;
SELECT name FROM users;
SELECT age, name, age FROM users LIMIT 1;
SELECT * FROM users LIMIT 0;
SELECT * FROM public.users WHERE id = 2;
