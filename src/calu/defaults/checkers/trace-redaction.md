# What the trace keeps and what it may lose

Code reviewers read the trace to understand why the code is as it is. Many people consider
their chat with a coding agent private. The trace therefore keeps what the work was, and it
may lose what tells about the person.

## Keep, faithfully

- Every requirement and decision that entered the session: stated by the user, or proposed
  by Claude and accepted by the user. Never reword, weaken or drop one. A request in the form
  of a question ("Can you make the timeout 30 s?") is a requirement: keep it.
- What was changed and why: the tool calls, the checks, their summaries and their reasons.
- Everything that the two sections below do not name, unchanged.

## May be removed

You may remove the questions that the user asked, and information that indicates when this
session happened and how long it took:

- A question of the user that asks for an explanation or an opinion and sets no requirement.
  If the user's message holds nothing else, remove the whole block. If it also holds
  requirements or decisions, remove the questions and keep the rest.
- Dates, times of day, weekdays and durations that say when the session took place or how
  long it lasted ("it is late here", "after three hours"), and counts of attempts ("third
  try"). In code, command output and file contents, replace such a time stamp by
  `[redacted: time]` instead of removing it. Dates that are data of the project (a release
  date, a date in a test) stay.
- Remarks about the user's mood, health, family or private life.

Remove whole sentences or list items; do not rewrite the sentences that stay.

## Replace by a placeholder

Replace each occurrence by a placeholder of the form `[redacted: <type>]`:

| In the text | Placeholder |
|---|---|
| API keys, access keys, client secrets | `[redacted: api key]` |
| Passwords and passphrases | `[redacted: password]` |
| Access tokens, session tokens, cookies, the signature of a signed URL | `[redacted: token]` |
| Private keys | `[redacted: private key]` |
| The password or token inside a connection string or URL (keep the rest of the URL) | `[redacted: password]` |
| Names of private persons | `[redacted: name]` |
| E-mail addresses of persons | `[redacted: e-mail address]` |
| Phone numbers | `[redacted: phone number]` |
| Postal addresses | `[redacted: postal address]` |
| Bank account numbers, IBANs, payment card numbers | `[redacted: account number]` |
| Government identifiers (passport, tax, social security numbers) | `[redacted: identifier]` |
| Dates of birth, health data and similar personal details of a person | `[redacted: personal data]` |

A value that only stands for a secret is not a secret: keep variable names (`API_KEY`,
`os.environ["TOKEN"]`), obvious dummies (`xxx`, `<your key>`, `changeme`, addresses at
`example.com`) and existing `[redacted: ...]` placeholders as they are.

Not personal data, keep them: names of companies, products, libraries, files, functions and
variables; names of public figures in a public role; the role names "user", "Claude",
"subagent", "checker"; version numbers, commit ids, UUIDs of sessions, port numbers, IP
addresses of private networks and of `localhost`.

When you are not sure whether something is a secret or identifies a private person, replace
it.
