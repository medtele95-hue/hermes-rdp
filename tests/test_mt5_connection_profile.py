# -*- coding: utf-8 -*-
"""mission/DASHBOARD.md — MT5Connection.connect() gained optional
login/password/server/path kwargs. The critical regression to prevent: the
zero-arg call (used by every existing call site in app/main.py and the
diagnostic tools) must remain byte-for-byte today's behaviour —
`mt5.initialize()` with no kwargs at all."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from app.mt5.connection import MT5Connection


class TestConnectBackwardCompatible(unittest.TestCase):
    def _connection_with_mocked_mt5(self):
        conn = MT5Connection()
        return conn

    def test_zero_args_calls_initialize_with_no_kwargs(self) -> None:
        with patch("app.mt5.connection.mt5") as mock_mt5:
            mock_mt5.initialize.return_value = True
            mock_mt5.terminal_info.return_value = object()
            mock_mt5.account_info.return_value = object()
            conn = self._connection_with_mocked_mt5()
            result = conn.connect()
        self.assertTrue(result)
        mock_mt5.initialize.assert_called_once_with()

    def test_partial_credentials_do_not_trigger_explicit_login(self) -> None:
        """login without password+server must NOT be passed through partially
        — MT5 would misinterpret a partial credential set."""
        with patch("app.mt5.connection.mt5") as mock_mt5:
            mock_mt5.initialize.return_value = True
            mock_mt5.terminal_info.return_value = object()
            mock_mt5.account_info.return_value = object()
            conn = self._connection_with_mocked_mt5()
            conn.connect(login=12345)
        mock_mt5.initialize.assert_called_once_with()

    def test_full_credentials_passed_through(self) -> None:
        with patch("app.mt5.connection.mt5") as mock_mt5:
            mock_mt5.initialize.return_value = True
            mock_mt5.terminal_info.return_value = object()
            mock_mt5.account_info.return_value = object()
            conn = self._connection_with_mocked_mt5()
            conn.connect(login=12345, password="secret", server="Broker-Demo")
        mock_mt5.initialize.assert_called_once_with(login=12345, password="secret", server="Broker-Demo")

    def test_path_only_passed_through_alone(self) -> None:
        with patch("app.mt5.connection.mt5") as mock_mt5:
            mock_mt5.initialize.return_value = True
            mock_mt5.terminal_info.return_value = object()
            mock_mt5.account_info.return_value = object()
            conn = self._connection_with_mocked_mt5()
            conn.connect(path="C:\\MT5\\terminal64.exe")
        mock_mt5.initialize.assert_called_once_with(path="C:\\MT5\\terminal64.exe")

    def test_path_and_full_credentials_combined(self) -> None:
        with patch("app.mt5.connection.mt5") as mock_mt5:
            mock_mt5.initialize.return_value = True
            mock_mt5.terminal_info.return_value = object()
            mock_mt5.account_info.return_value = object()
            conn = self._connection_with_mocked_mt5()
            conn.connect(login=1, password="p", server="s", path="C:\\MT5\\terminal64.exe")
        mock_mt5.initialize.assert_called_once_with(
            path="C:\\MT5\\terminal64.exe", login=1, password="p", server="s",
        )


if __name__ == "__main__":
    unittest.main()
