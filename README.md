# mini-DBMS

[![test](https://github.com/yongminkim0501/mini-DBMS/actions/workflows/test.yml/badge.svg)](https://github.com/yongminkim0501/mini-DBMS/actions/workflows/test.yml)

Krafton Jungle Week 12 DBMS 구현

파이썬(표준 라이브러리만)으로 만드는 파일 기반 미니 DBMS. PostgreSQL 문법 부분집합 / SQLSTATE 호환을 목표로 한다.

| Phase | 내용 | 상태 |
|---|---|---|
| 0 | 저장 계층(TableFile, 포맷 v2: 레코드 CRC + quarantine 복구), 스키마 카탈로그, SQLSTATE 에러 체계 | ✅ |
| 1 | SQL 처리기: 렉서·파서·AST, 바인더·플래너·실행기·CLI (docs/sql-grammar.md, docs/execution.md) | ✅ |
| 2 | id 기반 메모리 B+ 트리 인덱스 | |
| 3 | 스레드 풀 API 서버 (PostgreSQL wire protocol) | |

## 구조

```
minidb/errors.py      SQLSTATE 예외 체계
minidb/catalog.py     스키마 JSON 로딩/검증 (db/schema/*.json, 관례상 <oid>.json)
minidb/storage.py     TableFile: 레코드 인코딩/디코딩, 파일 I/O (db/data/<oid>.tbl)
minidb/identifiers.py 비인용 식별자 규칙 (ASCII 만 소문자로 접기)
minidb/tools/dump.py  테이블 파일 덤프
minidb/sql/           렉서, 문장 분리(스트리밍 포함), AST, 재귀 하강 파서, psql 스타일 에러 출력
minidb/binder.py      AST + 카탈로그 → 바인딩된 문장 (이름 해석, 타입 변환)
minidb/planner.py     실행 계획 (choose_access_path: Phase 2 IndexScan 확장 지점)
minidb/executor.py    계획 노드 (이터레이터 모델), EXPLAIN
minidb/engine.py      Database: 외부가 쓰는 단일 진입점
minidb/result.py      QueryResult, ColumnInfo
minidb/formatter.py   psql aligned 출력
minidb/__main__.py    CLI (python -m minidb)
scripts/bench_storage.py  저장 계층 벤치마크 → results/*.json
docs/storage-format.md    파일 포맷과 설계 이유
docs/sql-grammar.md       지원 SQL 문법(BNF), 렉싱 규칙, PostgreSQL 과의 차이
docs/execution.md         파이프라인, 바인더 타입 규칙, NULL 의미론, 이터레이터 모델, EXPLAIN, 락 지점
scripts/gen_insert_sql.py 대량 INSERT 스크립트 생성, scripts/bench_e2e.py 종단 벤치마크
```

## 개발 환경

CI 와 **같은 파이썬 패치 버전**을 쓴다. 패치 버전이 다르면 GC·free-threading 동작과 벤치마크 수치가 달라질 수 있다.

| 항목 | 버전 |
|---|---|
| CPython (GIL 빌드) | `3.14.6+gil` |
| CPython (free-threaded 빌드) | `3.14.6t` |
| uv | `0.12.0` (uv 버전마다 내려받을 수 있는 파이썬 목록이 다르다) |

```sh
# uv 가 관리하는 동일 버전 설치 (시스템/Homebrew 파이썬이 아니라 uv 관리 빌드를 쓴다)
uv python install 3.14.6 3.14.6t

# 테스트: 두 빌드에서 각각
UV_PYTHON_PREFERENCE=only-managed uv run --python 3.14.6+gil pytest
UV_PYTHON_PREFERENCE=only-managed uv run --python 3.14.6t    pytest
```

- `uv python install` 을 먼저 해야 한다. uv 0.12.0 은 `--python 3.14.6+gil` 요청에 대해 자동 다운로드를 하지 않는다
  (`No interpreter found for Python 3.14.6+gil in managed installations`). CI 도 같은 이유로 설치 단계를 따로 둔다.
- `+gil` 을 꼭 붙인다. `--python 3.14.6` 처럼 빌드를 지정하지 않으면, 프로젝트 `.venv` 가 이미 free-threaded 로
  만들어져 있을 때 그 venv 를 그대로 재사용한다.
- pytest 헤더의 `minidb: python 3.14.6, free-threaded build=..., GIL enabled=...` 로 어떤 빌드에서 돌았는지 확인한다.
- 버전을 올릴 때는 `.github/workflows/test.yml` 의 `PYTHON_PATCH`·매트릭스·uv 버전과 이 표를 함께 바꾼다.

## CLI

```sh
# 스크립트 실행 (- 는 표준 입력). 오류가 나도 계속하려면 --on-error continue
uv run --python 3.14.6t python -m minidb run script.sql --db ./db --on-error continue --timing

# SQL 문자열 실행
uv run --python 3.14.6t python -m minidb -c "SELECT * FROM users WHERE age > 20 LIMIT 5" --db ./db

# 테이블 파일 덤프
uv run --python 3.14.6t python -m minidb dump users --db ./db
```

출력은 psql 과 같은 형식이다:

```
$ python -m minidb -c "INSERT INTO users (name, age) VALUES ('kim', 25), ('홍길동', 31) RETURNING *" --db ./db
 id |  name  | age 
----+--------+-----
  1 | kim    |  25
  2 | 홍길동 |  31
(2 rows)

INSERT 0 2

$ python -m minidb -c "SELECT * FROM users WHERE name = 1" --db ./db
ERROR:  operator does not exist: text = integer
LINE 1: SELECT * FROM users WHERE name = 1
                                       ^
HINT:  No operator matches the given name and argument types. You might need to add explicit type casts.

$ python -m minidb -c "EXPLAIN SELECT name FROM users WHERE age > 30 LIMIT 3" --db ./db
         QUERY PLAN          
-----------------------------
 Limit
   ->  Seq Scan on users
         Filter: (age > 30)
(3 rows)
```

컬럼 목록 없는 INSERT 는 PostgreSQL 처럼 id 를 포함한 모든 컬럼이 대상이다: `INSERT INTO users VALUES (DEFAULT, 'kim', 25)`.
종료 코드는 성공 0, 문장 오류 1, 사용법/파일 오류 2.

## 벤치마크

```sh
uv run --python 3.14.6t python scripts/gen_insert_sql.py --rows 1000000 --batch 5000 --seed 42 --out data/users_1m.sql
uv run --python 3.14.6t python scripts/bench_e2e.py --out results/e2e_3.14.6t.json
uv run --python 3.14.6t python scripts/bench_storage.py --rows 1000000 --db ./bench_db --seed 42 --out results/storage_3.14t.json
uv run --python 3.14.6t python scripts/bench_parser.py --out results/parser_3.14t.json
```
