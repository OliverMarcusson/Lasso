from lasso.daemon import Backoff, Observation, OfflineTracker, State, auto_connect_block, user_active
from lasso.nm import known_connections, parse_ethernet_up, parse_ssids, split_terse


def offline_tracker(since=0.0):
    t = OfflineTracker()
    t.update("none", since, False)
    return t


def block(obs=None, tracker=None, backoff=None, now=100.0, state=State.IDLE):
    return auto_connect_block(obs or Observation(connectivity="none"), tracker or offline_tracker(),
                              backoff or Backoff(), now, 20, state)


def test_all_conditions_met():
    assert block() is None


def test_needs_grace_period_offline():
    assert block(now=10) == "not offline long enough"
    assert block(now=20) is None


def test_online_never_connects():
    t = OfflineTracker()
    t.update("full", 0, False)
    assert block(tracker=t) == "not offline long enough"


def test_limited_connectivity_counts_as_offline():
    t = OfflineTracker()
    t.update("limited", 0, False)
    assert block(tracker=t) is None


def test_ethernet_known_wifi_idle_block():
    assert block(Observation(ethernet_up=True)) == "ethernet is up"
    assert block(Observation(known_wifi=["Home"])) == "known Wi-Fi in range"
    assert block(Observation(user_active=False)) == "user idle or locked"


def test_busy_state_blocks():
    assert block(state=State.JOINING) == "busy (JOINING)"


def test_manual_disconnect_holds_until_network_returns():
    t = offline_tracker()
    t.mark_manual(50)
    assert block(tracker=t) == "manual disconnect"
    t.update("none", 120, False)
    assert block(tracker=t) == "manual disconnect"
    assert t.update("full", 130, False) is True   # real network came back
    assert t.manual is False
    t.update("none", 140, False)
    assert block(tracker=t, now=170) is None


def test_stale_full_right_after_manual_disconnect_is_ignored():
    t = OfflineTracker()
    t.update("full", 0, True)
    t.mark_manual(10)
    t.update("full", 11, False)
    assert t.manual is True


def test_lasso_connectivity_does_not_end_manual_or_offline_period_event():
    t = offline_tracker()
    t.mark_manual(0)
    assert t.update("full", 100, True) is False
    assert t.manual is True


def test_backoff_steps():
    b = Backoff()
    assert b.allowed(0)
    b.failed(0)
    assert not b.allowed(59) and b.allowed(60)
    b.failed(60)
    assert not b.allowed(359) and b.allowed(360)
    b.failed(360)
    assert not b.allowed(1259) and b.allowed(1260)
    b.failed(1260)
    assert b.until == 1260 + 900   # stays at 15 min
    b.reset()
    assert b.allowed(0)


def test_backoff_blocks_gate():
    b = Backoff()
    b.failed(95)
    assert block(backoff=b, now=100) == "backing off"
    assert block(backoff=b, now=156) is None


def test_user_active():
    now = 10_000_000_000
    assert user_active({"IdleHint": "no", "LockedHint": "no"}, now, 5)
    assert not user_active({"LockedHint": "yes"}, now, 5)
    assert user_active({"IdleHint": "yes", "IdleSinceHint": str(now - 4 * 60_000_000)}, now, 5)
    assert not user_active({"IdleHint": "yes", "IdleSinceHint": str(now - 6 * 60_000_000)}, now, 5)
    assert user_active({}, now, 5)


def test_terse_parsing():
    assert split_terse(r"My\:SSID:wifi:a\\b") == ["My:SSID", "wifi", "a\\b"]
    assert parse_ssids("Home\n\nLasso-abc123\nCafe\\:5G\n") == {"Home", "Lasso-abc123", "Cafe:5G"}
    assert parse_ethernet_up("wifi:connected\nethernet:unavailable\n") is False
    assert parse_ethernet_up("wifi:connected\nethernet:connected\n") is True


def test_known_connections_intersection():
    saved = {"Home": "Home wifi", "Work": "Work"}
    assert known_connections({"Home", "Other"}, saved) == ["Home wifi"]
    assert known_connections({"Other"}, saved) == []
