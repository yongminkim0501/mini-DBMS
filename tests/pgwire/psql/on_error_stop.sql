\set ON_ERROR_STOP on
INSERT INTO users (name) VALUES ('before');
SELECT name FROM users;
SELECT * FROM nope;
INSERT INTO users (name) VALUES ('after');
SELECT name FROM users;
