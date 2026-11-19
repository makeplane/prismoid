# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's archive filters: what leaves a frame, and what may enter one."""

import io
import tarfile
from typing import Any

import pytest

from prismoid.ai.frames import archives
from prismoid.ai.frames.archives import ChunkReader, checkpoint_filter, empty_tar, restore_filter, rewrite
from prismoid.ai.frames.layout import AGENT_UID, BROWSER_UID


def _tar(*members: tuple[tarfile.TarInfo, bytes | None]) -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for info, data in members:
            if data is not None:
                info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    buf.seek(0)
    return buf


def _file(name: str, data: bytes = b"x", mode: int = 0o644, uid: int = 0) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.mode, info.uid = mode, uid
    return info, data


def _link(name: str, target: str, kind: bytes = tarfile.SYMTYPE) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    return info, None


def _members(buf: io.BytesIO) -> dict[str, tarfile.TarInfo]:
    buf.seek(0)
    with tarfile.open(fileobj=buf, mode="r") as tar:
        return {m.name: m for m in tar.getmembers()}


def test_restoring_work_cannot_escape_through_a_symlink_or_a_special_file() -> None:
    dev = tarfile.TarInfo("work/null")
    dev.type = tarfile.CHRTYPE
    src = _tar(
        _file("work/notes.md"),
        _link("work/evil", "/etc"),
        _file("work/evil/cron.d/x"),  # beneath a symlink seen earlier: dropped
        _file("work/suid", mode=0o4755),
        _link("work/hard", "work/notes.md", tarfile.LNKTYPE),  # to a file kept above: kept
        (dev, None),
        _file("etc/passwd"),  # outside the directory being restored
        _file("work/../etc/shadow"),
    )
    out = io.BytesIO()
    rewrite(src, out, restore_filter("/work"))
    kept = _members(out)
    assert set(kept) == {"work/notes.md", "work/evil", "work/suid", "work/hard"}
    assert all(m.uid == AGENT_UID and m.gid == AGENT_UID for m in kept.values())
    assert kept["work/suid"].mode & 0o7000 == 0


def test_restoring_refuses_a_root_symlink_and_paths_that_dodge_an_earlier_symlink() -> None:
    """The directory being restored must stay a directory (`work -> /etc` would make every later
    write to `/work` land in `/etc`), and a non-canonical name (`work/./evil/…`, `work//evil/…`)
    must not slip past the "beneath a symlink" check - the daemon cleans the path and follows it."""
    root_link = _tar(_link("work", "/etc"), _file("work/cron.d/x"))
    out = io.BytesIO()
    rewrite(root_link, out, restore_filter("/work"))
    assert set(_members(out)) == {"work/cron.d/x"}  # `/work` stays a directory: this lands inside it

    dodges = _tar(
        _link("work/evil", "/etc"),
        _file("work/./evil/cron.d/x"),
        _file("work//evil/cron.d/y"),
        _file("work/ok"),
    )
    out = io.BytesIO()
    rewrite(dodges, out, restore_filter("/work"))
    assert set(_members(out)) == {"work/evil", "work/ok"}


def test_restoring_keeps_a_hard_link_to_a_file_kept_earlier_as_the_same_content() -> None:
    """Docker's tar emits the second name of a multiply-linked file as a hard link (git objects
    from a local clone, pnpm/uv caches): dropping it would lose that name on every wake."""
    src = _tar(_file("work/a", b"shared"), _link("work/b", "./work/a", tarfile.LNKTYPE))
    out = io.BytesIO()
    rewrite(src, out, restore_filter("/work"))
    out.seek(0)
    with tarfile.open(fileobj=out, mode="r") as tar:
        b = tar.getmember("work/b")
        assert b.islnk() and b.linkname == "work/a" and b.uid == AGENT_UID
        first, second = tar.extractfile("work/a"), tar.extractfile("work/b")
        assert first is not None and second is not None
        assert first.read() == second.read() == b"shared"


def test_restoring_drops_a_hard_link_to_anything_but_a_regular_file_kept_earlier() -> None:
    folder = tarfile.TarInfo("work/dir")
    folder.type = tarfile.DIRTYPE
    src = _tar(
        _link("work/h-out", "etc/passwd", tarfile.LNKTYPE),  # outside the root
        _link("work/h-early", "work/later", tarfile.LNKTYPE),  # not seen yet
        _file("work/later"),
        (folder, None),
        _link("work/h-dir", "work/dir", tarfile.LNKTYPE),  # a directory
        _link("work/sym", "/etc/shadow"),
        _link("work/h-sym", "work/sym", tarfile.LNKTYPE),  # a symlink
        _file("work/d/x"),
        _link("work/d", "/etc"),  # its parent became a symlink after it was kept
        _link("work/h-through", "work/d/x", tarfile.LNKTYPE),
        _file("work/f"),
        _link("work/f", "/etc/shadow"),  # replaced by a symlink after it was kept
        _link("work/h-replaced", "work/f", tarfile.LNKTYPE),
        _link("work/h-dots", "work/./later", tarfile.LNKTYPE),  # not canonical
        _link("work/h-root", "work", tarfile.LNKTYPE),  # the root itself
    )
    out = io.BytesIO()
    rewrite(src, out, restore_filter("/work"))
    assert set(_members(out)) == {"work/later", "work/dir", "work/sym", "work/d/x", "work/d", "work/f"}


def test_rewrite_keeps_no_member_list_however_long_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    """tarfile appends every TarInfo to `.members` even in stream mode, on the reader and the
    writer: a checkpoint of a million files would hold a million headers. `rewrite` drops them."""
    src = _tar(*[_file(f"work/f{i}") for i in range(500)], *[_file(f"etc/x{i}") for i in range(500)])
    opened: list[tarfile.TarFile] = []
    real_open = tarfile.open

    def spy(*args: Any, **kwargs: Any) -> tarfile.TarFile:
        tar = real_open(*args, **kwargs)
        opened.append(tar)
        return tar

    monkeypatch.setattr(tarfile, "open", spy)
    assert rewrite(src, io.BytesIO(), restore_filter("/work")) == 500
    assert len(opened) == 2
    assert all(tar.members == [] for tar in opened)  # pyright: ignore[reportAttributeAccessIssue]


def test_restoring_the_browser_stamps_its_owner_and_private_directories() -> None:
    top = tarfile.TarInfo("browser")
    top.type, top.mode = tarfile.DIRTYPE, 0o755
    prof = tarfile.TarInfo("browser/profile")
    prof.type, prof.mode = tarfile.DIRTYPE, 0o755
    src = _tar((top, None), (prof, None), _file("browser/profile/Default/Cookies"), _file("work/stray"))
    out = io.BytesIO()
    rewrite(src, out, restore_filter("/browser"))
    kept = _members(out)
    assert set(kept) == {"browser", "browser/profile", "browser/profile/Default/Cookies"}
    assert kept["browser"].mode == 0o700 and kept["browser/profile"].mode == 0o700
    assert all(m.uid == BROWSER_UID for m in kept.values())


def test_the_browser_checkpoint_drops_locks_and_caches_and_work_passes_through() -> None:
    src = _tar(
        _file("browser/profile/Default/Cookies"),
        _link("browser/profile/SingletonLock", "host-123"),
        _file("browser/profile/Default/Cache/Cache_Data/data_0"),
        _file("browser/profile/Default/Code Cache/js/index"),
        _file("browser/profile/GPUCache/data_1"),
    )
    out = io.BytesIO()
    rewrite(src, out, checkpoint_filter("/browser"))
    assert set(_members(out)) == {"browser/profile/Default/Cookies"}

    work = _tar(_file("work/Singleton.java"), _file("work/Cache/keep.txt"))  # not Chromium's: untouched
    out = io.BytesIO()
    rewrite(work, out, checkpoint_filter("/work"))
    assert set(_members(out)) == {"work/Singleton.java", "work/Cache/keep.txt"}


def test_restoring_ignores_pax_headers_that_would_override_the_stamped_owner() -> None:
    """A tampered tar can carry PAX records (`uid=0`, `uname=root`, an xattr) that outrank the
    ustar fields the filter stamps - `tarfile` copies a member's PAX records into the header it
    writes, and Docker's reader honours PAX over ustar. The boundary drops them."""
    info, data = _file("work/root-owned")
    info.pax_headers = {"uid": "0", "gid": "0", "uname": "root", "SCHILY.xattr.security.capability": "x"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    buf.seek(0)
    out = io.BytesIO()
    rewrite(buf, out, restore_filter("/work"))
    kept = _members(out)["work/root-owned"]
    assert (kept.uid, kept.gid, kept.uname) == (AGENT_UID, AGENT_UID, "agent")
    assert not any(key.startswith("SCHILY.xattr") for key in kept.pax_headers)


def test_restoring_drops_member_types_that_are_not_files_directories_or_symlinks() -> None:
    odd = tarfile.TarInfo("work/odd")
    odd.type = b"Z"  # a type tarfile does not know: never written back as a header with no body
    src = _tar(_file("work/ok"), (odd, None))
    out = io.BytesIO()
    rewrite(src, out, restore_filter("/work"))
    assert set(_members(out)) == {"work/ok"}


def test_a_gzip_checkpoint_restores_and_keeps_a_files_own_mode() -> None:
    """`unpack` takes a plain or gzip tar (a stored checkpoint is `.tar.gz`); only the two browser
    directories have their mode forced - a file keeps its own (minus the special bits)."""
    raw = _tar(_file("work/private.key", mode=0o600))
    gz = io.BytesIO()
    import gzip

    gz.write(gzip.compress(raw.getvalue()))
    gz.seek(0)
    out = io.BytesIO()
    rewrite(gz, out, restore_filter("/work"))
    assert _members(out)["work/private.key"].mode == 0o600


def test_a_chunked_stream_reads_whole_across_uneven_and_empty_chunks() -> None:
    """Docker's `get_archive` yields 2 MiB chunks; the reader hands them to tarfile as one stream."""
    data = bytes(range(256)) * 40
    chunks = [data[:1], b"", data[1:5000], data[5000:5001], b"", data[5001:]]
    reader = io.BufferedReader(ChunkReader(chunks))
    assert reader.read() == data and reader.read() == b""


def test_an_absent_directory_archives_as_an_empty_tar_that_restores_to_nothing() -> None:
    out = io.BytesIO()
    empty_tar(out)
    out.seek(0)
    restored = io.BytesIO()
    assert rewrite(out, restored, restore_filter("/work")) == 0


def test_restoring_refuses_a_checkpoint_past_the_member_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared worker holds every kept name while it restores (the symlink and hard-link checks
    need them), so a crafted checkpoint of millions of entries is refused as unreadable, and the
    store reads an unreadable checkpoint as lost: the Machine rebuilds."""
    monkeypatch.setattr(archives, "RESTORE_MAX_MEMBERS", 10)
    src = _tar(*(_file(f"work/f{n}") for n in range(11)))
    with pytest.raises(archives.RestoreTooLarge) as refused:
        rewrite(src, io.BytesIO(), restore_filter("/work"))
    assert isinstance(refused.value, tarfile.TarError)


def test_restoring_refuses_a_checkpoint_past_the_name_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(archives, "RESTORE_NAME_BYTES", 1_000)
    src = _tar(*(_file(f"work/{'n' * 90}{n}") for n in range(20)))  # ~1,900 bytes of names
    with pytest.raises(archives.RestoreTooLarge):
        rewrite(src, io.BytesIO(), restore_filter("/work"))


def test_an_honest_checkpoint_under_the_caps_restores() -> None:
    src = _tar(*(_file(f"work/src/f{n}.py") for n in range(50)))
    dst = io.BytesIO()
    assert rewrite(src, dst, restore_filter("/work")) == 50
