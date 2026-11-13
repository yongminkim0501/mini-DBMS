INSERT INTO users (name, age) VALUES ('홍길동', 31), ('김', NULL), (NULL, 7), ('Ａｂ전각', 1);
INSERT INTO users (name, age) VALUES ('줄바꿈
두 번째 줄', 2);
SELECT * FROM users;
SELECT name FROM users WHERE name = '홍길동';
SELECT id, age FROM users WHERE age = NULL;
SELECT age, name FROM users WHERE id < 3;
