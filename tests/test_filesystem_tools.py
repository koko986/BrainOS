from __future__ import annotations

import pytest

from second_brain.computer import filesystem as fs
from second_brain.computer.filesystem import FileToolError


def test_write_then_read_round_trip(tmp_path):
    target = tmp_path / "nested" / "note.txt"

    message = fs.write_file(target, "hello marlin")

    assert "Created" in message
    assert fs.read_file(target).text == "hello marlin"


def test_write_creates_missing_parents(tmp_path):
    target = tmp_path / "a" / "b" / "c" / "deep.txt"

    fs.write_file(target, "deep")

    assert target.exists()


def test_write_reports_overwrite(tmp_path):
    target = tmp_path / "note.txt"
    fs.write_file(target, "first")

    assert "Overwrote" in fs.write_file(target, "second")
    assert fs.read_file(target).text == "second"


def test_append_file(tmp_path):
    target = tmp_path / "log.txt"
    fs.write_file(target, "one\n")

    fs.append_file(target, "two\n")

    assert fs.read_file(target).text == "one\ntwo\n"


def test_edit_file_replaces_exact_text(tmp_path):
    target = tmp_path / "config.ini"
    fs.write_file(target, "mode=slow\nlevel=1\n")

    fs.edit_file(target, "mode=slow", "mode=fast")

    assert "mode=fast" in fs.read_file(target).text


def test_edit_file_refuses_ambiguous_match(tmp_path):
    target = tmp_path / "dup.txt"
    fs.write_file(target, "x\nx\n")

    with pytest.raises(FileToolError, match="appears 2 times"):
        fs.edit_file(target, "x", "y")


def test_edit_file_replace_all(tmp_path):
    target = tmp_path / "dup.txt"
    fs.write_file(target, "x\nx\n")

    fs.edit_file(target, "x", "y", replace_all=True)

    assert fs.read_file(target).text == "y\ny\n"


def test_edit_file_missing_text_is_an_error(tmp_path):
    target = tmp_path / "note.txt"
    fs.write_file(target, "hello")

    with pytest.raises(FileToolError, match="not found"):
        fs.edit_file(target, "goodbye", "hi")


def test_write_file_does_not_translate_newlines(tmp_path):
    target = tmp_path / "lf.txt"

    fs.write_file(target, "one\ntwo\n")

    assert target.read_bytes() == b"one\ntwo\n"


def test_edit_preserves_crlf_line_endings(tmp_path):
    """Text-mode writes would turn each CRLF into CRCRLF on every edit."""

    target = tmp_path / "crlf.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")

    fs.edit_file(target, "alpha", "gamma")

    assert target.read_bytes() == b"gamma\r\nbeta\r\n"


def test_repeated_edits_do_not_accumulate_carriage_returns(tmp_path):
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"count=1\r\n")

    for value in range(2, 6):
        fs.edit_file(target, f"count={value - 1}", f"count={value}")

    assert target.read_bytes() == b"count=5\r\n"


def test_read_file_truncates_and_reports_total(tmp_path):
    target = tmp_path / "big.txt"
    fs.write_file(target, "a" * 5000)

    payload = fs.read_file(target, max_chars=1000)

    assert payload.truncated
    assert len(payload.text) == 1000
    assert payload.total_chars == 5000


def test_read_file_start_line(tmp_path):
    target = tmp_path / "lines.txt"
    fs.write_file(target, "one\ntwo\nthree\n")

    assert fs.read_file(target, start_line=3).text.strip() == "three"


def test_read_file_rejects_binary(tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"PK\x00\x00binary")

    with pytest.raises(FileToolError, match="binary"):
        fs.read_file(target)


def test_delete_file(tmp_path):
    target = tmp_path / "gone.txt"
    fs.write_file(target, "bye")

    fs.delete_path(target)

    assert not target.exists()


def test_delete_non_empty_folder_needs_recursive(tmp_path):
    folder = tmp_path / "stuff"
    fs.write_file(folder / "inner.txt", "data")

    with pytest.raises(FileToolError, match="recursive"):
        fs.delete_path(folder)

    fs.delete_path(folder, recursive=True)
    assert not folder.exists()


def test_move_and_copy(tmp_path):
    source = tmp_path / "a.txt"
    fs.write_file(source, "content")

    fs.copy_path(source, tmp_path / "b.txt")
    fs.move_path(source, tmp_path / "c.txt")

    assert (tmp_path / "b.txt").exists()
    assert (tmp_path / "c.txt").exists()
    assert not source.exists()


def test_list_folder(tmp_path):
    fs.write_file(tmp_path / "one.txt", "1")
    fs.create_folder(tmp_path / "sub")

    listing = fs.list_folder(tmp_path)

    assert "sub" in listing["folders"]
    assert [item["name"] for item in listing["files"]] == ["one.txt"]


def test_find_files_wraps_bare_terms_in_wildcards(tmp_path):
    fs.write_file(tmp_path / "deep" / "budget_2026.csv", "x")

    found = fs.find_files(tmp_path, "budget")

    assert len(found["matches"]) == 1
    assert found["pattern"] == "*budget*"


def test_grep_files_finds_line_numbers(tmp_path):
    fs.write_file(tmp_path / "notes.txt", "alpha\nbeta needle\ngamma\n")

    hits = fs.grep_files(tmp_path, "NEEDLE")

    assert hits["hits"][0]["line"] == 2
    assert "needle" in hits["hits"][0]["text"]


def test_path_info_for_missing_path(tmp_path):
    assert fs.path_info(tmp_path / "nope.txt") == {
        "path": str(tmp_path / "nope.txt"),
        "exists": False,
    }


def test_resolve_path_expands_named_folders():
    assert fs.resolve_path("documents").name == "Documents"


def test_resolve_path_requires_a_value():
    with pytest.raises(FileToolError):
        fs.resolve_path("")


def test_missing_path_raises(tmp_path):
    with pytest.raises(FileToolError, match="does not exist"):
        fs.read_file(tmp_path / "absent.txt")
