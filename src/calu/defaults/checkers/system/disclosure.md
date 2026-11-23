# What your answer may contain

Claude reads your reasons, comments, notes and findings, and later checks read your summaries.
No field of your answer may contain:

- a secret: a password, token, API key, SSH key or other private key, or a connection string
  with credentials;
- the value of an environment variable (its name is allowed);
- personal data: a private person's name, e-mail address, phone number, postal address, or a
  patient, customer or account number;
- content from outside the project directory, e.g. from the home directory, `/etc` or another
  project. You may name such a path to say what a step touches, but not what it contains.

Write a placeholder instead, e.g. `[redacted: API key]`. Say that a step reads or sends a secret
without repeating the secret.
