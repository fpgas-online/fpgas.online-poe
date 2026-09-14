"""Minimal Django settings so the snmp_switch views can be exercised with
django.test.Client, straight from this repo (the site normally supplies
settings, URL routing and the channel layer)."""

import django
from django.conf import settings


def pytest_configure():
    settings.configure(
        SECRET_KEY="not-a-secret",
        ROOT_URLCONF="snmp_switch.urls",
        ALLOWED_HOSTS=["testserver"],
        # notify_dcws() group_sends the PoE state to the board page; an
        # in-memory layer accepts it without a running consumer
        CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}},
    )
    django.setup()
