# -*- coding: utf-8 -*-
"""
Тестове покриття для TokenProvider‑ів (Instagram, TikTok, Threads).
Перевіряє, що `access_token()` правильно отримує токен із середовища або secret‑store.
"""

import os
from types import SimpleNamespace

import pytest

# Import провайдерів
from publishers.oauth import (
    InstagramTokenProvider,
    TikTokTokenProvider,
    ThreadsTokenProvider,
    # Для контролю кэшування, використаємо базовий клас
    BaseTokenProvider,
)

# Helper to mock secret store
def _mock_secret_store(monkeypatch, secret_name, secret_value):
    """Підміняє `core.first_run.ensure_secret_store` щоб повернути dict із заданим секретом."""
    from core import first_run

    def fake_ensure_secret_store():
        return {secret_name: secret_value}

    monkeypatch.setattr(first_run, "ensure_secret_store", fake_ensure_secret_store)


@pytest.mark.parametrize(
    "provider_cls, env_name, secret_name, token_value",
    [
        (InstagramTokenProvider, "INSTAGRAM_ACCESS_TOKEN", "instagram_access_token", "insta-token-123"),
        (TikTokTokenProvider, "TIKTOK_ACCESS_TOKEN", "tiktok_access_token", "tiktok-token-456"),
        (ThreadsTokenProvider, "THREADS_ACCESS_TOKEN", "threads_access_token", "threads-token-789"),
    ],
)
def test_access_token_from_env(monkeypatch, provider_cls, env_name, secret_name, token_value):
    """Тестує, що токен успішно читається з змінної оточення."""
    # Ensure середовище чисте
    monkeypatch.delenv(env_name, raising=False)
    # Встановлюємо змінну оточення
    monkeypatch.setenv(env_name, token_value)
    # Mock secret store – не повинна бути викликана
    _mock_secret_store(monkeypatch, secret_name, "should-not-be-used")

    provider = provider_cls()
    assert provider.access_token() == token_value


@pytest.mark.parametrize(
    "provider_cls, env_name, secret_name, token_value",
    [
        (InstagramTokenProvider, "INSTAGRAM_ACCESS_TOKEN", "instagram_access_token", "insta-secret-321"),
        (TikTokTokenProvider, "TIKTOK_ACCESS_TOKEN", "tiktok_access_token", "tiktok-secret-654"),
        (ThreadsTokenProvider, "THREADS_ACCESS_TOKEN", "threads_access_token", "threads-secret-987"),
    ],
)
def test_access_token_from_secret_store(monkeypatch, provider_cls, env_name, secret_name, token_value):
    """Тестує, що токен читається зі secret‑store, коли змінна оточення відсутня."""
    # Видаляємо змінну оточення, якщо вона існує
    monkeypatch.delenv(env_name, raising=False)
    # Підмінаємо secret store функцію
    _mock_secret_store(monkeypatch, secret_name, token_value)

    provider = provider_cls()
    assert provider.access_token() == token_value


def test_caching_behavior(monkeypatch):
    """Переконуємося, що токен кешується після першого отримання.
    Викликаємо `_get_secret` лише один раз.
    """
    # Підміняємо `publishers.oauth._get_secret` щоб відстежити виклики
    from publishers import oauth

    call_counter = SimpleNamespace(count=0)

    def counting_get_secret(name):
        call_counter.count += 1
        return "cached-token"

    monkeypatch.setattr(oauth, "_get_secret", counting_get_secret)

    provider = oauth.BaseTokenProvider()
    # Перша згода, повинна викликати _get_secret
    token1 = provider.access_token()
    # Друга згода, має використати кеш і не викликати знову
    token2 = provider.access_token()

    assert token1 == "cached-token"
    assert token2 == "cached-token"
    assert call_counter.count == 1

