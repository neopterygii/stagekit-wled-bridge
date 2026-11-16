"""The release check: a tag, version.py and CHANGELOG.md must agree.

CI runs `tools/check_release_tag.py` on a `v*` tag before building the image.
These tests cover the check itself, and assert that the tree as committed is
consistent — so a version bump without a changelog entry fails on the branch,
not on the day of the release.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import check_release_tag as rc  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

VERSION_PY = '"""doc"""\n\n__version__ = "1.2.0"\n'
DATED = "# Changelog\n\n## 1.2.0 — 2026-11-16\n\nNotes.\n\n## 1.0.0 — 2026-07-27\n"
DRAFT = "# Changelog\n\n## 1.2.0 — draft\n\nNotes.\n"


def test_matching_dated_release_passes():
    assert rc.check("v1.2.0", VERSION_PY, DATED) == []


def test_tag_ahead_of_source_is_refused():
    problems = rc.check("v1.3.0", VERSION_PY, DATED)
    assert len(problems) == 1
    assert "v1.3.0" in problems[0] and "1.2.0" in problems[0]


def test_forgotten_bump_is_refused():
    """The case the check exists for: tagged 1.2.0, source still says 1.1.0."""
    stale = VERSION_PY.replace("1.2.0", "1.1.0")
    problems = rc.check("v1.2.0", stale, DATED)
    assert any("does not match __version__" in p for p in problems)
    assert any("newest entry is 1.2.0" in p for p in problems)


@pytest.mark.parametrize("tag", ["1.2.0", "v1.2", "v1.2.0-rc1", "release", ""])
def test_malformed_tag_is_refused(tag):
    problems = rc.check(tag, VERSION_PY, DATED)
    assert any("not of the form" in p for p in problems)


def test_draft_changelog_blocks_a_tag_but_not_a_branch():
    assert rc.check(None, VERSION_PY, DRAFT) == []
    problems = rc.check("v1.2.0", VERSION_PY, DRAFT)
    assert len(problems) == 1 and "draft" in problems[0]


def test_undated_entry_blocks_a_tag():
    undated = "## 1.2.0\n\nNotes.\n"
    assert rc.check(None, VERSION_PY, undated) == []
    assert any("no date" in p for p in rc.check("v1.2.0", VERSION_PY, undated))


def test_changelog_behind_source_fails_without_a_tag():
    old = "## 1.1.0 — 2026-08-26\n"
    problems = rc.check(None, VERSION_PY, old)
    assert problems == ["CHANGELOG.md: newest entry is 1.1.0, but __version__ is 1.2.0"]


def test_missing_changelog_entry_and_missing_version():
    problems = rc.check(None, "x = 1\n", "# Changelog\n")
    assert any("__version__" in p for p in problems)
    assert any("no '## X.Y.Z' entry" in p for p in problems)


@pytest.mark.parametrize("heading", [
    "## 1.2.0 — 2026-11-16", "## 1.2.0 – 2026-11-16", "## 1.2.0 - 2026-11-16",
])
def test_dash_styles_in_the_heading(heading):
    assert rc.newest_changelog_entry(heading + "\n") == ("1.2.0", "2026-11-16")


def test_subheadings_are_not_entries():
    text = "# Changelog\n\n### 9.9.9 notes\n\n## 1.2.0 — draft\n"
    assert rc.newest_changelog_entry(text) == ("1.2.0", "draft")


def test_committed_tree_is_consistent():
    """version.py and CHANGELOG.md in this checkout agree with each other."""
    assert rc.check(
        None,
        (ROOT / "version.py").read_text(encoding="utf-8"),
        (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
    ) == []


def test_source_version_matches_the_imported_module():
    import version
    assert rc.source_version(
        (ROOT / "version.py").read_text(encoding="utf-8")) == version.__version__


def test_main_exit_status(capsys):
    assert rc.main([]) == 0
    assert "consistent" in capsys.readouterr().out
    assert rc.main(["v0.0.1"]) == 1
    assert "does not match" in capsys.readouterr().err


def test_ci_runs_the_check_on_tags_before_building():
    """The workflow calls the script in the job the image build depends on."""
    workflow = (ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
    test_job, _, build_job = workflow.partition("  build-and-push:")
    assert "tools/check_release_tag.py" in test_job
    assert "startsWith(github.ref, 'refs/tags/v')" in test_job
    assert "needs: test" in build_job
