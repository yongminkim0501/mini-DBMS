import io
import json

import pytest
from pgclient import PgClient

from minidb.server import Server, ServerConfig

USERS = {
    "oid": 16384,
    "table": "users",
    "columns": [
        {"name": "id", "type": "BIGINT", "auto_increment": True},
        {"name": "name", "type": "TEXT", "nullable": True},
        {"name": "age", "type": "BIGINT", "nullable": True},
    ],
}


def make_db(path):
    (path / "schema").mkdir(parents=True)
    (path / "schema" / "16384.json").write_text(json.dumps(USERS), encoding="utf-8")
    return path


@pytest.fixture
def db_dir(tmp_path):
    return make_db(tmp_path / "db")


@pytest.fixture
def server_factory(db_dir):
    servers = []

    def make(**kw):
        cfg = ServerConfig(port=None, pg_port=0, workers=kw.pop("workers", 4), **kw)
        s = Server(db_dir, cfg, log_stream=io.StringIO()).start()
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.shutdown(timeout=10)


@pytest.fixture
def server(server_factory):
    return server_factory()


@pytest.fixture
def client_factory(server):
    clients = []

    def make(startup=True, **kw):
        c = PgClient(server.pg_address)
        clients.append(c)
        if startup:
            c.startup(**kw)
        return c

    yield make
    for c in clients:
        c.close()


@pytest.fixture
def pg(client_factory):
    """시작이 끝난 연결 하나 (users 테이블 비어 있음)."""
    return client_factory()
