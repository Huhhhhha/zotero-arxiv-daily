"""Offline coverage for the no-send SMTP preflight."""
import importlib.util
import smtplib
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "check_smtp", Path(__file__).parents[1] / ".github/scripts/check_smtp.py"
)
check_smtp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_smtp)


def setup_config(monkeypatch, port=465):
    monkeypatch.setenv("SENDER", "sender@example.org")
    monkeypatch.setenv("SENDER_PASSWORD", "example-app-password")
    monkeypatch.setenv("CUSTOM_CONFIG", f"email:\n  smtp_server: smtp.qq.com\n  smtp_port: {port}\n")


def test_implicit_tls_authentication_sends_no_email(monkeypatch, capsys):
    setup_config(monkeypatch)
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            calls.append(("ssl", host, port, timeout))
        def login(self, sender, password):
            calls.append(("login",))
        def quit(self):
            calls.append(("quit",))
    monkeypatch.setattr(smtplib, "SMTP_SSL", Connection)
    assert check_smtp.main() == 0
    assert calls == [("ssl", "smtp.qq.com", 465, 30), ("login",), ("quit",)]
    output = capsys.readouterr().out
    assert "No email was sent" in output
    assert "example-app-password" not in output


def test_disconnect_reports_failure_without_exposing_credentials(monkeypatch, capsys):
    setup_config(monkeypatch)
    class Connection:
        def __init__(self, *args, **kwargs):
            pass
        def login(self, sender, password):
            raise smtplib.SMTPServerDisconnected("sensitive details")
        def quit(self):
            raise smtplib.SMTPServerDisconnected()
        def close(self):
            pass
    monkeypatch.setattr(smtplib, "SMTP_SSL", Connection)
    assert check_smtp.main() == 1
    output = capsys.readouterr().err
    assert "SMTPServerDisconnected" in output
    assert "sensitive details" not in output
    assert "example-app-password" not in output


def test_missing_password_stops_before_connecting(monkeypatch, capsys):
    setup_config(monkeypatch)
    monkeypatch.setenv("SENDER_PASSWORD", "")
    def unexpected(*args, **kwargs):
        raise AssertionError("Must not connect without credentials")
    monkeypatch.setattr(smtplib, "SMTP_SSL", unexpected)
    assert check_smtp.main() == 1
    assert "missing" in capsys.readouterr().err


def test_starttls_used_on_non_ssl_port(monkeypatch):
    setup_config(monkeypatch, port=587)
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            calls.append(("tcp", host, port, timeout))
        def starttls(self):
            calls.append(("starttls",))
        def login(self, sender, password):
            calls.append(("login",))
        def quit(self):
            calls.append(("quit",))
    monkeypatch.setattr(smtplib, "SMTP", Connection)
    assert check_smtp.main() == 0
    assert calls == [("tcp", "smtp.qq.com", 587, 30), ("starttls",), ("login",), ("quit",)]
