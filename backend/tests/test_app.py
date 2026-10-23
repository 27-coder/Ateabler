from unittest.mock import patch

from ateable.app import main


def test_native_entry_dispatches_arguments_to_cli() -> None:
    with patch("ateable.cli.main", return_value=7) as cli_main:
        assert main(["DeviceInfo", "--json"]) == 7
    cli_main.assert_called_once_with(["DeviceInfo", "--json"])


def test_native_entry_opens_desktop_without_arguments() -> None:
    with patch("ateable.desktop.main", return_value=9) as desktop_main:
        assert main([]) == 9
    desktop_main.assert_called_once_with()


def test_native_entry_ignores_macos_process_serial_number() -> None:
    with patch("ateable.desktop.main", return_value=0) as desktop_main:
        assert main(["-psn_0_12345"]) == 0
    desktop_main.assert_called_once_with()
