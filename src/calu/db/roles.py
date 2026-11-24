"""calu's database roles (migrations 0001, 0015, 0033, 0043; agent/records/PLAN.md D265).

    role            logs in with                         may
    owner           the `db-admin` entry                 migrate, create roles, everything
    calu_admin      the `user` entry: the calu user      read and write every table; every
                    password, or a generated one when    calu process of the person who set
                    that password is empty               calu up logs in as it
    calu_operator   no login (a group)                   `calu_caller_is_operator()` asks for
                                                         its membership; calu_admin has it
    calu_session    no login since migration 0043        its grants and policies stay, unused

The roles are server-wide; their names are fixed.
"""

SESSION = "calu_session"
"""The role sessions logged in as before migration 0043."""

OPERATOR = "calu_operator"
"""The group whose members may act on every session; `calu_admin` is one."""

ADMIN = "calu_admin"
"""The role of the user login."""
