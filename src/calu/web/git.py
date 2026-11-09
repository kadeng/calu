"""Links into a git browser, instead of a git browser of calu's own (PLAN.md phase 6).

Every step records the commit the workspace was on (`steps.git_commit`) and every project may
record its git remote (`projects.git_remote`), which is enough to link a step to the commit it
ran on — in the browser the repository already has. calu does not browse git itself: GitHub,
GitLab, Gitea, cgit and gitweb all do it better, and a session's working tree lives on another
host anyway.

    git@github.com:kadeng/calu.git  + 9f2c1b  ->  https://github.com/kadeng/calu/commit/9f2c1b

`web.git_url_template` overrides the guess for anything else, for example a local gitweb or a
klaus instance:

    http://localhost:8080/calu/commit/{commit}
"""

import re

SSH_REMOTE = re.compile(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?$")
"""`git@host:owner/repo(.git)` and its `ssh://` spelling."""

HTTP_REMOTE = re.compile(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?$")
"""`https://host/owner/repo(.git)`, with any credentials in the URL dropped."""

COMMIT_PATHS = {
    "github.com": "commit",
    "gitlab.com": "-/commit",
    "bitbucket.org": "commits",
}
"""Host -> the path segment that shows one commit; other hosts are guessed as "commit"."""


def commit_url(remote: str, commit: str, template: str = "") -> str:
    """Build the URL that shows one commit of a project.

    Args:
        remote: The project's git remote, in any of the usual spellings ("" if unknown).
        commit: The commit id.
        template: `web.git_url_template`; if given, it is formatted with `commit` and `remote`
            and used instead of guessing from the remote.

    Returns:
        The URL, or "" if there is nothing to link to (no commit, or no remote and no template).
    """
    if not commit:
        return ""
    if template:
        try:
            return template.format(commit=commit, remote=remote)
        except KeyError, IndexError:
            return ""
    host, path = _split(remote)
    if not host:
        return ""
    segment = COMMIT_PATHS.get(host, "commit")
    return f"https://{host}/{path}/{segment}/{commit}"


def project_url(remote: str) -> str:
    """Build the URL of the project itself, for a link to its git browser.

    Args:
        remote: The project's git remote ("" if unknown).

    Returns:
        The URL, or "" if the remote cannot be read as a web address.
    """
    host, path = _split(remote)
    return f"https://{host}/{path}" if host else ""


def _split(remote: str) -> tuple[str, str]:
    """Split a git remote into host and path.

    Args:
        remote: The remote in SSH or HTTP spelling.

    Returns:
        (host, path) without a trailing ".git", or ("", "") if it is neither spelling (a local
        path, for example).
    """
    text = remote.strip()
    for pattern in (SSH_REMOTE, HTTP_REMOTE):
        found = pattern.match(text)
        if found:
            return found.group(1), found.group(2).strip("/")
    return "", ""
