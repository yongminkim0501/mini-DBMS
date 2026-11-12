-- args: --on-error continue
-- "Users" 와 users 는 다른 테이블 (데이터 파일도 따로)
INSERT INTO users (name) VALUES ('lower');
INSERT INTO "Users" (tag) VALUES ('UPPER');
INSERT INTO USERS (name) VALUES ('folded-to-lower');
SELECT * FROM users;
SELECT * FROM "Users";
SELECT * FROM Users;
SELECT * FROM "USERS";
SELECT tag FROM users;
