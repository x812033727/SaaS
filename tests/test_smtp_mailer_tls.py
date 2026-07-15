"""SMTP TLS 模式與不洩漏服務商回應的可操作錯誤訊息。"""

import smtplib

import pytest

from saas_mvp.services.mailer import MailerError, SmtpMailer


class _FakeSmtp:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started_tls = False
        self.logged_in = None
        self.sent = None
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def starttls(self, **_kwargs):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent = message


def _mailer(port):
    return SmtpMailer(
        host="smtp.example.com",
        port=port,
        user="mailer@example.com",
        password="secret",
        from_address="mailer@example.com",
    )


def test_port_465_uses_implicit_tls(monkeypatch):
    implicit = type("ImplicitTls", (_FakeSmtp,), {"instances": []})
    explicit = type("ExplicitTls", (_FakeSmtp,), {"instances": []})
    monkeypatch.setattr("saas_mvp.services.mailer.smtplib.SMTP_SSL", implicit)
    monkeypatch.setattr("saas_mvp.services.mailer.smtplib.SMTP", explicit)

    _mailer(465).send(to="to@example.com", subject="test", body="body")

    assert len(implicit.instances) == 1
    assert not implicit.instances[0].started_tls
    assert not explicit.instances


def test_port_587_uses_starttls(monkeypatch):
    implicit = type("ImplicitTls", (_FakeSmtp,), {"instances": []})
    explicit = type("ExplicitTls", (_FakeSmtp,), {"instances": []})
    monkeypatch.setattr("saas_mvp.services.mailer.smtplib.SMTP_SSL", implicit)
    monkeypatch.setattr("saas_mvp.services.mailer.smtplib.SMTP", explicit)

    _mailer(587).send(to="to@example.com", subject="test", body="body")

    assert len(explicit.instances) == 1
    assert explicit.instances[0].started_tls
    assert not implicit.instances


@pytest.mark.parametrize(
    ("smtp_error", "expected"),
    [
        (smtplib.SMTPDataError(451, b"provider internal detail"), "稍後再試"),
        (smtplib.SMTPDataError(550, b"provider internal detail"), "寄送額度"),
        (smtplib.SMTPServerDisconnected("provider internal detail"), "寄送途中中斷"),
    ],
)
def test_send_failure_is_actionable_without_exposing_provider_detail(
    monkeypatch, smtp_error, expected
):
    class FailingSmtp(_FakeSmtp):
        instances = []

        def send_message(self, message):
            raise smtp_error

    monkeypatch.setattr("saas_mvp.services.mailer.smtplib.SMTP_SSL", FailingSmtp)

    with pytest.raises(MailerError) as caught:
        _mailer(465).send(to="to@example.com", subject="test", body="body")

    assert expected in str(caught.value)
    assert "provider internal detail" not in str(caught.value)
