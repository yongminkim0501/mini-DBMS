-- 다중 행 INSERT 와 RETURNING
INSERT INTO users (name, age) VALUES ('a', 1), ('b', 2), ('c', 3);
INSERT INTO users (name, age) VALUES ('d', 4), ('e', 5) RETURNING *;
INSERT INTO users (name) VALUES ('f') RETURNING id, name;
INSERT INTO users VALUES (DEFAULT, 'g', 7), (DEFAULT, 'h', 8) RETURNING age, id;
SELECT * FROM users;
