"""Links into the project's own git browser (`calu.web.git`)."""

import pytest

pytest.importorskip("starlette", reason="the web interface needs the `web` extra")

from calu.web.git import commit_url, project_url

SPELLINGS = [
    ("git@github.com:kadeng/calu.git", "github.com", "kadeng/calu"),
    ("git@github.com:kadeng/calu", "github.com", "kadeng/calu"),
    ("ssh://git@github.com/kadeng/calu.git", "github.com", "kadeng/calu"),
    ("https://github.com/kadeng/calu.git", "github.com", "kadeng/calu"),
    ("https://github.com/kadeng/calu", "github.com", "kadeng/calu"),
    # Credentials in the URL are dropped rather than linked to.
    ("https://someone:token@github.com/kadeng/calu.git", "github.com", "kadeng/calu"),
    # A self-hosted instance with a path of its own.
    ("git@git.example.com:team/group/thing.git", "git.example.com", "team/group/thing"),
]


@pytest.mark.parametrize(("remote", "host", "path"), SPELLINGS)
def test_a_remote_in_any_usual_spelling_becomes_a_project_link(
    remote: str, host: str, path: str
) -> None:
    """Every spelling of a remote leads to the same web address.

    Args:
        remote: The remote as git writes it.
        host: The host it should link to.
        path: The path it should link to.
    """
    assert project_url(remote) == f"https://{host}/{path}"


def test_the_commit_path_follows_the_host() -> None:
    """GitHub, GitLab and Bitbucket spell "one commit" differently."""
    assert commit_url("git@github.com:kadeng/calu.git", "9f2c1b") == (
        "https://github.com/kadeng/calu/commit/9f2c1b"
    )
    assert commit_url("git@gitlab.com:kadeng/calu.git", "9f2c1b") == (
        "https://gitlab.com/kadeng/calu/-/commit/9f2c1b"
    )
    assert commit_url("https://bitbucket.org/kadeng/calu.git", "9f2c1b") == (
        "https://bitbucket.org/kadeng/calu/commits/9f2c1b"
    )
    # An unknown host is guessed as "commit", which is what Gitea and cgit use.
    assert commit_url("git@git.example.com:team/thing.git", "9f2c1b") == (
        "https://git.example.com/team/thing/commit/9f2c1b"
    )


def test_a_template_wins_over_the_guess_and_never_raises() -> None:
    """`web.git_url_template` is for everything the guess does not cover."""
    template = "http://localhost:8080/calu/commit/{commit}"
    assert commit_url("git@github.com:kadeng/calu.git", "9f2c1b", template) == (
        "http://localhost:8080/calu/commit/9f2c1b"
    )
    # The remote is available to the template as well.
    assert commit_url("git@example.com:x/y.git", "abc", "{remote}#{commit}") == (
        "git@example.com:x/y.git#abc"
    )
    # A template with a placeholder calu does not fill must not break the page.
    assert commit_url("git@github.com:kadeng/calu.git", "9f2c1b", "{nonsense}/{commit}") == ""
    assert commit_url("git@github.com:kadeng/calu.git", "9f2c1b", "{}/{commit}") == ""


def test_nothing_to_link_to_is_an_empty_string_not_a_broken_link() -> None:
    """Every page asks for these links, so they have to answer for a project without a remote."""
    assert commit_url("", "9f2c1b") == ""  # no remote and no template
    assert commit_url("git@github.com:kadeng/calu.git", "") == ""  # no commit
    assert commit_url("", "") == ""
    # A local path or a remote calu cannot read is not a web address.
    for remote in ("/srv/git/calu.git", "../calu", "file:///srv/git/calu.git", "  "):
        assert project_url(remote) == ""
        assert commit_url(remote, "9f2c1b") == ""
