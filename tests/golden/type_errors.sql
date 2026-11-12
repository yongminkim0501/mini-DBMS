-- args: --on-error continue
-- 타입 변환과 각종 오류 (캐럿, DETAIL, HINT)
INSERT INTO users (name, age) VALUES (25, '30');
INSERT INTO users (name, age) VALUES ('spaces', ' 7 ');
SELECT * FROM users;
INSERT INTO users (name, age) VALUES ('bad', 'abc');
INSERT INTO users (name, age) VALUES ('big', 9223372036854775808);
INSERT INTO users (name, age) VALUES ('bigtext', '99999999999999999999');
INSERT INTO users VALUES ('kim', 25);
INSERT INTO users (id, name) VALUES (1, 'x');
INSERT INTO users VALUES (DEFAULT, 'a', 1, 2);
INSERT INTO users (name, age) VALUES ('a');
INSERT INTO users (name, nope) VALUES ('a', 1);
INSERT INTO users (name, name) VALUES ('a', 'b');
INSERT INTO "Users" (tag) VALUES (NULL);
SELECT * FROM users WHERE name = 1;
SELECT * FROM users
  WHERE 5 <= name;
SELECT * FROM users WHERE age = 'x';
SELECT nope FROM users;
SELECT * FROM nope;
SELECT * FROM other.users;
SELECT * FROM users LIMIT -1;
INSERT INTO users (name) VALUES ('ok'), (1), ('ok3') RETURNING name;
