-- NULL 과 DEFAULT
INSERT INTO users (name, age) VALUES (NULL, NULL), ('x', DEFAULT), (DEFAULT, 9);
INSERT INTO users VALUES (DEFAULT);
INSERT INTO users VALUES (DEFAULT, 'only-name');
INSERT INTO users (id, name) VALUES (DEFAULT, 'id-default') RETURNING *;
SELECT * FROM users;
-- NULL 과의 비교는 UNKNOWN: 어떤 행도 통과하지 못한다
SELECT * FROM users WHERE age = NULL;
SELECT * FROM users WHERE name <> NULL;
SELECT id FROM users WHERE age > 0;
