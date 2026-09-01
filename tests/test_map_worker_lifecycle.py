"""
The renderer is the part that degrades. Chromium is restarted on a schedule, but the
triggers used to be a single 12-hour timer plus `is_connected()` — which only catches a
browser that died outright. A live browser with wedged or OOM-killed renderers kept the
connection open and failed every job it was handed until the timer came round hours
later. These tests drive the decisions that close that gap.

Everything here runs against the real functions with fake browsers and a fake clock. No
test reads the source of what it covers: an assertion about the text of an
implementation passes for code that no longer works and fails for code that still does.
"""

import asyncio
import types

import pytest

import map_worker
from map_worker import BrowserLifecycle


class FakeClock:
    """A monotonic clock the test moves by hand, plus a wall clock that can misbehave."""

    def __init__(self):
        self._monotonic = 1000.0
        self._wall = 1_700_000_000.0

    def monotonic(self):
        return self._monotonic

    def time(self):
        return self._wall

    def advance(self, seconds):
        self._monotonic += seconds
        self._wall += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(map_worker, "time", fake)
    return fake


@pytest.fixture
def idle_memory(monkeypatch):
    """
    A browser tree well under the ceiling. Pinned rather than measured: the real reader
    walks whatever children the test process happens to have, which is not this test's
    subject and not the same on every machine.
    """
    monkeypatch.setattr(map_worker, "child_process_memory_mb", lambda: 300.0)


def rendered(lifecycle, pages=1):
    """Put a browser in the state it reaches after handling `pages` jobs."""
    for _ in range(pages):
        lifecycle.note_page_opened()
        lifecycle.note_render_result(True)
    return lifecycle


# --- scheduled replacement ------------------------------------------------------------


def test_a_fresh_lightly_used_browser_is_left_alone(clock, idle_memory):
    assert rendered(BrowserLifecycle()).scheduled_restart_reason() is None


def test_the_clock_still_triggers_a_restart(clock, idle_memory):
    lifecycle = BrowserLifecycle()

    clock.advance(map_worker.PLAYWRIGHT_RESTART_INTERVAL_SECONDS)

    assert lifecycle.scheduled_restart_reason() is not None


def test_a_wall_clock_jump_neither_forces_nor_postpones_a_restart(clock, idle_memory):
    """An NTP step moves time.time() and leaves time.monotonic() alone."""
    lifecycle = rendered(BrowserLifecycle())

    clock._wall += 10 * map_worker.PLAYWRIGHT_RESTART_INTERVAL_SECONDS
    assert lifecycle.scheduled_restart_reason() is None

    clock._wall -= 20 * map_worker.PLAYWRIGHT_RESTART_INTERVAL_SECONDS
    clock.advance(map_worker.PLAYWRIGHT_RESTART_INTERVAL_SECONDS)
    assert lifecycle.scheduled_restart_reason() is not None


def test_the_page_count_triggers_a_restart_on_its_own(clock, idle_memory):
    """
    Wear tracks renders, not the clock. A backfill drives two renders per job through one
    browser, so thousands of pages can pass through it well inside the twelve hours.
    """
    lifecycle = rendered(BrowserLifecycle(), pages=map_worker.PLAYWRIGHT_RESTART_AFTER_PAGES)

    assert lifecycle.scheduled_restart_reason() is not None


def test_the_memory_ceiling_triggers_a_restart(clock, monkeypatch):
    monkeypatch.setattr(
        map_worker, "child_process_memory_mb",
        lambda: map_worker.PLAYWRIGHT_RESTART_MEMORY_MB + 1,
    )

    assert rendered(BrowserLifecycle()).scheduled_restart_reason() is not None


def test_an_unreadable_process_tree_does_not_force_restarts(clock, monkeypatch):
    """child_process_memory_mb returns None when psutil is missing or denied."""
    monkeypatch.setattr(map_worker, "child_process_memory_mb", lambda: None)

    assert rendered(BrowserLifecycle()).scheduled_restart_reason() is None


def test_the_memory_ceiling_is_read_once_per_rendered_page(clock, monkeypatch):
    """
    Reading it walks the whole Chromium process tree. An idle worker asks every
    POLL_INTERVAL_SECONDS, and a browser that has rendered nothing since the last look
    cannot have grown.
    """
    readings = []
    monkeypatch.setattr(
        map_worker, "child_process_memory_mb", lambda: readings.append(1) or 300.0
    )
    lifecycle = rendered(BrowserLifecycle())

    lifecycle.scheduled_restart_reason()
    lifecycle.scheduled_restart_reason()
    lifecycle.scheduled_restart_reason()
    assert len(readings) == 1

    rendered(lifecycle)
    lifecycle.scheduled_restart_reason()
    assert len(readings) == 2


def test_an_idle_browser_never_walks_the_process_tree(clock, monkeypatch):
    def fail():
        raise AssertionError("walked the process tree on an idle poll")

    monkeypatch.setattr(map_worker, "child_process_memory_mb", fail)

    assert BrowserLifecycle().scheduled_restart_reason() is None


def test_a_replaced_browser_starts_its_counters_over(clock, idle_memory):
    lifecycle = rendered(BrowserLifecycle(), pages=map_worker.PLAYWRIGHT_RESTART_AFTER_PAGES)
    assert lifecycle.scheduled_restart_reason() is not None

    lifecycle.note_launched()

    assert lifecycle.scheduled_restart_reason() is None


# --- replacement because the browser looks broken -------------------------------------


def failing(lifecycle, times):
    for _ in range(times):
        lifecycle.note_page_opened()
        lifecycle.note_render_result(False)
    return lifecycle


def test_a_single_failed_render_is_not_the_browsers_fault(clock):
    lifecycle = failing(BrowserLifecycle(), 1)

    assert lifecycle.fault_restart_reason(is_connected=True, page_close_failed=False) is None


def test_failures_in_a_row_replace_a_still_connected_browser(clock):
    """
    The case is_connected() misses: renderers wedged or OOM-killed while the connection
    holds, so every job runs into its timeouts and nothing ever notices.
    """
    lifecycle = failing(BrowserLifecycle(), map_worker.RENDER_FAILURE_THRESHOLD)

    assert lifecycle.fault_restart_reason(is_connected=True, page_close_failed=False)


def test_a_page_that_will_not_close_counts_even_when_the_render_worked(clock):
    lifecycle = rendered(BrowserLifecycle())

    assert lifecycle.fault_restart_reason(is_connected=True, page_close_failed=True)


def test_one_relaunch_per_run_of_trouble(clock):
    """
    A cause that is not the browser's — missing PMTiles, an unreadable database — would
    otherwise relaunch Chromium on every job from the third failure on.
    """
    lifecycle = failing(BrowserLifecycle(), map_worker.RENDER_FAILURE_THRESHOLD)
    assert lifecycle.fault_restart_reason(is_connected=True, page_close_failed=False)
    lifecycle.note_fault_restart()

    failing(lifecycle, 1)
    assert lifecycle.fault_restart_reason(is_connected=True, page_close_failed=False) is None
    assert lifecycle.fault_restart_reason(is_connected=True, page_close_failed=True) is None


def test_a_disconnected_browser_is_replaced_even_after_a_relaunch(clock):
    """Without a live browser there is nothing left to render with."""
    lifecycle = failing(BrowserLifecycle(), map_worker.RENDER_FAILURE_THRESHOLD)
    lifecycle.note_fault_restart()

    assert lifecycle.fault_restart_reason(is_connected=False, page_close_failed=False)


def test_a_successful_render_buys_the_next_run_of_trouble_a_relaunch(clock):
    lifecycle = failing(BrowserLifecycle(), map_worker.RENDER_FAILURE_THRESHOLD)
    lifecycle.note_fault_restart()

    rendered(lifecycle)
    failing(lifecycle, map_worker.RENDER_FAILURE_THRESHOLD)

    assert lifecycle.fault_restart_reason(is_connected=True, page_close_failed=False)


# --- the bottom rung: stop claiming jobs ----------------------------------------------


def test_the_worker_keeps_going_while_a_relaunch_might_still_help(clock):
    lifecycle = failing(BrowserLifecycle(), map_worker.RENDER_FAILURE_GIVE_UP_THRESHOLD - 1)

    assert lifecycle.give_up_reason() is None


def test_the_worker_gives_up_inside_the_stated_time_budget(clock):
    """
    The rung is a budget, not a count: what matters is how long a stranded queue stays
    stranded. Walking the ladder the way the loop does — a job that burns the render
    timeout, then the wait it earns — has to reach the exit inside that budget.
    """
    lifecycle = BrowserLifecycle()
    spent = 0
    while lifecycle.give_up_reason() is None:
        failing(lifecycle, 1)
        spent += map_worker.JOB_RENDER_TIMEOUT_SECONDS + lifecycle.failure_delay_seconds()
        assert spent <= map_worker.RENDER_FAILURE_GIVE_UP_SECONDS, (
            "the queue stays stranded longer than the budget allows"
        )

    assert spent <= map_worker.RENDER_FAILURE_GIVE_UP_SECONDS


def test_the_worker_does_not_give_up_before_trying_a_relaunch(clock):
    """The step most likely to help would otherwise be skipped on a tight budget."""
    lifecycle = failing(BrowserLifecycle(), map_worker.RENDER_FAILURE_THRESHOLD)

    assert lifecycle.fault_restart_reason(is_connected=True, page_close_failed=False)
    assert lifecycle.give_up_reason() is None


def test_the_worker_gives_up_once_replacing_the_browser_has_not_helped(clock):
    """
    Every other trigger can be starved here: the relaunch for this run of trouble is
    spent, new_page() times out before anything renders so the page count stalls, and
    with it the memory ceiling. Without this rung the only way out is the twelve hour
    clock, spent claiming jobs and leaving them in 'processing'.
    """
    lifecycle = failing(BrowserLifecycle(), map_worker.RENDER_FAILURE_GIVE_UP_THRESHOLD)

    assert lifecycle.give_up_reason()


def test_a_relaunch_does_not_reset_the_failure_ladder(clock):
    """Otherwise a fault that survives the relaunch could never reach the bottom rung."""
    lifecycle = failing(BrowserLifecycle(), map_worker.RENDER_FAILURE_THRESHOLD)
    lifecycle.note_fault_restart()

    failing(lifecycle, map_worker.RENDER_FAILURE_GIVE_UP_THRESHOLD
            - map_worker.RENDER_FAILURE_THRESHOLD)

    assert lifecycle.give_up_reason()


def test_a_successful_render_clears_the_ladder(clock):
    lifecycle = failing(BrowserLifecycle(), map_worker.RENDER_FAILURE_GIVE_UP_THRESHOLD - 1)

    rendered(lifecycle)

    assert lifecycle.give_up_reason() is None
    assert lifecycle.failure_delay_seconds() == map_worker.RENDER_FAILURE_DELAY_SECONDS


def test_the_failure_wait_grows_once_the_cause_looks_systemic(clock):
    lifecycle = failing(BrowserLifecycle(), 1)
    assert lifecycle.failure_delay_seconds() == map_worker.RENDER_FAILURE_DELAY_SECONDS

    failing(lifecycle, map_worker.RENDER_FAILURE_THRESHOLD - 1)

    assert lifecycle.failure_delay_seconds() == map_worker.RENDER_FAILURE_BACKOFF_SECONDS


# --- every browser call a job makes is bounded ----------------------------------------


class HangingPage:
    """A page whose close() never returns, the way a wedged browser leaves one."""

    def __init__(self, close_hangs=False, close_raises=None):
        self.close_hangs = close_hangs
        self.close_raises = close_raises
        self.closed = False

    def on(self, event, handler):
        pass

    async def close(self):
        if self.close_hangs:
            await asyncio.Event().wait()
        if self.close_raises:
            raise self.close_raises
        self.closed = True


class FakeBrowser:
    def __init__(self, page=None, new_page_hangs=False, connected=True):
        self.page = page or HangingPage()
        self.new_page_hangs = new_page_hangs
        self.connected = connected

    async def new_page(self):
        if self.new_page_hangs:
            await asyncio.Event().wait()
        return self.page

    async def close(self):
        self.connected = False

    def is_connected(self):
        return self.connected


@pytest.fixture
def short_timeouts(monkeypatch):
    """The real ceilings are minutes; the behaviour under them is what is being tested."""
    monkeypatch.setattr(map_worker, "PAGE_OPEN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(map_worker, "JOB_RENDER_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(map_worker, "PAGE_CLOSE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(map_worker, "BROWSER_CLOSE_TIMEOUT_SECONDS", 0.05)


def render_returning(result=None, hangs=False):
    async def fake_render(page, job_id, server_url):
        if hangs:
            await asyncio.Event().wait()
        if isinstance(result, Exception):
            raise result

    return fake_render


async def test_a_healthy_job_renders_and_closes_its_page(monkeypatch, short_timeouts):
    monkeypatch.setattr(map_worker, "render_map_with_playwright", render_returning())
    browser = FakeBrowser()
    lifecycle = BrowserLifecycle()

    ok, close_failed = await map_worker._render_job(browser, "trip", "url", lifecycle, True)

    assert (ok, close_failed) == (True, False)
    assert browser.page.closed
    assert lifecycle.pages_rendered == 1


async def test_a_browser_that_will_not_hand_out_a_page_does_not_park_the_worker(
    monkeypatch, short_timeouts
):
    monkeypatch.setattr(map_worker, "render_map_with_playwright", render_returning())
    lifecycle = BrowserLifecycle()

    ok, _ = await asyncio.wait_for(
        map_worker._render_job(FakeBrowser(new_page_hangs=True), "trip", "url", lifecycle, True),
        timeout=5,
    )

    assert ok is False
    # Counted on the attempt: a browser this wedged never reaches a successful render,
    # so a count that only tracked successes would freeze and starve the page trigger.
    assert lifecycle.pages_rendered == 1


async def test_a_renderer_that_never_settles_does_not_park_the_worker(
    monkeypatch, short_timeouts
):
    """
    page.evaluate() carries no timeout and Playwright's default does not cover it — it
    simply waits for the browser-side promise. The restart logic only ever runs on a job
    that ends.
    """
    monkeypatch.setattr(
        map_worker, "render_map_with_playwright", render_returning(hangs=True)
    )
    browser = FakeBrowser()

    ok, _ = await asyncio.wait_for(
        map_worker._render_job(browser, "trip", "url", BrowserLifecycle(), True), timeout=5
    )

    assert ok is False
    assert browser.page.closed, "the page has to be released even when the render is cut off"


async def test_a_page_that_will_not_close_is_reported_not_awaited(
    monkeypatch, short_timeouts
):
    """page.close() rides the same connection the render just failed on."""
    monkeypatch.setattr(map_worker, "render_map_with_playwright", render_returning())
    browser = FakeBrowser(page=HangingPage(close_hangs=True))

    ok, close_failed = await asyncio.wait_for(
        map_worker._render_job(browser, "trip", "url", BrowserLifecycle(), True), timeout=5
    )

    assert (ok, close_failed) == (True, True)


async def test_a_failing_render_does_not_escape_the_job(monkeypatch, short_timeouts):
    """The loop decides what a failure means; it must not be knocked out by one."""
    monkeypatch.setattr(
        map_worker, "render_map_with_playwright",
        render_returning(RuntimeError("renderer blew up")),
    )
    browser = FakeBrowser()

    ok, close_failed = await map_worker._render_job(
        browser, "trip", "url", BrowserLifecycle(), True
    )

    assert (ok, close_failed) == (False, False)
    assert browser.page.closed


async def test_a_hanging_tile_server_does_not_park_the_staticmap_fallback(
    monkeypatch, short_timeouts
):
    """
    The fallback has no browser to replace, so the job ceiling is all there is. Its
    render is a blocking `requests` call staticmap3 leaves untimed by default.
    """
    async def hangs(job_id):
        await asyncio.Event().wait()

    monkeypatch.setattr(map_worker, "render_map_with_staticmap3", hangs)

    ok, close_failed = await asyncio.wait_for(
        map_worker._render_job(None, "trip", None, BrowserLifecycle(), False), timeout=5
    )

    assert (ok, close_failed) == (False, False)


def test_every_tile_request_carries_a_timeout(monkeypatch, tmp_path):
    """
    The job ceiling abandons a slow render rather than stopping its thread, so without
    this the thread outlives the job it belonged to.
    """
    captured = {}

    class FakeStaticMap:
        def __init__(self, width, height, **kwargs):
            captured.update(kwargs)

        def add_line(self, line):
            pass

        def add_marker(self, marker):
            pass

        def render(self, zoom=None):
            return types.SimpleNamespace(save=lambda path: None)

    monkeypatch.setattr(map_worker, "StaticMap", FakeStaticMap)
    monkeypatch.setattr(map_worker, "Line", lambda *a, **k: None)
    monkeypatch.setattr(map_worker, "CircleMarker", lambda *a, **k: None)
    monkeypatch.setattr(map_worker.settings, "KARTO_TILE_URL_TEMPLATE", "http://tiles/{z}/{x}/{y}.png")
    monkeypatch.setattr(map_worker.settings, "MAPS_STORAGE_PATH", tmp_path)

    trip = types.SimpleNamespace(
        id="trip-1", vehicle_id="CAR",
        geojson='{"coordinates": [[8.0, 50.0], [8.1, 50.1]]}',
    )
    monkeypatch.setattr(
        map_worker, "database",
        types.SimpleNamespace(SessionLocal=lambda: RecordingSession([])),
    )
    monkeypatch.setattr(
        map_worker, "crud",
        types.SimpleNamespace(
            get_trip_with_points=lambda db, trip_id: trip,
            update_trip_map_path=lambda *a, **k: None,
        ),
    )

    asyncio.run(map_worker.render_map_with_staticmap3("trip-1"))

    assert captured["tile_request_timeout"] == map_worker.TILE_REQUEST_TIMEOUT_SECONDS


async def test_a_playwright_driver_that_will_not_start_does_not_park_the_restart(
    monkeypatch, short_timeouts
):
    """
    The last-resort branch of the restart path: the browser is already gone, and a
    driver that never answers would strand the worker exactly where it was recovering.
    """
    monkeypatch.setattr(map_worker, "PLAYWRIGHT_START_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(map_worker, "BROWSER_RESTART_RETRY_DELAY_SECONDS", 0)

    class HangingDriver:
        async def start(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(map_worker, "async_playwright", HangingDriver)

    async def launch_fails():
        raise RuntimeError("no browser")

    dead_context = types.SimpleNamespace(
        chromium=types.SimpleNamespace(launch=launch_fails),
        stop=_returning(None),
    )

    browser, ctx = await asyncio.wait_for(
        map_worker._restart_browser(FakeBrowser(), dead_context), timeout=5
    )

    assert (browser, ctx) == (None, None)


async def test_tearing_down_a_wedged_browser_is_bounded(short_timeouts):
    class WedgedBrowser:
        async def close(self):
            await asyncio.Event().wait()

    await asyncio.wait_for(
        map_worker._close_quietly(WedgedBrowser().close(), "Closing"), timeout=5
    )


async def test_a_teardown_error_is_not_fatal(short_timeouts):
    async def raises():
        raise RuntimeError("driver gone")

    await map_worker._close_quietly(raises(), "Closing")


async def test_the_restart_path_survives_a_browser_that_will_not_close(short_timeouts):
    """
    The restart is entered because the browser is suspected of being wedged, so its
    close() is the least trustworthy call in the worker.
    """
    class WedgedBrowser:
        async def close(self):
            await asyncio.Event().wait()

    replacement = FakeBrowser()
    context = types.SimpleNamespace(
        chromium=types.SimpleNamespace(launch=_returning(replacement))
    )

    browser, ctx = await asyncio.wait_for(
        map_worker._restart_browser(WedgedBrowser(), context), timeout=5
    )

    assert browser is replacement
    assert ctx is context


def _returning(value):
    async def launch():
        return value

    return launch


# --- a finished job is off the queue before the browser is touched --------------------


class RecordingSession:
    def __init__(self, log):
        self.log = log

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def booking_log(monkeypatch):
    log = []
    monkeypatch.setattr(
        map_worker, "database",
        types.SimpleNamespace(SessionLocal=lambda: RecordingSession(log)),
    )
    monkeypatch.setattr(
        map_worker, "crud",
        types.SimpleNamespace(
            complete_map_generation_job=lambda db, job_id: log.append(job_id),
            count_pending_map_jobs=lambda db: 0,
        ),
    )
    return log


async def test_a_rendered_trip_is_booked_before_the_caller_can_restart_anything(
    monkeypatch, short_timeouts, booking_log
):
    """
    The previews are already on disk by then, and a restart that cannot bring a browser
    back exits the worker — leaving the row in 'processing' until the stale-job timeout
    hands it to someone who renders the whole trip again.
    """
    monkeypatch.setattr(map_worker, "render_map_with_playwright", render_returning())

    ok, _ = await map_worker._process_job(
        FakeBrowser(), "trip-42", "url", BrowserLifecycle(), True
    )

    assert ok is True
    assert booking_log == ["trip-42"]


async def test_a_failed_trip_stays_on_the_queue(monkeypatch, short_timeouts, booking_log):
    monkeypatch.setattr(
        map_worker, "render_map_with_playwright", render_returning(RuntimeError("nope"))
    )
    lifecycle = BrowserLifecycle()

    ok, _ = await map_worker._process_job(FakeBrowser(), "trip-42", "url", lifecycle, True)

    assert ok is False
    assert booking_log == []
    assert lifecycle.consecutive_render_failures == 1
