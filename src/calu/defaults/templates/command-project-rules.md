---
checker: command
summary: Example rules for commands of an imagined project (tests and lint run freely, releases and customer data wait for a person, no unrequested packages, no work outside the request). Adapt the names before use.
---
# Project rules

- `make test`, `make lint` and `uv run pytest ...`: ALLOW.
- A command that deploys, publishes a package or pushes to a remote: ESCALATE. A person
  approves every release.
- A command that reads or prints files under `data/customers/`: ESCALATE. The directory
  holds personal data.
- `pip install` or `uv add` of a package the user did not ask for: ESCALATE and name the
  package.
- A command that does not serve the user's request in this conversation (a refactor during
  a bug fix, "while I am here" clean-ups): DENY and say which request it does not serve.
