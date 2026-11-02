import asyncio
import gzip
import os
import shutil
import tempfile
import time
from typing import Any
import orjson

from cache import SimpleLRU
from telegram_client import TelegramManager, _json_default
from search import SearchEngine
from duplicates import is_duplicate, choose_keeper, get_quality_score, load_kept_ids, save_kept_ids
from botpool import BotPool, BotLease


async def run_all_tests():
    print("=" * 60)
    print("STARTING TEST SUITE FOR TELEGRAM MUSIC STREAM FIXES")
    print("=" * 60)
    passed = 0
    total = 9

    # -------------------------------------------------------------
    # TEST 1: Cache save and reload
    # -------------------------------------------------------------
    print("\n--- TEST 1: Cache save and reload ---")
    tmp_dir = tempfile.mkdtemp()
    try:
        tm = TelegramManager()
        tm._cache_file_gz = os.path.join(tmp_dir, "test_cache.json.gz")
        tm._cache_file_tmp = tm._cache_file_gz + ".tmp"

        sample_tracks = [
            {"id": "101", "title": "Song One", "artist": "Artist A", "fileSize": 1000, "duration": 180},
            {"id": "102", "title": "Song Two", "artist": "Artist B", "fileSize": 2000, "duration": 210},
        ]
        tm.track_index = list(sample_tracks)
        await tm.save_cache()

        assert os.path.exists(tm._cache_file_gz), "Cache file was not created"
        with gzip.open(tm._cache_file_gz, "rb") as f:
            decompressed = f.read()
        loaded_json = orjson.loads(decompressed)
        assert len(loaded_json) == 2, f"Expected 2 tracks, got {len(loaded_json)}"

        # Now test load_cache
        tm2 = TelegramManager()
        tm2._cache_file_gz = tm._cache_file_gz
        assert tm2.load_cache() is True, "load_cache returned False"
        assert len(tm2.track_index) == 2
        assert tm2.track_index[0]["id"] == "101"
        assert tm2.track_index[1]["title"] == "Song Two"

        print("✓ Test 1 Passed: Cache saved, compressed, and reloaded successfully.")
        passed += 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------
    # TEST 2: Cache serialization with actual track metadata (sets/frozensets)
    # -------------------------------------------------------------
    print("\n--- TEST 2: Cache serialization with actual track metadata ---")
    tmp_dir = tempfile.mkdtemp()
    try:
        tm = TelegramManager()
        tm._cache_file_gz = os.path.join(tmp_dir, "test_cache2.json.gz")
        tm._cache_file_tmp = tm._cache_file_gz + ".tmp"

        track_with_sets = {
            "id": "201",
            "title": "Kesariya (From Brahmastra)",
            "artist": "Arijit Singh, Pritam",
            "album": "Brahmastra",
            "duration": 268,
            "format": "flac",
            "fileSize": 45000000,
            "fileName": "Kesariya.flac",
            "isAtmos": False,
            "quality": "16-bit / 44.1kHz FLAC Lossless",
            "_search_words": frozenset({"kesariya", "brahmastra", "arijit", "singh", "pritam"}),
            "tags": {"bollywood", "romantic"},  # set object
            "raw_bytes": b"metadata_tag",  # bytes object
        }
        tm.track_index = [track_with_sets]

        # Saving should NOT raise TypeError: Type is not JSON serializable: set / frozenset
        await tm.save_cache()

        # Load back
        tm2 = TelegramManager()
        tm2._cache_file_gz = tm._cache_file_gz
        assert tm2.load_cache() is True

        loaded_t = tm2.track_index[0]
        assert loaded_t["id"] == "201"
        assert isinstance(loaded_t["_search_words"], frozenset)
        assert "kesariya" in loaded_t["_search_words"]

        # Search engine queries work seamlessly on loaded tracks
        se = SearchEngine(tm2.track_index)
        results = se.search("Kesariya")
        assert len(results) == 1
        assert results[0]["id"] == "201"

        print("✓ Test 2 Passed: Set and frozenset serialized safely without error; search engine functional.")
        passed += 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------
    # TEST 3: New tracks arriving during indexing
    # -------------------------------------------------------------
    print("\n--- TEST 3: New tracks arriving during indexing ---")
    tmp_dir = tempfile.mkdtemp()
    try:
        tm = TelegramManager()
        tm._cache_file_gz = os.path.join(tmp_dir, "test_cache3.json.gz")
        tm._cache_file_tmp = tm._cache_file_gz + ".tmp"

        # Mock client that yields 3 messages with simulated delay
        class MockMsg:
            def __init__(self, mid, name):
                self.id = mid
                self.name = name

        class MockClient:
            def __init__(self, manager):
                self.manager = manager

            def on(self, event):
                def decorator(fn):
                    return fn
                return decorator

            async def iter_messages(self, channel, reverse=True):
                for mid in [301, 302]:
                    await asyncio.sleep(0.05)
                    yield MockMsg(mid, f"Track {mid}")

        tm.client = MockClient(tm)
        tm.channel_entity = "mock_channel"

        def mock_parse(msg):
            return {
                "id": str(msg.id),
                "title": getattr(msg, "name", f"Track {msg.id}"),
                "fileName": f"track_{msg.id}.mp3",
                "fileSize": 1000,
                "duration": 120,
            }
        tm._parse_track_message = mock_parse

        # Concurrently simulate an incoming message during indexing
        async def incoming_sender():
            await asyncio.sleep(0.02)  # fires while iter_messages is running
            new_track = {
                "id": "399",
                "title": "Realtime Incoming Track",
                "fileName": "track_399.mp3",
                "fileSize": 2500,
                "duration": 150,
            }
            if tm.is_indexing:
                tm._incoming_during_indexing.append(new_track)
            tm.track_index.insert(0, new_track)

        task_idx = asyncio.create_task(tm.index_tracks())
        task_inc = asyncio.create_task(incoming_sender())
        await asyncio.gather(task_idx, task_inc)

        ids = [t["id"] for t in tm.track_index]
        assert "399" in ids, f"Incoming track 399 was overwritten! Current IDs: {ids}"
        assert "301" in ids and "302" in ids, f"Indexed tracks missing! Current IDs: {ids}"

        print(f"✓ Test 3 Passed: Track arriving during indexing (ID 399) was preserved. Total tracks: {len(tm.track_index)}.")
        passed += 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------
    # TEST 4: Multiple indexing requests
    # -------------------------------------------------------------
    print("\n--- TEST 4: Multiple indexing requests ---")
    tmp_dir = tempfile.mkdtemp()
    try:
        tm = TelegramManager()
        tm._cache_file_gz = os.path.join(tmp_dir, "test_cache4.json.gz")
        tm._cache_file_tmp = tm._cache_file_gz + ".tmp"

        runs = 0

        class SlowClient:
            def on(self, event):
                def decorator(fn):
                    return fn
                return decorator

            async def iter_messages(self, channel, reverse=True):
                nonlocal runs
                runs += 1
                await asyncio.sleep(0.1)
                for mid in [401, 402]:
                    yield MockMsg(mid, f"Track {mid}")

        tm.client = SlowClient()
        tm.channel_entity = "mock_channel"
        tm._parse_track_message = lambda m: {"id": str(m.id), "title": m.name, "fileName": f"{m.id}.mp3", "fileSize": 1000, "duration": 120}

        # Launch 3 simultaneous indexing requests
        results = await asyncio.gather(
            tm.index_tracks(),
            tm.index_tracks(),
            tm.index_tracks(),
        )

        assert runs == 1, f"Expected exactly 1 channel scan, but got {runs}"
        assert all(len(r) == 2 for r in results), "All callers must receive full index"

        # Also verify error resilience: temporary network error must NOT discard existing tracks
        class ErrorClient:
            def on(self, event):
                return lambda fn: fn
            async def iter_messages(self, channel, reverse=True):
                yield MockMsg(999, "Temp Msg")
                raise ConnectionResetError("Simulated Telegram connection drop")

        tm.client = ErrorClient()
        retained = await tm.index_tracks()
        assert len(retained) == 2, f"Expected 2 tracks retained on error, got {len(retained)}"
        assert tm.track_index[0]["id"] in ["401", "402"], "Original tracks must be preserved"

        print("✓ Test 4 Passed: Multiple indexing requests synchronized; error resilience preserved existing tracks.")
        passed += 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # -------------------------------------------------------------
    # TEST 5: Duplicate detection safety
    # -------------------------------------------------------------
    print("\n--- TEST 5: Duplicate detection safety ---")
    base_track = {
        "id": "501",
        "title": "Channa Mereya",
        "fileName": "Channa_Mereya.flac",
        "format": "flac",
        "fileSize": 30000000,
        "duration": 289,
    }

    # 1. Exact match of all 4 criteria -> Duplicate
    exact_dup = {
        "id": "502",
        "title": "Channa Mereya",
        "fileName": "CHANNA_MEREYA.flac",
        "format": "flac",
        "fileSize": 30000000,
        "duration": 289,
    }
    assert is_duplicate(base_track, exact_dup) is True, "Exact match should be duplicate"

    # 2. Different extension -> NOT duplicate (Song.mp3 + Song.flac -> Keep both)
    diff_ext = dict(base_track, id="503", fileName="Channa_Mereya.mp3", format="mp3")
    assert is_duplicate(base_track, diff_ext) is False, "Different extension must NOT be duplicate"

    # 3. Different size -> NOT duplicate
    diff_size = dict(base_track, id="504", fileSize=29999999)
    assert is_duplicate(base_track, diff_size) is False, "Different size must NOT be duplicate"

    # 4. Different duration -> NOT duplicate
    diff_dur = dict(base_track, id="505", duration=290)
    assert is_duplicate(base_track, diff_dur) is False, "Different duration must NOT be duplicate"

    # 5. Missing metadata -> NOT duplicate
    missing_fn = dict(base_track, id="506", fileName=None)
    assert is_duplicate(base_track, missing_fn) is False, "Missing filename must NOT be duplicate"
    missing_sz = dict(base_track, id="507", fileSize=None)
    assert is_duplicate(base_track, missing_sz) is False, "Missing size must NOT be duplicate"
    missing_du = dict(base_track, id="508", duration=None)
    assert is_duplicate(base_track, missing_du) is False, "Missing duration must NOT be duplicate"

    # 6. Quality-based keeper selection
    high_q = {"id": "510", "format": "flac", "fileSize": 30000000, "duration": 200, "bitDepth": 16}
    low_q = {"id": "511", "format": "mp3", "fileSize": 5000000, "duration": 200}
    keeper, cand = choose_keeper(high_q, low_q)
    assert keeper["id"] == "510", "FLAC must be chosen over MP3"

    print("✓ Test 5 Passed: 4-criteria duplicate detection and quality-based selection verified.")
    passed += 1

    # -------------------------------------------------------------
    # TEST 6: Startup with 10 bots
    # -------------------------------------------------------------
    print("\n--- TEST 6: Startup with 10 bots ---")
    mock_tokens = [f"token_{i+1}" for i in range(20)]
    pool = BotPool(api_id=12345, api_hash="abcdef", tokens=mock_tokens)

    # Mock _connect_bot to avoid actual Telegram network connection in unit test
    async def mock_connect(bot_id, token):
        class MockClient:
            def is_connected(self):
                return True
            async def disconnect(self):
                pass
        return {
            "id": bot_id,
            "token": token,
            "client": MockClient(),
            "channel": "mock_channel",
            "cooldown_until": 0.0,
            "media_cache": SimpleLRU(500),
            "active": True,
            "active_streams": 0,
        }
    pool._connect_bot = mock_connect

    await pool.init("mock_channel")
    assert len(pool.bots) == 10, f"Expected 10 connected bots at startup, got {len(pool.bots)}"
    assert len(pool.tokens) == 20, f"Expected 20 tokens configured, got {len(pool.tokens)}"
    assert pool.bots[0]["id"] == "bot1"
    assert pool.bots[9]["id"] == "bot10"

    print(f"✓ Test 6 Passed: Exactly 10 bots connected at startup ({pool.bots[0]['id']}..{pool.bots[-1]['id']}). 10 available for dynamic activation.")
    passed += 1

    # -------------------------------------------------------------
    # TEST 7: Dynamic activation up to 20 bots
    # -------------------------------------------------------------
    print("\n--- TEST 7: Dynamic activation up to 20 bots ---")
    # All 10 startup bots are occupied by 10 user sessions
    for i in range(10):
        lease = await pool.acquire_for_session(f"user_session_{i+1}")
        assert lease is not None
        assert lease.bot["id"] == f"bot{i+1}"

    assert len(pool.bots) == 10

    # 11th user connects -> triggers dynamic activation of bot11
    lease11 = await pool.acquire_for_session("user_session_11")
    assert lease11 is not None
    assert lease11.bot["id"] == "bot11"
    assert len(pool.bots) == 11

    # Occupy all up to 20 bots
    for i in range(12, 21):
        l = await pool.acquire_for_session(f"user_session_{i}")
        assert l is not None
        assert l.bot["id"] == f"bot{i}"

    assert len(pool.bots) == 20, f"Expected 20 bots activated, got {len(pool.bots)}"

    # 21st user connects -> all 20 occupied, cannot exceed 20
    lease21 = await pool.acquire_for_session("user_session_21")
    assert lease21 is None, "Pool must return None when all 20 bots are occupied"
    assert len(pool.bots) == 20, "Pool must never activate more than 20 bots"

    print(f"✓ Test 7 Passed: Dynamically activated bots 11-20 upon demand; capped strictly at 20 bots.")
    passed += 1

    # -------------------------------------------------------------
    # TEST 8: FloodWait and bot replacement
    # -------------------------------------------------------------
    print("\n--- TEST 8: FloodWait and bot replacement ---")
    # Free session 1 so bot1 is available
    pool.release_session("user_session_1")
    lease = await pool.acquire_for_session("user_flood_test")
    assert lease is not None
    assert lease.bot["id"] == "bot1"

    # Simulate bot1 hitting FloodWait (60 seconds)
    class MockFloodWait(Exception):
        seconds = 60
    pool.report_error(lease.bot, MockFloodWait())

    assert lease.bot["cooldown_until"] > time.time()
    assert "user_flood_test" not in pool._sessions

    # Next acquire for that user must NOT return bot1; must return another healthy bot or activate if needed
    pool.release_session("user_session_2")  # free bot2
    new_lease = await pool.acquire_for_session("user_flood_test")
    assert new_lease is not None
    assert new_lease.bot["id"] != "bot1", "Must not assign bot that is in cooldown"
    assert new_lease.bot["id"] == "bot2", f"Expected bot2, got {new_lease.bot['id']}"

    print(f"✓ Test 8 Passed: Bot1 placed on 60s cooldown; user seamlessly reassigned to healthy Bot2.")
    passed += 1

    # -------------------------------------------------------------
    # TEST 9: Active stream protection
    # -------------------------------------------------------------
    print("\n--- TEST 9: Active stream protection ---")
    stream_bot = pool.bots[2]  # bot3
    assert stream_bot["active_streams"] == 0

    # Mark stream started on bot3
    pool.mark_stream_start(stream_bot["id"])
    assert stream_bot["active_streams"] == 1
    assert pool.is_stream_active(stream_bot["id"]) is True

    # Call close() while stream is active
    await pool.close()

    # Verify bot3 was protected from disconnection because it is actively streaming
    remaining_ids = [b["id"] for b in pool.bots]
    assert stream_bot["id"] in remaining_ids, f"Bot with active stream was disconnected! Remaining: {remaining_ids}"

    # End stream
    pool.mark_stream_end(stream_bot["id"])
    assert stream_bot["active_streams"] == 0
    assert pool.is_stream_active(stream_bot["id"]) is False

    # Now close disconnects it
    await pool.close()
    assert len(pool.bots) == 0, "Idle bots should now be disconnected"

    print("✓ Test 9 Passed: Bot actively streaming was protected from disconnection.")
    passed += 1

    print("\n" + "=" * 60)
    print(f"ALL {passed}/{total} TESTS PASSED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == "__main__":
    asyncio.run(run_all_tests())
