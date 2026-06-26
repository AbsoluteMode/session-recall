# tests/test_scope.py
from session_recall.scope import repo_root, scope_clause, project_label


# --- repo_root: normalize a cwd to its parent repository root ---------------

def test_repo_root_plain_path_unchanged():
    assert repo_root("/Users/me/myrepo") == "/Users/me/myrepo"


def test_repo_root_strips_claude_worktree_suffix():
    assert repo_root(
        "/Users/me/myrepo/.claude/worktrees/wt-a1b2c3"
    ) == "/Users/me/myrepo"


def test_repo_root_strips_trailing_slash():
    assert repo_root("/Users/me/myrepo/") == "/Users/me/myrepo"


def test_repo_root_worktree_with_trailing_slash():
    assert repo_root("/Users/me/myrepo/.claude/worktrees/foo-123/") == "/Users/me/myrepo"


def test_repo_root_empty_is_empty():
    assert repo_root("") == ""


# --- scope_clause: boundary-safe SQL predicate over a cwd column ------------

def test_scope_clause_none_matches_everything():
    sql, params = scope_clause("c.cwd", None)
    assert sql == ""
    assert params == []


def test_scope_clause_builds_exact_or_prefix_predicate():
    sql, params = scope_clause("c.cwd", "/Users/me/myrepo")
    # exact root OR anything strictly under it (with the '/%' boundary)
    assert "c.cwd = ?" in sql
    assert "c.cwd LIKE ? ESCAPE '\\'" in sql
    assert params == ["/Users/me/myrepo", "/Users/me/myrepo/%"]


def test_scope_clause_escapes_like_wildcards_in_root():
    # a root containing % or _ must not be interpreted as a LIKE wildcard
    sql, params = scope_clause("c.cwd", "/tmp/a_b%c")
    assert params == ["/tmp/a_b%c", "/tmp/a\\_b\\%c/%"]


# --- project_label: human repo label from a cwd ----------------------------

def test_project_label_basename_of_repo_root():
    assert project_label("/Users/me/myrepo") == "myrepo"


def test_project_label_collapses_worktree_to_repo():
    # the bug this fixes: a worktree cwd must yield the repo name, not a junk hash
    assert project_label("/Users/me/myrepo/.claude/worktrees/wt-a1b2c3") == "myrepo"


def test_project_label_empty():
    assert project_label("") == ""
