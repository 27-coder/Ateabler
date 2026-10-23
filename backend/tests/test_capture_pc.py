import pytest

from ateable.capture_pc import (
    capture_local,
    install_windows_menu,
    local_bucket_store,
    parse_capture_text,
    read_capture_file,
)


def test_capture_preserves_signed_links_csv_and_utf16_shortcuts(tmp_path):
    url = "https://objects.example/data.zip?signature=abc,def&token=kept"
    csv = tmp_path / "urls.csv"
    csv.write_text('title,url\n"Data","' + url + '"\n')
    assert read_capture_file(csv) == [{"url": url}]
    shortcut = tmp_path / "object.url"
    shortcut.write_bytes(("[InternetShortcut]\nURL=" + url + "\n").encode("utf-16"))
    assert read_capture_file(shortcut) == [{"url": url}]
    assert parse_capture_text(url + "\n" + url) == [{"url": url}]


def test_offline_capture_is_durable_without_master_download_directory(tmp_path):
    state = tmp_path / "state"
    items = [{"url": "https://example.com/model.bin"}]
    first = capture_local(state, items)
    again = capture_local(state, items, first["bucket_id"])
    assert first["accepted"] == 1 and again["duplicates"] == 1
    assert not (state / "unused-master-output").exists()


def test_renamed_inbox_still_receives_default_pc_capture(tmp_path):
    local_bucket_store(tmp_path).update_bucket("inbox", name="Later")
    result = capture_local(tmp_path, [{"url": "https://example.com/archive.zip"}])
    assert result["bucket_id"] == "inbox" and result["accepted"] == 1


def test_local_media_is_not_uploaded_or_read_as_a_capture_list(tmp_path):
    media = tmp_path / "video.mp4"
    media.write_bytes(b"local media")
    with pytest.raises(ValueError, match="local media"):
        read_capture_file(media)
    with pytest.raises(ValueError):
        parse_capture_text("blob:https://example.com/x\nfile:///secret")


def test_registry_menu_is_per_user_and_keeps_user_paths_as_argv(tmp_path):
    root = tmp_path / "project with spaces"
    (root / ".venv" / "Scripts").mkdir(parents=True)
    (root / ".venv" / "Scripts" / "pythonw.exe").touch()
    (root / "scripts").mkdir()
    (root / "scripts" / "capture-to-bucket.py").touch()
    writes = []

    class Registry:
        HKEY_CURRENT_USER = "USER"
        KEY_WRITE = 1
        REG_SZ = 1

        def CreateKeyEx(self, hive, path, *args):
            assert hive == "USER"
            writes.append(path)

            class Key:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

            return Key()

        def SetValueEx(self, key, name, reserved, kind, value):
            if value.startswith('"'):
                assert '"%1"' in value
                assert "--quiet" in value and "--state-dir" in value

    assert len(install_windows_menu(root, registry=Registry())) == 5
    assert all(
        "SystemFileAssociations" in path and "Ateable.CaptureBucket" in path
        for path in writes
    )
