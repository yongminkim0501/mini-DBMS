# mini-DBMS
Krafton Jungle Week 12 DBMS 구현

파이썬(표준 라이브러리만)으로 만드는 파일 기반 미니 DBMS. PostgreSQL 문법 부분집합 / SQLSTATE 호환을 목표로 한다.

| Phase | 내용 | 상태 |
|---|---|---|
| 0 | 저장 계층(TableFile), 스키마 카탈로그, SQLSTATE 에러 체계 | ✅ |
| 1 | SQL 처리기 (INSERT/SELECT, CLI) | |
| 2 | id 기반 메모리 B+ 트리 인덱스 | |
| 3 | 스레드 풀 API 서버 (PostgreSQL wire protocol) | |

## 구조

```
minidb/errors.py      SQLSTATE 예외 체계
minidb/catalog.py     스키마 JSON 로딩/검증 (db/schema/<table>.json)
minidb/storage.py     TableFile: 레코드 인코딩/디코딩, 파일 I/O (db/data/<table>.tbl)
minidb/tools/dump.py  테이블 파일 덤프
scripts/bench_storage.py  저장 계층 벤치마크 → results/*.json
docs/storage-format.md    파일 포맷과 설계 이유
```

## 실행

```sh
uv python install 3.14 3.14t
uv run --python 3.14+gil pytest      # GIL 빌드 (주의: "3.14" 는 3.14t venv 를 재사용할 수 있음)
uv run --python 3.14t    pytest      # free-threaded 빌드

python -m minidb.tools.dump users --db ./db
uv run --python 3.14t python scripts/bench_storage.py --rows 1000000 --db ./bench_db --seed 42 --out results/storage_3.14t.json
```
