INSERT INTO users (name, age) VALUES ('kim', 25), ('lee', 31);
INSERT INTO users (name, age) VALUES ('park', 47) RETURNING *;
INSERT INTO users (name) VALUES ('choi') RETURNING id, name;
SELECT * FROM users;
SELECT name, age FROM users WHERE age > 30;
SELECT * FROM users WHERE id = 2;
SELECT id FROM users WHERE id >= 2 LIMIT 2;
SELECT * FROM users WHERE id = 100;
