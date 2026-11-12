-- 한글 데이터와 식별자 정렬
INSERT INTO 회원 (이름, 나이, 메모) VALUES ('홍길동', 30, '서울'), ('kim', 7, NULL), ('이순신장군', 45, 'mixed 한글 text');
SELECT * FROM 회원;
SELECT 이름, 나이 FROM 회원 WHERE 이름 = '홍길동';
SELECT 메모 FROM 회원 WHERE 나이 > 10;
INSERT INTO 회원 (이름) VALUES ('줄
바꿈') RETURNING *;
