-- WHERE 의 모든 비교 연산자
INSERT INTO users (name, age) VALUES ('kim', 25), ('lee', 31), ('park', NULL), ('choi', 31), ('jung', 40);
SELECT id, name FROM users WHERE age = 31;
SELECT id, name FROM users WHERE age <> 31;
SELECT id, name FROM users WHERE age != 31;
SELECT id, name FROM users WHERE age < 31;
SELECT id, name FROM users WHERE age <= 31;
SELECT id, name FROM users WHERE age > 31;
SELECT id, name FROM users WHERE age >= 31;
SELECT id, name FROM users WHERE 31 < age;
SELECT id, name FROM users WHERE age >= 25 AND age < 40 AND name <> 'lee';
SELECT id, name FROM users WHERE name < 'kim';
SELECT id, name FROM users WHERE age = '31';
SELECT id FROM users WHERE age >= 0 LIMIT 2;
SELECT id FROM users WHERE age >= 0 LIMIT ALL;
