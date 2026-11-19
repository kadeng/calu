"""The web interface of calu: the herd overview and what steers the checkers.

See agent/records/PLAN.md phase 6.

`calu.web.app` holds the Starlette application, `calu.web.git` the links into whatever git
browser a project already has. Starlette, Jinja2 and uvicorn are ordinary dependencies of
calu: one installation holds every command.
"""
