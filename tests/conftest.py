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
        # what a site must supply before the views act on anything
        # (snmp_switch.policy): which ports are boards, and a store for the
        # power-cycle rate limit (one process here, so local memory will do)
        SNMP_SWITCH_PORT_POLICY="tests.policy.offered",
        SNMP_SWITCH_RATE_LIMIT_CACHE="poe-rate-limit",
        CACHES={
            "default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"},
            "poe-rate-limit": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
        },
    )
    django.setup()
