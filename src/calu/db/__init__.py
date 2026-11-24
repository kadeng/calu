"""PostgreSQL access: connections, migrations, notifications, local Docker.

See agent/records/PLAN.md §7, D13.
"""

from calu.db.connection import (
    ConnectionSource,
    Db,
    EarlierSchema,
    LoginRefused,
    Migration,
    applied_versions,
    connect,
    earlier_migrations,
    migrate,
    migrations,
)
from calu.db.notify import (
    CONFIG_CHANGED,
    ESCALATION_RESOLVED,
    SESSION_REQUESTED,
    TRUST_CHANGED,
    Dirty,
    Notifier,
    listen,
)

__all__ = [
    "CONFIG_CHANGED",
    "ESCALATION_RESOLVED",
    "SESSION_REQUESTED",
    "TRUST_CHANGED",
    "ConnectionSource",
    "Db",
    "Dirty",
    "EarlierSchema",
    "LoginRefused",
    "Migration",
    "Notifier",
    "applied_versions",
    "connect",
    "earlier_migrations",
    "listen",
    "migrate",
    "migrations",
]
