from aa_hmi.rfcomm import MAX_CHANNEL, build_channel_candidates


def test_no_sdp_hits_falls_back_to_full_brute_force_range():
    candidates = build_channel_candidates("AA:BB:CC:DD:EE:FF", sdptool_probe=lambda mac: [])
    assert candidates == list(range(1, MAX_CHANNEL + 1))


def test_sdp_hits_come_first_deduplicated():
    candidates = build_channel_candidates("AA:BB:CC:DD:EE:FF", sdptool_probe=lambda mac: [4, 7])
    assert candidates[:2] == [4, 7]
    assert candidates.count(4) == 1
    assert candidates.count(7) == 1
    assert len(candidates) == MAX_CHANNEL  # still covers every channel, just reordered
    assert set(candidates) == set(range(1, MAX_CHANNEL + 1))


def test_sdp_hits_out_of_valid_range_are_dropped():
    candidates = build_channel_candidates("AA:BB:CC:DD:EE:FF", sdptool_probe=lambda mac: [0, 99, 4])
    assert candidates[0] == 4
    assert 0 not in candidates
    assert 99 not in candidates
    assert len(candidates) == MAX_CHANNEL
