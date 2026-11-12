-- args: --on-error continue
-- 성공과 실패가 섞인 스크립트: 실패한 문장만 건너뛰고 계속한다
INSERT INTO users (name) VALUES ('first');
SELECT * FORM users;
INSERT INTO users (name) VALUES ('second');
UPDATE users SET name = 'x';
SELECT * FROM users WHERE id = 1 OR id = 2;
INSERT INTO users (name) VALUES ('third'), (NULL), ('a' );
SELECT name FROM users;
-- 다중 행 INSERT 는 원자적: 두 번째 행이 NOT NULL 을 어기면 첫 행도 쓰지 않는다
INSERT INTO "Users" (tag) VALUES ('t1'), (NULL);
SELECT * FROM "Users";
SELECT 'never closed FROM users;
SELECT name FROM users;
