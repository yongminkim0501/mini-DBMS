# minidb SQL 문법

minidb 는 PostgreSQL 문법의 **부분집합**을 지원한다. 오류는 PostgreSQL 의 SQLSTATE 와 메시지 형식을 따른다.
이 문서는 렉서(`minidb/sql/lexer.py`), 파서(`minidb/sql/parser.py`), 에러 출력(`minidb/sql/errorfmt.py`)이
구현하는 규칙을 정리한다. 파서는 스키마를 모른다. 테이블·컬럼 존재와 타입 검사는 다음 단계의 바인더가 맡는다.

## 1. 문법 (BNF)

```
script         := statement { ';' statement } [ ';' ]          -- 빈 문장(;;)은 무시
statement      := insert | select

insert         := INSERT INTO qualified_name [ '(' ident { ',' ident } ')' ]
                  VALUES row { ',' row }
                  [ RETURNING ( '*' | ident { ',' ident } ) ]
row            := '(' value { ',' value } ')'
value          := literal | DEFAULT

select         := SELECT ( '*' | ident { ',' ident } ) FROM qualified_name
                  [ WHERE cond { AND cond } ]
                  [ LIMIT ( integer | ALL ) ]
cond           := ident op literal | literal op ident
op             := '=' | '<>' | '!=' | '<' | '<=' | '>' | '>='

literal        := [ '-' ] integer | string | NULL
qualified_name := ident [ '.' ident ]
```

`{ x }` 는 0회 이상 반복, `[ x ]` 는 생략 가능이다.

### 파서의 구조 (재귀 하강)

문법 규칙 하나가 `_Parser` 메서드 하나에 대응한다(`statement → _statement()`, `insert → _insert()`,
`row → _row()`, `cond → _cond()` …). 반복 `{ ',' row }` 는 "다음 토큰이 `,` 인 동안 `_row()` 호출" 하는
while 루프가 되고, 선택 `[ RETURNING … ]` 은 "다음 토큰이 RETURNING 이면" 하는 if 가 된다.
이 문법은 **LL(1)** 이다. 다음 토큰 하나만 보면 어느 분기인지 항상 정해지므로 되돌아가기(backtracking)가 없다.
`cond` 의 두 형태도 첫 토큰이 식별자인지 리터럴인지로 바로 구분된다.

### 정규화

| 입력 | AST | 이유 |
|---|---|---|
| `a != 1` | `Condition('a', '<>', 1)` | 연산자를 한 가지 철자로 |
| `42 = id` | `Condition('id', '=', 42)` | 컬럼을 항상 왼쪽에 |
| `5 < age` | `Condition('age', '>', 5)` | 뒤집을 때 연산자 방향도 반전 (`<`↔`>`, `<=`↔`>=`) |
| `- 5` (MINUS, INTEGER) | `Literal(-5)` | 음수 리터럴로 합침. `-9223372036854775808` 도 표현 가능 |
| `LIMIT ALL` | `limit=None` | LIMIT 없음과 동일 |
| `Users`, `"Users"` | `users`, `Users` | 비인용은 소문자, 인용은 그대로 |

조건 정규화 덕분에 Phase 2 의 실행기는 B+ 트리를 쓸 수 있는 `id = 상수` 조건을 찾을 때
`cond.column == "id" and cond.op == "="` 한 가지 모양만 보면 된다.

`to_sql()` 은 AST 를 위 정규형의 SQL 로 되돌린다(식별자는 필요할 때만 큰따옴표, 문자열은 `''` 이스케이프).
모든 정상 테스트 케이스에 대해 `parse(to_sql(ast)) == ast` 가 성립한다(position 필드는 비교 제외).

## 2. 렉싱 규칙

| 항목 | 규칙 |
|---|---|
| 공백 | 스페이스, 탭, `\r`, `\n` |
| 줄 주석 | `--` 부터 줄 끝까지 |
| 블록 주석 | `/* … */`, PostgreSQL 처럼 **중첩 지원**: `/* a /* b */ c */` 전체가 주석. 닫히지 않으면 `unterminated /* comment` |
| 예약 키워드 | `SELECT FROM WHERE INSERT INTO VALUES RETURNING LIMIT ALL AND NULL DEFAULT` (대소문자 무시). 비인용으로 식별자 자리에 오면 구문 오류 |
| 비인용 식별자 | `[A-Za-z_][A-Za-z0-9_$]*`, 소문자로 변환 |
| 인용 식별자 | `"…"`, 대소문자 유지, `""` → `"`. 빈 식별자는 `zero-length delimited identifier`, 닫히지 않으면 `unterminated quoted identifier` |
| 식별자 길이 | UTF-8 **63바이트** 초과면 `NameTooLong` (42622) |
| 문자열 | `'…'`, `''` → `'`, 줄바꿈 포함 가능. 닫히지 않으면 `unterminated quoted string` |
| 정수 | `[0-9]+` → 파이썬 `int` (범위 검사는 바인더) |
| 기호 | `( ) , ; * . = <> != < <= > >= -` |

**구현**: 모든 규칙을 이름 있는 그룹의 alternation 으로 묶은 컴파일된 정규식 하나로, 현재 위치에서 `match` 한 뒤
`m.lastgroup` 으로 분기한다. 정규식 엔진(C)이 토큰 종류를 한 번에 판별한다. 대안의 순서가 의미를 가진다.
`--`/`/*` 는 `-` 보다, `E'` 는 식별자보다, 소수는 정수보다 앞에 있다. 마지막 대안 `(?P<other>.)` 가 어떤
문자든 매치하므로 매치 실패가 없다. 정규식은 중첩(짝 맞추기)을 표현할 수 없으므로, 블록 주석만 `/*`·`*/` 를
정규식으로 찾고 깊이는 직접 센다.

모든 토큰은 `offset`(0부터), `end`, `line`·`column`(1부터, 문자 단위)을 가진다. 오류의 `position` 은
원문 기준 1부터 시작하는 **문자** 위치다(PostgreSQL ErrorResponse 의 P 필드).

## 3. 문장 분리 (`split_statements`)

`text.split(';')` 가 아니라 **세미콜론 토큰**에서 자른다. 문자열 `'a;b'`, 인용 식별자 `"x;y"`, 주석 안의 `;` 는
토큰이 아니므로 자르지 않는다. 공백·주석만 있는 문장은 결과에서 뺀다.

```python
StatementSource(text, start_offset, error=None, tokens=None)
```

- `text`: 첫 토큰부터 마지막 토큰까지의 원문(앞뒤 공백·주석과 `;` 제외). `start_offset` 은 원문에서의 시작 위치.
- `tokens`: 분리하면서 만든 토큰. `parse_statement` 가 다시 렉싱하지 않도록 재사용한다.
- **렉싱 오류 처리**: 닫히지 않은 문자열 같은 렉싱 오류가 나면 그 뒤의 원문은 해석할 수 없다. 따옴표가 안 닫혔다면
  뒤의 `;` 가 문자열 안인지 밖인지 알 수 없기 때문이다. 그래서 오류 **이전의 완전한 문장들은 그대로 반환**하고,
  **마지막 원소**로 `StatementSource(text=<오류 문장 시작 ~ 원문 끝>, start_offset=…, error=<예외>)` 를 붙인 뒤
  멈춘다. 오류 문장의 시작은 그 문장의 첫 토큰이다. 토큰이 하나도 없으면(예: 문장 맨 앞의 닫히지 않은 주석)
  오류 위치가 시작이다. `error` 가 있는 원소를 `parse_statement` 에 넘기면 그 예외를 그대로 던진다.
- 구문 오류는 분리 단계에서 검사하지 않는다. 그래서 CLI 의 `--on-error continue` 는 문장마다 `parse_statement` 를
  호출해, 한 문장의 구문 오류가 나머지 실행을 막지 않게 할 수 있다.
- `iter_statements(text)` 는 같은 일을 하는 스트리밍 버전이다. `parse_script` 가 이것을 쓴다. 파싱이 끝난 문장의
  토큰을 바로 해제하므로 큰 스크립트에서 GC 부담이 준다(§6).

## 4. 지원 / 미지원 기능

| 기능 | 상태 | 오류 |
|---|---|---|
| `INSERT … VALUES` (다중 행, 컬럼 목록, `DEFAULT`, `RETURNING */목록`) | ✅ | |
| `SELECT */목록 FROM [schema.]t [WHERE … AND …] [LIMIT n/ALL]` | ✅ | |
| 비교 `= <> != < <= > >=`, 리터럴이 왼쪽인 조건 | ✅ | |
| `UPDATE`, `DELETE`, `CREATE`, `DROP`, `ALTER`, `BEGIN`, `COMMIT`, `ROLLBACK`, `WITH`, `EXPLAIN` … | ❌ | 0A000 `UPDATE statements are not supported` |
| `WHERE … OR …` | ❌ | 0A000 `OR in WHERE clause is not supported` |
| `WHERE NOT …` | ❌ | 0A000 `NOT in WHERE clause is not supported` |
| `IS [NOT] NULL`, `IN`, `LIKE`, `BETWEEN` | ❌ | 0A000 |
| `WHERE (…)` 괄호 그룹 | ❌ | 0A000 `parenthesized conditions in WHERE clause are not supported` |
| 컬럼끼리 비교 `a = b` | ❌ | 0A000 `comparison between two columns is not supported` |
| `ORDER BY`, `GROUP BY`, `HAVING`, `OFFSET`, `UNION` … | ❌ | 0A000 `ORDER BY is not supported` 등 |
| `JOIN`, `FROM a, b` | ❌ | 0A000 |
| 함수 호출 `count(*)`, 수식, `t.col` | ❌ | 0A000 |
| `INSERT … SELECT`, `DEFAULT VALUES` | ❌ | 0A000 |
| 소수·지수 `1.5`, `1e3` | ❌ | 0A000 `numeric literals are not supported` |
| 파라미터 `$1`, `E'…'`/`B'…'`/`X'…'`/`U&'…'`, `$$…$$` | ❌ | 0A000 |
| `LIMIT -1` | ❌ | 2201W `LIMIT must not be negative` |

`or`, `not`, `is`, `order`, `join`, `offset` 같은 단어는 **해당 위치에서만** 미지원 기능으로 인식한다(예: WHERE
조건 사이의 비인용 `or`). PostgreSQL 에서 이 단어들은 예약어라 컬럼 이름으로 쓰려면 원래 인용해야 하므로
동작이 크게 다르지 않다. 인용한 `"or"` 는 언제나 일반 식별자다. `to_sql()` 은 이런 이름을 항상 인용한다.

## 5. PostgreSQL 과 다르게 동작하는 부분

| 항목 | PostgreSQL | minidb | 이유 |
|---|---|---|---|
| 63바이트 초과 식별자 | 63바이트로 **잘라내고** NOTICE | `NameTooLong` (42622) **오류** | 잘린 이름이 다른 객체와 우연히 같아지는 조용한 오동작을 막는다. NOTICE 를 내는 채널(Phase 3 전)도 아직 없다 |
| 비인용 식별자의 비 ASCII 문자 | `이름` 허용 | 구문 오류 (인용하면 허용) | 명세의 정규식을 따름. 한글 이름은 `"이름"` 으로 쓴다 |
| `123abc` | PG 15+: `trailing junk after numeric literal` | 동일 | PG 15+ 동작을 따름 |
| 인접 문자열 이어 붙이기 `'a'\n'b'` | `'ab'` | 구문 오류 | 부분집합 |
| `SELECT 1` (FROM 없음) | 허용 | 구문 오류 | 문법이 FROM 을 요구 |
| `LIMIT NULL` | LIMIT 없음 | 구문 오류 | 문법이 정수/ALL 만 허용 |
| `INSERT` 컬럼 수 ≠ 값 수 | 파싱 단계에서 42601 | 바인더가 검사 | 컬럼 목록이 없는 경우는 스키마가 있어야 알 수 있으므로 한곳(바인더)에서 일관되게 검사 |
| 스크립트 중간 문장의 "end of input" | `;` 토큰을 가리킴 | 문장 마지막 토큰 바로 뒤(대개 `;` 위치) | 문장을 먼저 분리하므로 파서는 `;` 를 보지 않는다 |

## 6. 성능 (100만 행 INSERT 스크립트, M-series 단일 스레드)

| 항목 | 3.14 (GIL) | 3.14t |
|---|---|---|
| 10,000행 INSERT 한 문장 렉싱 (60,009 토큰) | 72 ms | 67 ms |
| 같은 문장 파싱 (토큰 재사용) | 36 ms | 35 ms |
| 100만 행 스크립트 `split_statements` (렉싱 포함) | 9.7 s | 7.9 s |
| 100만 행 스크립트 전체 파싱 | 4.9 s | 3.6 s |
| **합계** | **14.6 s** | **11.5 s** |
| 스트리밍 (`iter_statements` → 파싱 → 버림) | 11.7 s | 10.1 s |
| SELECT 단문 1개 (렉싱 포함) | 22.8 µs | 23.3 µs |

- **렉서가 병목**이다(토큰당 ~1.2 µs, 파서는 ~0.6 µs). 파이썬 수준에서 토큰마다 정규식 매치 1회, 분기,
  NamedTuple 생성이 일어난다.
- 문장 하나만 잴 때(72 ms × 100 = 7.2 s)보다 스크립트 전체가 느린 이유는 **GC** 다. 600만 개의 토큰과
  100만 개의 AST 노드가 동시에 살아 있으면 주기적 GC 가 그 객체들을 반복해서 훑는다. GC 를 끄고 재면 약 30%
  빨라진다. 3.14t 의 GC 구현은 일반 빌드와 달라서 이 차이가 작고, 그래서 스크립트 전체에서는 3.14t 가 더 빠르다.
  라이브러리가 전역 GC 설정을 바꾸는 것은 스레드 안전하지 않으므로 하지 않는다. 대신 `iter_statements` 로
  문장 단위 스트리밍을 하면 살아 있는 객체가 줄어든다.
- 저장 계층 적재(100만 행 약 1.45 s)와 비교하면 SQL 텍스트 처리가 **약 8–10배** 느리다. 대량 적재 경로는
  나중에 파싱을 거치지 않는 별도 경로(예: COPY 류)가 필요할 수 있다.

## 7. 에러 메시지 예시 (`format_error`)

```
ERROR:  syntax error at or near "FORM"
LINE 1: SELECT * FORM users;
                 ^
```

```
ERROR:  OR in WHERE clause is not supported
LINE 3: WHERE age > 20 OR age < 10;
                       ^
```

전각 문자는 폭 2로 계산하고, 긴 줄은 캐럿 주변 60칸만 남긴다(libpq 와 같은 값):

```
ERROR:  VALUES lists must all be the same length
LINE 1: ...NSERT INTO users (name, age) VALUES ('홍길동', 30), ('kim');
                                                               ^
```

```
ERROR:  LIMIT must not be negative
LINE 1: SELECT * FROM users LIMIT -1;
                                  ^
```

```
ERROR:  unterminated quoted string
LINE 1: SELECT * FROM users WHERE name = 'abc
                                         ^
```

```
ERROR:  numeric literals are not supported
LINE 1: SELECT * FROM users WHERE age = 1.5;
                                        ^
```

- position 이 없는 오류는 `ERROR:  메시지` 한 줄만 출력한다.
- 여러 줄 SQL 이면 position 이 속한 줄의 번호와 그 줄만 보여준다. `\r\n` 줄바꿈도 처리한다.
- 탭은 공백 하나로 바꿔 터미널 탭 폭과 관계없이 캐럿을 맞춘다.

| SQLSTATE | 클래스 | 상황 |
|---|---|---|
| 42601 | `SqlSyntaxError` | 구문 오류, 렉싱 오류(닫히지 않은 따옴표·주석 등) |
| 0A000 | `FeatureNotSupported` | 미지원 기능 |
| 42622 | `NameTooLong` | 식별자 63바이트 초과 |
| 2201W | `InvalidRowCountInLimitClause` | LIMIT 음수 |
