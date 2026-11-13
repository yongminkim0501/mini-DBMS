# minidb 실행 계층: 바인더 · 플래너 · 실행기 · 엔진 · CLI

SQL 텍스트가 결과로 바뀌기까지의 과정과, 각 단계가 따르는 PostgreSQL 규칙을 정리한다.

## 1. 전체 파이프라인

```
 SQL 파일/문자열
     │  iter_script_statements (CLI, 청크 스트리밍) / iter_statements (문자열)
     ▼
 StatementSource ── 문장 원문 + 원문 기준 위치 + 토큰          [minidb/sql/lexer.py, stream.py]
     │  parse_statement            문법만 검사 (스키마를 모른다)
     ▼
 AST (InsertStmt / SelectStmt / ExplainStmt)                     [minidb/sql/parser.py]
     │  bind(stmt, catalog)        이름 해석 · 타입 검사 · 값 변환
     ▼
 BoundInsert / BoundSelect / BoundExplain                       [minidb/binder.py]
     │  plan_select / plan_insert  접근 경로 결정 (choose_access_path)
     ▼
 계획 트리 (Limit → Project → Filter → SeqScan, Insert → ValuesScan)  [minidb/planner.py]
     │  이터레이터 실행 (행을 하나씩 pull)
     ▼
 TableFile.scan() / append_many()                               [minidb/storage.py]
     │
     ▼
 QueryResult(columns, rows, command_tag)                        [minidb/result.py]
     │  format_table / format_error
     ▼
 psql 형식 출력 (stdout) / 오류 (stderr)                        [minidb/formatter.py, __main__.py]
```

`Database`(minidb/engine.py)는 이 과정을 감싸는 단일 진입점이다. CLI가 이것을 쓰고, Phase 3 서버도 이것을 쓴다.

### 파서와 바인더를 나눈 이유

- **파서는 스키마를 모른다.** "문법적으로 맞는가"만 본다. 카탈로그 없이 SQL 을 검사·정규화·`to_sql()` 할 수 있고,
  카탈로그가 바뀌어도 파서는 그대로다.
- **바인더는 "의미가 맞는가"를 본다.** 테이블·컬럼 존재, 타입 호환, INSERT 대상 컬럼 수, id 규칙 같은 것들이다.
  모두 카탈로그가 있어야 판단할 수 있다.
- 바인딩이 끝난 문장에는 실행에 필요한 것만 남는다: 컬럼 **인덱스**, 이미 **변환된** 파이썬 값. 그래서 실행기는 행마다
  이름을 찾거나 타입을 다시 확인하지 않는다. 핫 루프가 `row[i] op value` 비교뿐이다.
- PostgreSQL 도 같은 구조다(raw parser → parse analysis → planner → executor). 오류 종류도 단계와 맞아떨어진다.
  42601(구문)은 파서가, 42P01/42703/42883/22P02(의미)는 바인더가 낸다.

## 2. 바인더 규칙

### 테이블

| 입력 | 결과 |
|---|---|
| `users`, `public.users` | 카탈로그의 `users` (스키마는 `public` 하나뿐) |
| `other.users` | 3F000 `schema "other" does not exist` |
| 없는 테이블 | 42P01 `relation "x" does not exist` (원문에 `public.x` 라고 썼으면 `"public.x"`) |
| `"Users"` vs `users` | 다른 테이블 (렉서가 비인용 식별자만 접고, 카탈로그는 정확히 비교) |

### INSERT

| 상황 | PostgreSQL / minidb 동작 |
|---|---|
| 컬럼 목록 있음 | 그 순서대로 대상. 없는 컬럼 42703 `column "x" of relation "t" does not exist`, 중복 42701 |
| 컬럼 목록 없음 | **id 를 포함한 모든 컬럼**이 순서대로 대상. `VALUES ('kim', 25)` 는 'kim' 이 id 에 대응해 실패하고, `VALUES (DEFAULT, 'kim', 25)` 가 올바른 형태 |
| 값 > 대상 컬럼 | 42601 `INSERT has more expressions than target columns` (캐럿은 첫 초과 값) |
| 컬럼 목록 있음, 값 < 대상 | 42601 `INSERT has more target columns than expressions` (캐럿은 값이 없는 첫 컬럼) |
| 컬럼 목록 없음, 값 < 대상 | 나머지는 기본값 (id 자동 부여, 나머지 NULL) |
| id 에 `DEFAULT` | 허용 |
| id 에 다른 값 (NULL 포함) | 428C9 `cannot insert a non-DEFAULT value into column "id"` + DETAIL `Column "id" is an identity column defined as GENERATED ALWAYS.` |
| id 외 컬럼의 `DEFAULT`, 생략한 컬럼 | NULL |
| NULL → NOT NULL 컬럼 | 23502 `null value in column "x" of relation "t" violates not-null constraint` |
| RETURNING 컬럼 | 존재해야 함 (42703) |

**대입 변환 (assignment cast)**

| 컬럼 ← 리터럴 | 결과 | 오류 |
|---|---|---|
| BIGINT ← 정수 | 그대로 | 범위 밖 22003 `bigint out of range` |
| BIGINT ← 문자열 | `int8in` 규칙으로 해석: 앞뒤 공백(isspace) 허용, 부호, ASCII 10진 숫자 | 22P02 `invalid input syntax for type bigint: "abc"`, 범위 밖 22003 `value "…" is out of range for type bigint` |
| TEXT ← 문자열 | 그대로 | |
| TEXT ← 정수 | 10진 문자열로 (`25` → `'25'`) | |
| 아무 컬럼 ← NULL | NULL | NOT NULL 컬럼이면 23502 |

바인딩은 **모든 행에 대해 끝난 뒤에만** 실행으로 넘어간다. 1000번째 행이 실패하면 1~999번째 행도 쓰지 않는다.
저장 계층에서만 알 수 있는 검사(TEXT 최대 바이트 등)는 `append_many` 의 "전부 검증 후 한 번에 쓰기"로 원자성이 지켜진다.

### SELECT

| 상황 | 동작 |
|---|---|
| 선택 컬럼 / WHERE 컬럼이 없음 | 42703 `column "x" does not exist` |
| 같은 컬럼을 여러 번 선택 | 허용 |
| BIGINT 컬럼 vs 정수 | 그대로 비교 (bigint 범위 밖 정수도 비교 가능: PostgreSQL 의 bigint vs numeric) |
| BIGINT 컬럼 vs 문자열 | BIGINT 로 변환 (`age = '25'`), 실패 시 22P02 |
| TEXT 컬럼 vs 문자열 | 그대로 |
| TEXT 컬럼 vs 정수 | 42883 `operator does not exist: text = integer` + HINT. 캐럿은 연산자. 원문 순서와 연산자 그대로 (`5 <= name` → `integer <= text`). 정수 크기에 따라 `integer`/`bigint`/`numeric` |
| 아무 컬럼 vs NULL | 허용, 결과는 항상 UNKNOWN |

TEXT 비교는 **유니코드 코드포인트 순서**다. 파이썬 `str` 비교와 같고, PostgreSQL 의 `"C"` collation 과 같다.
그래서 대문자 < 소문자 (`'Z' < 'a'`) 이고, 한글은 ASCII 보다 크다. 로캘 기반 정렬(`ko_KR`)은 지원하지 않는다.

## 3. NULL 의미론 (3치 논리)

| 식 | 결과 |
|---|---|
| `NULL = 1`, `age = NULL` (age 가 무엇이든) | UNKNOWN |
| `TRUE AND UNKNOWN` | UNKNOWN |
| `FALSE AND UNKNOWN` | FALSE |
| WHERE | **TRUE 인 행만** 통과 (FALSE·UNKNOWN 모두 제외) |

- 컬럼 값이 NULL 인 행은 그 컬럼에 대한 어떤 비교도 통과하지 못한다(`<>` 포함).
- 리터럴이 NULL 인 조건(`age = NULL`)은 모든 행에서 UNKNOWN 이다. `Filter` 는 이를 알고 스캔을 시작하지 않고 바로
  끝난다(PostgreSQL 은 계획 단계에서 상수 false 로 접는다). NULL 인지 검사하려면 `IS NULL` 이 필요하지만 아직 지원하지 않는다.

## 4. 계획 노드와 이터레이터 모델

```
Limit(n) ──pull──▶ Project(cols) ──pull──▶ Filter(conds) ──pull──▶ SeqScan(table)   ──▶ TableFile.scan()
                                                              └──▶ IndexScan(table) ──▶ 점: B+ 트리 search → read_at / 범위: 시작 offset → TableFile.scan_from()
```

각 노드는 `__iter__` 가 제너레이터인 객체다. 상위 노드가 `next()` 로 행을 하나 **당기면**, 그 요청이 아래로 내려가
SeqScan 이 파일에서 레코드 하나를 디코딩해 올려 보낸다. 행은 필요할 때 하나씩만 만들어진다(Volcano 모델).

| 노드 | 하는 일 |
|---|---|
| `SeqScan` | `TableFile.scan()` 순서대로 행을 내보낸다. `rows_scanned` 로 읽은 행 수를 센다 |
| `IndexScan` | id 인덱스(`<table>_pkey`) 사용. 점 검색은 `search` 한 번 + `read_at` 한 번. 범위는 하한 이상인 첫 키의 offset 만 트리에서 찾고 `scan_from` 으로 파일을 순차로 읽다가 상한에서 멈춘다(클러스터드 범위 스캔, EXPLAIN 의 `Heap Fetch: sequential`). `rows_fetched` 로 읽은 행 수를 센다 (Phase 2, docs/bptree.md) |
| `Filter` | AND 로 연결된 비교를 모두 TRUE 로 만족하는 행만 통과 |
| `Project` | 출력 컬럼만 골라 낸다 (전체를 순서대로 고르면 그대로 통과) |
| `Limit` | n 개를 내보내면 **하위를 더 당기지 않고** 끝난다 |
| `ValuesScan` → `Insert` | 바인딩된 행들을 `TableHandle.append_many` 한 번으로 쓰고(파일 → 인덱스 순), RETURNING 이 있으면 부여된 id 를 포함한 행을 내보낸다 |

**LIMIT 이 스캔을 멈추는 원리**: `Limit` 은 n 번째 행을 `yield` 한 다음 `return` 한다. 하위 제너레이터에 다음 행을
요청하지 않으므로 SeqScan 의 `for` 루프는 다음 레코드를 읽지 않은 채 멈춰 있다. `Limit` 은 끝날 때 하위 이터레이터를
`close()` 해서 스캔용 파일 핸들도 바로 닫는다. 100만 건에서 `LIMIT 10` 은 레코드 10개만 읽는다(테스트는 `rows_scanned == 10`
으로 검증). 조건이 있으면 조건을 통과한 10번째 행을 찾을 때까지만 읽는다.

**SELECT 결과 스트리밍**: `QueryResult.rows` 는 계획 트리의 이터레이터 그 자체다. 엔진은 결과를 리스트로 모으지 않는다.
`SELECT n` 태그는 행을 다 읽어야 알 수 있으므로, `command_tag` 를 읽으면 남은 행을 소비한 뒤 태그를 만든다. wire protocol
에서도 CommandComplete 가 DataRow 뒤에 온다. CLI 의 표 출력은 열 폭을 정해야 하므로 psql 처럼 결과를 모두 받은 뒤 출력한다.

## 5. `choose_access_path` (Phase 2: 인덱스 사용)

SELECT 계획의 모양은 항상 `[Limit] → Project → [Filter] → <접근 경로>` 이다. 접근 경로(테이블에서 행을 꺼내는 방법)는
`planner.choose_access_path(bound, table_handle, session)` **한 곳에서만** 정한다. 비용 모델은 없고 규칙 기반이다.

1. `enable_indexscan = off` (세션 설정, CLI `--no-index`) → `SeqScan`.
2. WHERE 에 NULL 과의 비교가 있으면 → `SeqScan` (조건 전체가 항상 거짓이라 Filter 가 스캔을 시작하지도 않는다).
3. id 조건(`=`, `<`, `<=`, `>`, `>=`, 모두 AND)을 `merge_id_conditions` 로 하나의 범위로 합친다.
   - `id = c` 는 하한 c(포함) + 상한 c(포함). 하한은 가장 큰 값, 상한은 가장 작은 값. 같은 값이면 미포함이 이긴다.
   - 하한 > 상한, 또는 같은 값인데 한쪽이 미포함 → **모순(empty)**.
   - BIGINT 범위 밖 상수: `id > 2^70` 은 empty, `id < 2^70` 은 "상한 없음" (`id < -2^70` 은 empty, `id > -2^70` 은 "하한 없음").
4. 결과에 따라
   - id 조건 없음 / `<>` 뿐 / 양쪽 다 무한 (`id < 2^70` 만) → `SeqScan` (인덱스로 전체를 훑는 것은 순차 스캔보다 느리다)
   - 점 (`lo == hi`, 둘 다 포함) → 점 검색 `IndexScan`
   - empty → `IndexScan(empty=True)`: 실행 시 트리도 파일도 건드리지 않는다 (PostgreSQL 도 모순 조건의 btree 스캔을 실행 시 바로 끝낸다)
   - 그 외 → 범위 `IndexScan`

**Filter 는 그대로 남아 모든 조건(id 조건 포함)을 다시 검사한다.** 그래서 접근 경로가 무엇이든 결과는 같고
(인덱스 켬/끔 차등 테스트로 검증), 플래너의 범위 계산이 조금 넓게 잡혀도 결과는 틀리지 않는다. 다만 범위를
**좁게** 잘못 잡으면 행이 빠지므로, 차등 테스트가 바로 그 종류의 버그(경계 포함 여부, off-by-one, 범위 밖 상수)를 잡는다.

**Seq Scan 의 조기 종료를 하지 않는 이유**: 파일 안의 id 는 append 순서대로 증가하므로, `id = 42` 를 찾는 Seq Scan 도
42 를 지나면 멈출 수 있다. 그래도 평균적으로 파일의 절반을 읽어야 하므로 여전히 O(n) 이고(id 가 클수록 느리다),
"id 가 정렬되어 있다"는 저장 계층의 성질에 기대게 되어 Seq Scan 의 의미(모든 행을 본다)가 흐려진다. 인덱스와 비교할
기준값이 "순수 전체 스캔"이어야 차이를 해석하기 쉽다는 점도 있어 Phase 1 과 동일한 순수 전체 스캔으로 두었다.

## 6. EXPLAIN 형식

PostgreSQL 텍스트 형식을 단순화했다. 비용(cost) 추정은 없다.

```
EXPLAIN SELECT name FROM users WHERE age > 30 AND name = 'kim' LIMIT 3;

                      QUERY PLAN
-------------------------------------------------------
 Limit
   ->  Seq Scan on users
         Filter: ((age > 30) AND (name = 'kim'::text))
(3 rows)
```

- `Project` 는 표시하지 않는다(PostgreSQL 은 스캔 노드의 target list 로 처리). `Filter` 는 바로 아래 스캔 노드의
  `Filter:` 속성으로 표시한다.
- 상수 표시: 정수 리터럴은 그대로, 문자열 리터럴은 변환된 타입을 붙인다(`'25'::bigint`, `'kim'::text`), NULL 은
  `NULL::bigint`. 조건은 정규화된 형태로 보인다(`5 < age` → `(age > 5)`).
- INSERT: `Insert on users` 아래에 한 행이면 `Result`, 여러 행이면 `Values Scan on "*VALUES*"`.
- 인덱스 (PostgreSQL 과 같은 모양): 인덱스에 쓴 조건은 `Index Cond:`, 나머지만 `Filter:`.
  `Index Cond:` 에는 병합 전의 원래 조건을 원문 순서대로 보여준다. 범위 Index Scan 에는 `Heap Fetch: sequential` 줄이 붙는다
  (시작 위치부터 파일을 순차로 읽는다, docs/bptree.md §2.1). PostgreSQL 에는 없는 줄이다.

```
 Index Scan using users_pkey on users
   Index Cond: ((id >= 1000) AND (id < 2000))
   Heap Fetch: sequential
   Filter: (age > 30)
```

**EXPLAIN ANALYZE** 는 실제로 실행한다. **INSERT 도 실제로 삽입된다**(PostgreSQL 과 동일). 결과 행은 클라이언트에 반환하지 않는다.

```
 Limit  (actual time=0.031..0.048 rows=1 loops=1)
   ->  Seq Scan on users  (actual time=0.030..0.047 rows=1 loops=1)
         Filter: (age > 30)
         Rows Removed by Filter: 1
 Execution Time: 0.061 ms
```

- `actual time=시작..끝` (ms): 첫 행을 내보낼 때까지 / 마지막 행까지 걸린 시간. 하위 노드 시간을 포함한다(PostgreSQL 과 같음).
- 스캔 노드의 rows 는 필터를 통과한 행 수이고, `Rows Removed by Filter` 는 읽었지만 버린 행 수다.
- 계측은 ANALYZE 일 때만 노드를 감싸므로, 일반 실행 경로에는 시간 측정 코드가 없다.

## 7. 결과와 명령 태그

| 문장 | columns | command_tag (PostgreSQL CommandComplete) |
|---|---|---|
| `INSERT …` | None | `INSERT 0 <행수>` |
| `INSERT … RETURNING` | RETURNING 컬럼 | `INSERT 0 <행수>` |
| `SELECT …` | 선택 컬럼 | `SELECT <행수>` |
| `EXPLAIN …` | `QUERY PLAN` (text) | `EXPLAIN` |
| `SET …` | None | `SET` |
| `SHOW name` | `name` (text), 한 행 | `SHOW` |

`ColumnInfo(name, type_name, type_oid)`: bigint 는 OID 20, text 는 OID 25. Phase 3 의 RowDescription 에 그대로 쓴다.

## 8. CLI

```
python -m minidb run <file.sql | -> --db <dir> [--on-error stop|continue] [--echo] [--timing] [--fsync] [--no-index]
python -m minidb -c "<SQL>" --db <dir> [같은 옵션]
python -m minidb dump <table> --db <dir>
```

- 입력은 `iter_script_statements` 로 **청크 스트리밍**한다(기본 1 MiB). 세미콜론으로 끝난 문장만 확정하고 버퍼에서 버리므로,
  100만 행 스크립트에서도 메모리는 "청크 + 문장 하나" 정도로 일정하다. 위치와 줄 번호는 파일 전체 기준을 유지한다.
- 오류는 stderr 에 `ERROR:` / `LINE n:` + 캐럿 / `DETAIL:` / `HINT:` (libpq 순서). LINE 은 파일 전체 기준 줄 번호다.
- 출력은 psql aligned 형식. 행이 있는 결과는 표 + 빈 줄, 행이 없는 명령은 태그, RETURNING 은 표 다음에 태그.
  SELECT/EXPLAIN 의 태그는 psql 처럼 출력하지 않는다.
- `--timing` 은 오류가 난 문장도 시간을 출력한다(psql 과 같음).
- 종료 코드: 모두 성공 0, 문장 오류가 하나라도 있으면 1, 사용법/파일/카탈로그 오류 2.
- 실행 하나가 세션 하나다. `--no-index` 는 시작 시 `SET enable_indexscan = off` 와 같고, 스크립트 안의 `SET` 으로 바꿀 수 있다.

## 9. Phase 3 락 지점

이번 단계의 `Database`·`TableHandle`·`TableFile`·`BPlusTree` 는 스레드 안전하지 않다. 락이 필요한 지점은 코드에 `# [LOCK]` 주석으로 표시했다.

| 지점 | 위치 | 락 | 이유 |
|---|---|---|---|
| 테이블 핸들 캐시 조회·생성 | `Database.table_handle` | 엔진 락 (짧게) | 같은 테이블에 핸들이 두 개 생기면 각자 next_id·인덱스를 들고 append 해 id 가 겹친다 |
| 카탈로그 reload | `Database.reload_catalog` | 엔진 배타 락 | 반쯤 갱신된 사전을 읽으면 안 된다. 실행 중인 문장이 없을 때만 |
| INSERT 실행 | `Insert._rows` → `TableHandle.append_many` | 테이블 배타(write) 락 | id 할당 → 쓰기 → `_end`/`_next_id` 갱신 → **인덱스 insert** 가 한 임계 구역 (파일엔 있고 인덱스엔 없는 순간을 남이 보면 안 된다) |
| IndexScan | `IndexScan._rows` → `BPlusTree.search/range` + `read_at` | 테이블 공유(read) 락, **range 순회가 끝날 때까지** | 순회 중 분할이 일어나면 리프 연결이 바뀐다 |
| 인덱스 무효화·재구성 | `TableHandle.invalidate_index` / `_rebuild` | 테이블 배타 락 | 파일 핸들과 인덱스를 함께 교체 |
| 세션 | `Session` | 없음 | 연결마다 하나, 스레드 사이에 공유하지 않는다 |
| SELECT 실행 시작 | `Database._select` → `TableFile.scan` 생성 | 테이블 공유(read) 락 | `_end` 스냅샷만 읽는다. 순회는 전용 fd 라 락 밖에서 진행 가능 (storage 불변식 (b)) |
| `Database.flush` / `close` | 엔진 | 테이블 배타 락 | 쓰기 핸들 사용 |

카탈로그는 `Database` 생성 시 **즉시** 로드한다. 지연 로드는 여러 스레드의 "첫 조회"가 동시에 적재를 시작하는 경쟁 조건을 만든다.
바인더·플래너·계획 노드는 문장마다 새로 만들고 공유 상태가 없다. `RecordCodec`·`TableSchema`·AST 는 불변이다.

## 10. 종단 벤치마크 (scripts/bench_e2e.py, 100만 행, M-series 10코어, 단일 스레드)

`data/users_1m.sql` (5,000행 INSERT × 200문장, 21.9 MiB)

| 항목 | 3.14.6 (GIL) | 3.14.6t |
|---|---|---|
| `Database.execute` 전체 적재 | 14.19 s (70,482 행/s) | 12.78 s (78,252 행/s) |
| 단계별: 렉싱·문장 분리 | 8.94 s (56.7%) | 7.78 s (57.0%) |
| 단계별: 파싱 | 4.14 s (26.2%) | 3.65 s (26.7%) |
| 단계별: 바인딩 | 1.04 s (6.6%) | 0.74 s (5.4%) |
| 단계별: 실행·저장 (append_many) | 1.66 s (10.5%) | 1.48 s (10.8%) |
| `WHERE id = N` 평균 (전체 스캔) | 1876 ms | 1880 ms |
| `WHERE name = '…'` 평균 (전체 스캔) | 1870 ms | 1886 ms |
| `LIMIT 10` 평균 | 0.116 ms | 0.110 ms |

- SQL 텍스트 처리(렉싱+파싱)가 적재 시간의 약 83%다. 저장 계층(append_many)은 약 10% 에 불과하다.
- id 조건 SELECT 도 인덱스가 없어 매번 100만 행을 디코딩하므로 약 1.9초다. **Phase 2 B+ 트리의 비교 기준값**이다.
- `LIMIT 10` 은 0.1 ms 다. 이터레이터 모델에서 10행을 읽은 뒤 스캔이 멈추기 때문이다.
- 단계별 측정의 합(15.8 s / 13.6 s)이 `execute` 총 시간보다 큰 이유는 두 가지다. 단계마다 시계를 읽는 비용이 있고,
  파일 스트리밍 경로에서는 청크 경계에 걸친 문장을 한 번 더 렉싱한다.
