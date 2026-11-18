"""Apply the bundled migrations using an explicit migration connection."""

from alembic import context
from sqlalchemy.engine import Connection

connection = context.config.attributes.get("connection")
if not isinstance(connection, Connection):
    raise RuntimeError("Use stellarweaver.web migrate with the migration DSN")
context.configure(connection=connection)
with context.begin_transaction():
    context.run_migrations()
