"""Request-local dashboard session ownership over the existing identity seam."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
from pathlib import Path
import re

from fastapi import HTTPException, Request

from gateway.owner_identity import host_owner_subject, principal_subject, same_owner
from hermes_state_local import POLICY_PREFIX
from hermes_state_mutation_binding import BINDING_PREFIX, binding_names


@dataclass(frozen=True)
class SessionReadScope:
    subject: str
    home: str | None = None


_scope: ContextVar[SessionReadScope | None] = ContextVar("dashboard_session_scope", default=None)


def request_session_subject(request):
    grant = getattr(request.state, "native_http_principal", None)
    if grant is not None:
        return grant["subject"]
    session = getattr(request.state, "session", None)
    if session is not None:
        return principal_subject({
            "user_id": session.user_id, "provider": session.provider, "issuer": session.issuer,
        })
    from hermes_cli.web_server import _has_valid_session_token

    if not getattr(request.app.state, "auth_required", False) and _has_valid_session_token(request):
        return host_owner_subject()
    return None


def scoped_session_reader() -> SessionReadScope | None:
    return _scope.get()


@contextmanager
def use_session_reader(scope):
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


async def session_request_scope(request: Request):
    subject = request_session_subject(request)
    if not subject:
        raise HTTPException(status_code=401, detail="Unauthorized")
    grant = getattr(request.state, "native_http_principal", None)
    read_scope = (SessionReadScope(subject, grant["profile_id"] if grant else None)
                  if subject and subject != host_owner_subject() else None)
    with use_session_reader(read_scope):
        if request.method in {"GET", "HEAD"} or request.url.path.endswith("/pull-requests"):
            yield
            return
        from hermes_cli.backend_retirement import retirement

        with retirement.work() as admitted:
            if not admitted:
                raise HTTPException(status_code=503, detail="runtime_draining")
            yield


def require_session_operator(request: Request) -> None:
    if request_session_subject(request) != host_owner_subject():
        raise HTTPException(status_code=403, detail="permission_denied")


def session_owned(subject, session_id, user_id, binding, receipt):
    """Private creation receipts and import bindings outrank a row's display identity."""
    if receipt is not None:
        try:
            saved = json.loads(receipt)
            return (saved["session_id"] == session_id and same_owner(saved["principal_id"], subject)
                    and (binding is None or binding_names(binding, subject)))
        except (ValueError, TypeError, KeyError):
            return False
    if binding is not None:
        return binding_names(binding, subject)
    return bool(user_id) and same_owner(user_id, subject)


def owns_stored_session(conn, subject, session_id):
    row = conn.execute(
        """SELECT s.id, s.user_id, b.value, p.value FROM sessions s
           LEFT JOIN state_meta b ON b.key = ? || s.id
           LEFT JOIN state_meta p ON p.key = ? || s.id
           WHERE s.id = ?""",
        (BINDING_PREFIX, POLICY_PREFIX, session_id),
    ).fetchone()
    return row is not None and session_owned(subject, *row)


class _ScopedConnection:
    # The stored readers use explicit SQLite indexes on messages. A view cannot
    # accept a named index, but its underlying table remains indexed.
    _message_index_hint = re.compile(r"\s+INDEXED BY idx_messages_\w+")

    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        return self.connection.execute(self._message_index_hint.sub("", sql), parameters)

    def __getattr__(self, name):
        return getattr(self.connection, name)


def bind_dashboard_reads(db):
    """Apply ownership before all row, lineage, FTS and aggregate projections.

    Views are private to this new mode=ro connection. They never attach to a
    gateway writer or a connection shared with another principal.
    """
    scope = scoped_session_reader()
    if scope is None:
        return
    if not db.read_only:
        raise RuntimeError("Dashboard ownership requires a read-only handle")
    if scope.home and Path(db.db_path).parent.resolve() != Path(scope.home).resolve():
        raise HTTPException(status_code=403, detail="profile_scope_mismatch")

    conn = db._conn
    conn.create_function("dashboard_session_owned", 4, lambda *row: int(session_owned(scope.subject, *row)))
    conn.execute(f"""
        CREATE TEMP VIEW dashboard_owned_sessions AS
        SELECT s.* FROM main.sessions AS s
        LEFT JOIN main.state_meta AS b ON b.key = '{BINDING_PREFIX}' || s.id
        LEFT JOIN main.state_meta AS p ON p.key = '{POLICY_PREFIX}' || s.id
        WHERE dashboard_session_owned(s.id, s.user_id, b.value, p.value)
    """)
    # Do not expose a foreign parent's id in the otherwise visible metadata.
    columns = [row[1] for row in conn.execute("PRAGMA main.table_info(sessions)")]
    projection = []
    for column in columns:
        if column == "parent_session_id":
            projection.append("""CASE WHEN EXISTS (
                SELECT 1 FROM dashboard_owned_sessions AS parent WHERE parent.id = s.parent_session_id
            ) THEN s.parent_session_id END AS parent_session_id""")
        else:
            projection.append('s."' + column.replace('"', '""') + '"')
    conn.execute("CREATE TEMP VIEW sessions AS SELECT " + ", ".join(projection) + " FROM dashboard_owned_sessions AS s")
    conn.execute("""
        CREATE TEMP VIEW messages AS SELECT m.* FROM main.messages AS m
        WHERE EXISTS (SELECT 1 FROM dashboard_owned_sessions AS s WHERE s.id = m.session_id)
    """)
    db._conn = _ScopedConnection(conn)
