# mini-DBMS

[![test](https://github.com/yongminkim0501/mini-DBMS/actions/workflows/test.yml/badge.svg)](https://github.com/yongminkim0501/mini-DBMS/actions/workflows/test.yml)

Krafton Jungle Week 12 DBMS 구현

파이썬(표준 라이브러리만)으로 만드는 파일 기반 미니 DBMS. PostgreSQL 문법 부분집합 / SQLSTATE 호환을 목표로 한다.

| Phase | 내용 | 상태 |
|---|---|---|
| 0 | 저장 계층(TableFile, 포맷 v2: 레코드 CRC + quarantine 복구), 스키마 카탈로그, SQLSTATE 에러 체계 | ✅ |
| 1 | SQL 처리기: 렉서·파서·AST (docs/sql-grammar.md) ✅ / 바인더·실행기·CLI | 진행 중 |
| 2 | id 기반 메모리 B+ 트리 인덱스 | |
| 3 | 스레드 풀 API 서버 (PostgreSQL wire protocol) | |

## 구조

```
minidb/errors.py      SQLSTATE 예외 체계
minidb/catalog.py     스키마 JSON 로딩/검증 (db/schema/<table>.json)
minidb/storage.py     TableFile: 레코드 인코딩/디코딩, 파일 I/O (db/data/<table>.tbl)
minidb/tools/dump.py  테이블 파일 덤프
minidb/sql/           렉서, 문장 분리, AST, 재귀 하강 파서, psql 스타일 에러 출력
scripts/bench_storage.py  저장 계층 벤치마크 → results/*.json
docs/storage-format.md    파일 포맷과 설계 이유
docs/sql-grammar.md       지원 SQL 문법(BNF), 렉싱 규칙, PostgreSQL 과의 차이
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

- `+gil` 을 꼭 붙인다. `--python 3.14.6` 처럼 빌드를 지정하지 않으면, 프로젝트 `.venv` 가 이미 free-threaded 로
  만들어져 있을 때 그 venv 를 그대로 재사용한다.
- pytest 헤더의 `minidb: python 3.14.6, free-threaded build=..., GIL enabled=...` 로 어떤 빌드에서 돌았는지 확인한다.
- 버전을 올릴 때는 `.github/workflows/test.yml` 의 `PYTHON_PATCH`·매트릭스·uv 버전과 이 표를 함께 바꾼다.

## 실행

```sh
uv run --python 3.14.6t python -m minidb.tools.dump users --db ./db
uv run --python 3.14.6t python scripts/bench_storage.py --rows 1000000 --db ./bench_db --seed 42 --out results/storage_3.14t.json
uv run --python 3.14.6t python scripts/bench_parser.py --out results/parser_3.14t.json
```
