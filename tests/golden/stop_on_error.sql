-- 기본값(--on-error stop): 첫 오류에서 멈춘다
INSERT INTO users (name) VALUES ('before');
SELECT * FROM users WHERE age = 'oops';
INSERT INTO users (name) VALUES ('never runs');
