"""Handing a file to an outside editor: the command, and the round trip."""

from __future__ import annotations

import io
import os

import pytest

from meridian_commander import extedit


# -- reading the setting -------------------------------------------------------

@pytest.mark.parametrize("setting", ["", "   ", None])
def test_a_blank_setting_means_the_built_in_editor(setting):
    assert extedit.editor_argv(setting) is None


def test_a_command_is_split_the_way_a_shell_would():
    assert extedit.editor_argv("vim") == ["vim"]
    assert extedit.editor_argv("  emacs -nw  ") == ["emacs", "-nw"]
    assert extedit.editor_argv("'my editor' -f") == ["my editor", "-f"]


def test_the_environment_is_expanded_before_the_command_is_split(monkeypatch):
    """An EDITOR of "code --wait" is a command and an argument.

    Expanding after the split would make it one impossible filename, which is
    the whole reason the order matters.
    """
    monkeypatch.setenv("EDITOR", "code --wait")
    assert extedit.editor_argv("$EDITOR") == ["code", "--wait"]


def test_a_leading_tilde_is_expanded(monkeypatch):
    monkeypatch.setenv("HOME", "/home/someone")
    assert extedit.editor_argv("~/bin/ed") == ["/home/someone/bin/ed"]


def test_an_unset_variable_is_reported_rather_than_run(monkeypatch):
    monkeypatch.delenv("EDITOR", raising=False)
    with pytest.raises(ValueError, match=r"does not set \$EDITOR"):
        extedit.editor_argv("$EDITOR")
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ValueError, match=r"does not set \$\{NOPE\}"):
        extedit.editor_argv("${NOPE}")


def test_a_variable_expanding_to_nothing_is_reported(monkeypatch):
    monkeypatch.setenv("EDITOR", "   ")
    with pytest.raises(ValueError, match="expands to nothing"):
        extedit.editor_argv("$EDITOR")


def test_a_command_of_empty_quotes_is_reported(monkeypatch):
    """It splits to one empty word, which is not a program anyone can run."""
    monkeypatch.setenv("EDITOR", "\'\'")
    with pytest.raises(ValueError, match="expands to nothing"):
        extedit.editor_argv("$EDITOR")


def test_unbalanced_quotes_are_reported():
    with pytest.raises(ValueError, match="not a usable command"):
        extedit.editor_argv("vim 'unclosed")


# -- the local case ------------------------------------------------------------

class _Local:
    """Enough of LocalFileSystem for the in-place path: it has a real file."""

    def local_path(self, path):
        return path

    def basename(self, path):
        return os.path.basename(path)


def test_a_local_file_is_edited_where_it_lies(tmp_path):
    """No copy, no upload: the editor opens the real file, in its directory.

    Which is the point of using it -- ``:w`` writes the file the user is
    looking at, keeping its permissions and its editor's undo history.
    """
    calls = []

    def run(cmd, cwd):
        calls.append((cmd, cwd))
        return 0

    path = str(tmp_path / "notes.txt")
    assert extedit.edit(_Local(), path, ["vim"], run) == "Edited notes.txt"
    assert calls == [(["vim", path], str(tmp_path))]


def test_an_editor_that_will_not_start_is_reported():
    result = extedit.edit(_Local(), "/tmp/x.txt", ["nosucheditor"],
                          lambda cmd, cwd: None)
    assert result == "Could not start nosucheditor"


def test_a_non_zero_exit_warns_that_the_file_may_be_unsaved():
    result = extedit.edit(_Local(), "/tmp/x.txt", ["vim"],
                          lambda cmd, cwd: 1)
    assert "exited 1" in result
    assert "x.txt may be unsaved" in result


def test_a_bare_filename_has_no_directory_to_run_in():
    """``cwd=None`` rather than ``""``, which subprocess would reject."""
    seen = []
    extedit.edit(_Local(), "notes", ["vim"],
                 lambda cmd, cwd: seen.append(cwd) or 0)
    assert seen == [None]
    extedit.edit(_Local(), "/notes", ["vim"],
                 lambda cmd, cwd: seen.append(cwd) or 0)
    assert seen[1] == "/"


# -- viewing with an outside pager ---------------------------------------------

def test_a_local_file_is_paged_where_it_lies(tmp_path):
    """No copy: the pager opens the real file, in its own directory."""
    calls = []

    def run(cmd, cwd):
        calls.append((cmd, cwd))
        return 0

    path = str(tmp_path / "notes.txt")
    assert extedit.view(_Local(), path, ["less"], run) == "Viewed notes.txt"
    assert calls == [(["less", path], str(tmp_path))]


def test_a_pager_that_will_not_start_is_reported():
    result = extedit.view(_Local(), "/tmp/x.txt", ["nosuchpager"],
                          lambda cmd, cwd: None)
    assert result == "Could not start nosuchpager"


def test_a_pager_exiting_non_zero_says_so_without_alarm(tmp_path):
    """Nothing was being written, so there is nothing to warn about losing."""
    result = extedit.view(_Local(), "/tmp/x.txt", ["less"],
                          lambda cmd, cwd: 2)
    assert result == "less exited 2"


# -- the remote case -----------------------------------------------------------

class _Remote:
    """A filesystem with no local path: bytes behind a connection."""

    def __init__(self, data=b"hello\n", exists=True, write_error=None):
        self.data = data
        self._exists = exists
        self.write_error = write_error
        self.written = None

    def basename(self, path):
        return path.rsplit("/", 1)[-1]

    def exists(self, path):
        return self._exists

    def open_read(self, path):
        return io.BytesIO(self.data)

    def open_write(self, path):
        if self.write_error is not None:
            raise self.write_error
        fs = self

        class _Writer:
            def write(self, data):
                fs.written = data

            def close(self):
                pass

        return _Writer()


def _editing(text):
    """A run() that replaces the working copy's contents, as an editor would."""
    def run(cmd, cwd):
        with open(cmd[-1], "wb") as handle:
            handle.write(text)
        return 0
    return run


def test_a_remote_file_is_fetched_edited_and_written_back():
    fs = _Remote()
    result = extedit.edit(fs, "/srv/notes.txt", ["vim"], _editing(b"changed\n"))
    assert result == "Saved notes.txt (8 bytes)"
    assert fs.written == b"changed\n"


def test_the_working_copy_keeps_the_name_so_the_editor_knows_the_syntax():
    seen = []

    def run(cmd, cwd):
        seen.append(cmd[-1])
        return 0

    extedit.edit(_Remote(), "/srv/notes.py", ["vim"], run)
    assert os.path.basename(seen[0]) == "notes.py"


def test_the_working_copy_lives_in_a_directory_of_its_own(tmp_path):
    """Private (0700) and thrown away after, so a fetched file is not left
    readable by everyone else on this machine."""
    seen = []

    def run(cmd, cwd):
        seen.append((cmd[-1], cwd, os.stat(cwd).st_mode & 0o777))
        return 0

    extedit.edit(_Remote(), "/srv/notes.txt", ["vim"], run)
    copy, workdir, mode = seen[0]
    assert os.path.dirname(copy) == workdir
    assert mode == 0o700
    assert not os.path.exists(workdir)


def test_an_unchanged_file_is_not_written_back():
    """Quitting vim with :q should cost no upload."""
    fs = _Remote()
    result = extedit.edit(fs, "/srv/notes.txt", ["vim"], lambda cmd, cwd: 0)
    assert result == "notes.txt unchanged"
    assert fs.written is None


def test_an_editor_that_removed_the_copy_writes_nothing_back():
    fs = _Remote()

    def run(cmd, cwd):
        os.unlink(cmd[-1])
        return 0

    assert extedit.edit(fs, "/srv/notes.txt", ["vim"], run) \
        == "notes.txt unchanged"
    assert fs.written is None


def test_a_remote_file_that_does_not_exist_yet_starts_empty():
    fs = _Remote(exists=False)
    seen = []

    def run(cmd, cwd):
        with open(cmd[-1], "rb") as handle:
            seen.append(handle.read())
        with open(cmd[-1], "wb") as handle:
            handle.write(b"new\n")
        return 0

    assert extedit.edit(fs, "/srv/new.txt", ["vim"], run) \
        == "Saved new.txt (4 bytes)"
    assert seen == [b""]
    assert fs.written == b"new\n"


def test_an_editor_that_will_not_start_leaves_the_remote_file_alone():
    fs = _Remote()
    assert extedit.edit(fs, "/srv/notes.txt", ["vim"], lambda cmd, cwd: None) \
        == "Could not start vim"
    assert fs.written is None


def test_a_failed_write_back_keeps_the_edit_and_says_where():
    """The one case where the working copy is deliberately not deleted.

    A dropped connection or a read-only archive would otherwise throw away
    everything the user typed.
    """
    fs = _Remote(write_error=OSError("connection lost"))
    result = extedit.edit(fs, "/srv/notes.txt", ["vim"], _editing(b"changed\n"))
    assert "Could not save notes.txt: connection lost" in result
    kept = result.rsplit("your edit is in ", 1)[1]
    assert os.path.exists(kept)
    with open(kept, "rb") as handle:
        assert handle.read() == b"changed\n"
    os.unlink(kept)
    os.rmdir(os.path.dirname(kept))


def test_a_file_too_large_to_fetch_is_refused_before_any_editor_runs():
    fs = _Remote(data=b"x" * (extedit.MAX_FETCH_BYTES + 1))
    started = []
    result = extedit.edit(fs, "/srv/huge.log", ["vim"],
                          lambda cmd, cwd: started.append(cmd) or 0)
    assert "huge.log is larger than 8 MB" in result
    assert started == []


def test_a_file_that_cannot_be_read_is_reported():
    class _Unreadable(_Remote):
        def open_read(self, path):
            raise OSError("permission denied")

    result = extedit.edit(_Unreadable(), "/srv/notes.txt", ["vim"],
                          lambda cmd, cwd: 0)
    assert result == "Cannot read notes.txt: permission denied"


def test_a_reader_that_will_not_close_is_not_an_error():
    """The bytes are already in hand; a failing close is not worth the edit."""
    class _StickyReader(io.BytesIO):
        def close(self):
            raise OSError("nope")

    class _Sticky(_Remote):
        def open_read(self, path):
            return _StickyReader(self.data)

    fs = _Sticky()
    assert extedit.edit(fs, "/srv/notes.txt", ["vim"], _editing(b"new\n")) \
        == "Saved notes.txt (4 bytes)"


# -- viewing a remote file -----------------------------------------------------

def test_a_remote_file_is_fetched_to_a_private_copy_and_paged():
    seen = []

    def run(cmd, cwd):
        seen.append((cmd[-1], cwd, os.stat(cwd).st_mode & 0o777,
                     open(cmd[-1], "rb").read()))
        return 0

    assert extedit.view(_Remote(b"log line\n"), "/srv/app.log", ["less"],
                        run) == "Viewed app.log"
    copy, workdir, mode, contents = seen[0]
    assert os.path.basename(copy) == "app.log"     # the name, for the pager
    assert os.path.dirname(copy) == workdir
    assert mode == 0o700
    assert contents == b"log line\n"
    # Nothing to save, so the copy does not outlive the pager.
    assert not os.path.exists(workdir)


def test_a_copy_a_pager_scribbled_on_is_never_sent_back():
    """A pager has nothing to send back, even if the file on disk changed."""
    fs = _Remote(b"before\n")

    def run(cmd, cwd):
        with open(cmd[-1], "wb") as handle:
            handle.write(b"after\n")
        return 0

    assert extedit.view(fs, "/srv/notes.txt", ["less"], run) == "Viewed notes.txt"
    assert fs.written is None


def test_a_remote_file_too_big_to_fetch_is_refused_for_viewing(monkeypatch):
    monkeypatch.setattr(extedit, "MAX_FETCH_BYTES", 4)
    result = extedit.view(_Remote(b"far too long"), "/srv/big.log", ["less"],
                          lambda cmd, cwd: 0)
    assert "too big" in result


def test_a_remote_file_that_cannot_be_read_is_reported_for_viewing():
    class _Broken(_Remote):
        def open_read(self, path):
            raise OSError("connection reset")

    result = extedit.view(_Broken(), "/srv/notes.txt", ["less"],
                          lambda cmd, cwd: 0)
    assert result == "Cannot read notes.txt: connection reset"


def test_a_pager_that_will_not_start_still_clears_the_copy_up():
    seen = []
    result = extedit.view(_Remote(), "/srv/notes.txt", ["nosuchpager"],
                          lambda cmd, cwd: seen.append(cwd) or None)
    assert result == "Could not start nosuchpager"
    assert not os.path.exists(seen[0])


# -- the working copy's name ---------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("notes.txt", "notes.txt"),
    ("a/b.txt", "a_b.txt"),
    ("", "meridian-edit.txt"),
    (".", "meridian-edit.txt"),
    ("..", "meridian-edit.txt"),
    ("  ", "meridian-edit.txt"),
])
def test_a_name_from_another_machine_cannot_escape_its_directory(name,
                                                                 expected):
    assert extedit._copy_name(name) == expected


def test_a_path_ending_in_a_separator_stays_inside_its_directory():
    """``basename`` is empty there, so the name falls back to the whole path.

    Which is exactly the case the flattening is for: the working copy must
    land in the directory made for it whatever the remote name looks like.
    """
    seen = []
    extedit.edit(_Remote(exists=False), "/srv/odd/", ["vim"],
                 lambda cmd, cwd: seen.append((cmd[-1], cwd)) or 0)
    copy, workdir = seen[0]
    assert os.path.dirname(copy) == workdir


def test_a_working_copy_that_cannot_be_removed_is_not_an_error(monkeypatch):
    """Cleanup is best effort: a temporary file is not worth failing over."""
    def refuse(path):
        raise OSError("busy")

    monkeypatch.setattr(os, "unlink", refuse)
    monkeypatch.setattr(os, "rmdir", refuse)
    assert extedit.edit(_Remote(), "/srv/notes.txt", ["vim"],
                        lambda cmd, cwd: 0) == "notes.txt unchanged"
