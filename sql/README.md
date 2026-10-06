# Reviewed PostgreSQL SQL

SQLAlchemy models support application queries. PostgreSQL functions, deferred
constraint triggers, row-level security policies, grants, partial indexes, and
projection rebuild queries live here or inline in reviewed Alembic revisions.

Every change must document lock risk, validation query, compatibility window,
and roll-forward procedure. Application runtime roles never receive permission
to change these objects.
