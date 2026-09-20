"""Loading /etc/fpgas/watchdog.yml into a WatchdogConfig."""

import textwrap

import pytest

from fleet_watchdog.config import ConfigError, WatchdogConfig, load_config


def write(tmp_path, body):
    p = tmp_path / "watchdog.yml"
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_defaults_apply_when_only_required_keys_are_given(tmp_path):
    cfg = load_config(write(tmp_path, """
        switches_config: /etc/fpgas/switches.yml
        pib_network: "10.21"
        ssh_key: /var/lib/fleet-watchdog/id_ed25519
        known_hosts: /var/lib/fleet-watchdog/known_hosts
    """))
    assert isinstance(cfg, WatchdogConfig)
    assert cfg.interval == 300
    assert cfg.fail_threshold == 2
    assert cfg.ssh_timeout == 20
    assert cfg.probe_concurrency == 8
    assert cfg.poe_off_seconds == 30
    assert cfg.boot_grace == 300
    assert cfg.max_uptime_hours == 8
    assert cfg.uptime_jitter_minutes == 60
    assert cfg.hard_cap_hours == 12
    assert cfg.max_scheduled_cycles_per_sweep == 2
    assert cfg.cycle_concurrency == 2
    assert cfg.breaker_fraction == 0.5
    assert cfg.breaker_min_failures == 3
    assert cfg.ssh_user == "pi"
    assert cfg.exclude == {}


def test_values_override_defaults(tmp_path):
    cfg = load_config(write(tmp_path, """
        switches_config: /etc/fpgas/switches.yml
        pib_network: "10.21"
        ssh_key: /k
        known_hosts: /kh
        interval: 60
        fail_threshold: 5
        ssh_user: debian
    """))
    assert cfg.interval == 60
    assert cfg.fail_threshold == 5
    assert cfg.ssh_user == "debian"


def test_exclusions_are_keyed_by_switch_index_as_int(tmp_path):
    cfg = load_config(write(tmp_path, """
        switches_config: /etc/fpgas/switches.yml
        pib_network: "10.21"
        ssh_key: /k
        known_hosts: /kh
        exclude:
          1: [13]
          2: [11, 12, 27, 30]
    """))
    assert cfg.exclude == {1: frozenset({13}), 2: frozenset({11, 12, 27, 30})}


def test_a_missing_required_key_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="pib_network"):
        load_config(write(tmp_path, """
            switches_config: /etc/fpgas/switches.yml
            ssh_key: /k
            known_hosts: /kh
        """))


def test_the_config_is_immutable(tmp_path):
    cfg = load_config(write(tmp_path, """
        switches_config: /s
        pib_network: "10.21"
        ssh_key: /k
        known_hosts: /kh
    """))
    with pytest.raises(Exception):
        cfg.interval = 1


def test_an_unknown_key_is_fatal(tmp_path):
    """A typo must never load as the default. `max_uptime_hrs: 1` quietly
    leaving max_uptime_hours at 8 is how a fleet gets cycled on a rule nobody
    wrote, and nothing downstream could ever detect it."""
    with pytest.raises(ConfigError, match="max_uptime_hrs"):
        load_config(write(tmp_path, """
            switches_config: /s
            pib_network: "10.21"
            ssh_key: /k
            known_hosts: /kh
            max_uptime_hrs: 1
        """))


def test_the_unknown_key_error_lists_what_was_expected(tmp_path):
    with pytest.raises(ConfigError, match="max_uptime_hours"):
        load_config(write(tmp_path, """
            switches_config: /s
            pib_network: "10.21"
            ssh_key: /k
            known_hosts: /kh
            max_uptime_hrs: 1
        """))


def test_every_unknown_key_is_named_not_just_the_first(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, """
            switches_config: /s
            pib_network: "10.21"
            ssh_key: /k
            known_hosts: /kh
            intervl: 60
            zzz_bogus: 1
        """))
    assert "intervl" in str(exc.value)
    assert "zzz_bogus" in str(exc.value)


def test_a_value_of_the_wrong_type_names_the_key(tmp_path):
    with pytest.raises(ConfigError, match="interval"):
        load_config(write(tmp_path, """
            switches_config: /s
            pib_network: "10.21"
            ssh_key: /k
            known_hosts: /kh
            interval: not-a-number
        """))


def test_a_malformed_exclude_block_names_itself(tmp_path):
    with pytest.raises(ConfigError, match="exclude"):
        load_config(write(tmp_path, """
            switches_config: /s
            pib_network: "10.21"
            ssh_key: /k
            known_hosts: /kh
            exclude:
              1: 13
        """))


def test_the_new_health_settings_have_defaults(tmp_path):
    cfg = load_config(write(tmp_path, """
        switches_config: /s
        pib_network: "10.21"
        ssh_key: /k
        known_hosts: /kh
    """))
    assert cfg.unhealthy_exit_after == 3
    assert cfg.max_fault_clears == 3
