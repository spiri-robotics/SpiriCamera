"""Tests for the NiceGUI test UI's server-side pieces."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import cv2
import numpy as np
import pytest
from nicegui import ui as nicegui_ui
from nicegui.testing.user import User

from SpiriCamera import exif
from SpiriCamera import ui as camera_ui
from SpiriCamera.camera import Camera
from SpiriCamera.overlay import HudWidget


@pytest.fixture(autouse=True)
def fresh_meter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test its own meter and de-duplication state.

    Both are process-wide, matching the one-camera-per-process model, so
    without this a test reads whatever frames earlier tests happened to
    serve and the order of the file decides whether it passes.
    """
    monkeypatch.setattr(camera_ui, "frame_meter", camera_ui.RateMeter())
    monkeypatch.setattr(camera_ui, "_last_counted", None)


@pytest.fixture(autouse=True)
def fresh_ui_widgets() -> Iterator[None]:
    """Close and forget every widget the overlay panel created.

    ``_ui_widgets`` is process-wide, matching ``_camera`` -- left alone,
    one test's widgets would leak their zenoh resources into the next.
    """
    yield
    for widget in camera_ui._ui_widgets.values():
        widget.close()
    camera_ui._ui_widgets.clear()


class TestRateMeter:
    """The bandwidth meter behind the readout under the image."""

    def test_starts_empty(self) -> None:
        """Nothing served means no rate, not a division by zero."""
        assert camera_ui.RateMeter().rates() == (0.0, 0.0)

    def test_single_sample_is_not_a_rate(self) -> None:
        """One frame gives no interval to measure over."""
        meter = camera_ui.RateMeter()
        meter.record(1000)
        assert meter.rates() == (0.0, 0.0)

    def test_measures_frames_and_bytes(self) -> None:
        """Both rates reflect what was recorded."""
        meter = camera_ui.RateMeter(window=5.0)
        for _ in range(10):
            meter.record(1000)
            time.sleep(0.01)

        frames_per_second, bytes_per_second = meter.rates()

        assert frames_per_second > 0
        assert bytes_per_second == pytest.approx(frames_per_second * 1000, rel=0.01)

    def test_forgets_old_samples(self) -> None:
        """The window rolls, so a stopped stream decays to zero."""
        meter = camera_ui.RateMeter(window=0.05)
        meter.record(1000)
        meter.record(1000)
        time.sleep(0.1)
        assert meter.rates() == (0.0, 0.0)


class TestFrameRoute:
    """The HTTP route the browser pulls frames from."""

    @pytest.fixture
    def served_camera(self, monkeypatch: pytest.MonkeyPatch) -> Camera:
        """Install a known camera as the process-wide one."""
        cam = Camera(
            "testimage://", max_width=160, max_height=120, synq_auto_start=False
        )
        monkeypatch.setattr(camera_ui, "_camera", cam)
        yield cam
        cam.stop()

    def test_placeholder_before_the_first_frame(self, served_camera: Camera) -> None:
        """A stopped camera serves a placeholder, not an error."""
        response = camera_ui.serve_frame()
        assert response.status_code == 200
        assert response.media_type == "image/png"

    def test_serves_the_current_frame(self, served_camera: Camera) -> None:
        """Once running, the route hands out the latest encoded frame."""
        served_camera.start(background=False)
        served_camera.read()

        response = camera_ui.serve_frame()

        assert response.body == served_camera.image
        assert response.media_type == "image/jpeg"

    def test_frames_are_not_cached(self, served_camera: Camera) -> None:
        """A stale frame must never be reused by a proxy."""
        served_camera.start(background=False)
        served_camera.read()

        assert camera_ui.serve_frame().headers["Cache-Control"] == "no-store"

    def test_serving_feeds_the_meter(self, served_camera: Camera) -> None:
        """The readout measures real bytes leaving the route."""
        served_camera.start(background=False)

        for _ in range(3):
            served_camera.read()
            camera_ui.serve_frame()
            time.sleep(0.01)

        frames_per_second, bytes_per_second = camera_ui.frame_meter.rates()
        assert frames_per_second > 0
        assert bytes_per_second > 0


class TestSharedCamera:
    """One camera per process, not one per page load."""

    def test_returns_the_same_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Repeated calls share a camera, so the synq topic is unique."""
        cam = Camera("testimage://", synq_auto_start=False)
        monkeypatch.setattr(camera_ui, "_camera", cam)

        assert camera_ui.get_camera() is cam
        assert camera_ui.get_camera() is camera_ui.get_camera()

    def test_the_real_camera_is_authoritative(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """This process runs the device; its topic must say so.

        A non-authoritative camera never prefixes its topic with this
        machine's name and never registers its RPCs -- exactly wrong for
        the one camera this page actually captures with.
        """
        monkeypatch.setattr(camera_ui, "_camera", None)

        cam = camera_ui.get_camera()

        assert cam.synq_authoritive is True


class TestSourceOptions:
    """The known-sources picker's contents."""

    def test_includes_bundled_test_patterns(self) -> None:
        """Every shipped test pattern is offered, not just the default."""
        options = camera_ui._source_options()

        assert "testimage://pm5544" in options

    def test_includes_local_v4l_devices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A plugged-in USB camera shows up alongside the test patterns."""
        monkeypatch.setattr(
            camera_ui,
            "list_devices",
            lambda: [{"path": "/dev/video0", "label": "Some Webcam (/dev/video0)"}],
        )

        options = camera_ui._source_options()

        assert options["/dev/video0"] == "Some Webcam (/dev/video0)"


class TestModeSwitching:
    """Toggling between running a device ourselves and mirroring one."""

    @pytest.fixture(autouse=True)
    def clean_up(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        """Every test starts from no camera and closes whatever it built.

        ``monkeypatch`` only restores the ``_camera`` attribute; it does
        not close the zenoh resources a real ``Camera`` opened, so that
        is done by hand.
        """
        monkeypatch.setattr(camera_ui, "_camera", None)
        yield
        if camera_ui._camera is not None:
            camera_ui._camera.close()

    def test_set_authoritive_builds_a_real_camera(self) -> None:
        """Switching to authoritative mode makes an ordinary camera."""
        cam = camera_ui.set_authoritive("testimage://")

        assert cam.synq_authoritive is True
        assert cam is camera_ui.get_camera()

    def test_set_authoritive_closes_the_camera_it_replaces(self) -> None:
        """The old camera must not keep running in the background."""
        old = camera_ui.set_authoritive("testimage://")
        old.start(background=False)

        camera_ui.set_authoritive("testimage://pm5544")

        assert old.running is False

    def test_set_mirror_with_no_topic_is_inert(self) -> None:
        """Dropping into mirror mode without picking one shows nothing."""
        cam = camera_ui.set_mirror()

        assert cam.synq_authoritive is False
        assert cam.image == b""

    def test_set_mirror_fetches_the_given_topic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A topic (typed by hand or picked from discovery) is mirrored.

        ``Camera.from_topic`` performs a real RPC round trip over zenoh;
        that path is exercised in ``test_camera.py``'s rehydrate tests.
        Here it is stubbed so this test stays a fast, hermetic check of
        the wiring in :py:func:`SpiriCamera.ui.set_mirror`.
        """
        built = Camera("testimage://", synq_auto_start=False)
        monkeypatch.setattr(Camera, "from_topic", classmethod(lambda cls, topic: built))

        cam = camera_ui.set_mirror("some/topic")

        assert cam is built
        assert cam is camera_ui.get_camera()

    async def test_toggling_off_shows_the_discovery_panel(self, user: User) -> None:
        """Switching off authoritative mode replaces the device controls."""
        await user.open("/")
        await user.should_see("Start")

        user.find(kind=nicegui_ui.switch).click()

        await user.should_see("Cameras on the network")
        await user.should_not_see("Start")

    async def test_toggling_back_on_restores_device_controls(self, user: User) -> None:
        """The device controls come back when authoritative mode returns.

        The switch is re-found after the first click rather than reused:
        toggling mode rebuilds the whole panel the switch lives in, so
        the element from before that rebuild no longer has a parent.
        """
        await user.open("/")

        user.find(kind=nicegui_ui.switch).click()
        await user.should_see("Cameras on the network")
        user.find(kind=nicegui_ui.switch).click()

        await user.should_see("Start")
        await user.should_not_see("Cameras on the network")

    async def test_typing_a_topic_and_clicking_mirror(
        self, monkeypatch: pytest.MonkeyPatch, user: User
    ) -> None:
        """A hand-typed topic is not limited to what discovery found."""
        built = Camera("testimage://", synq_auto_start=False)
        monkeypatch.setattr(Camera, "from_topic", classmethod(lambda cls, topic: built))

        await user.open("/")
        user.find(kind=nicegui_ui.switch).click()
        await user.should_see("Cameras on the network")

        topic_field = next(
            element
            for element in user.find(kind=nicegui_ui.input).elements
            if element.props.get("label") == "Topic"
        )
        topic_field.value = "otherhost/spiricamera_testimage"

        user.find("Mirror").click()

        assert camera_ui.get_camera() is built


class TestOverlayWidgets:
    """Creating, listing, and removing HudWidgets from the debug UI."""

    def _authoritative_camera(self) -> Camera:
        cam = Camera("testimage://", synq_auto_start=False)
        cam.synq_authoritive = True
        cam.sync()
        return cam

    def test_create_overlay_widget_adds_it_to_overlay_widgets(self) -> None:
        """The new widget's topic lands in overlay_widgets, not just the
        widget itself getting published."""
        cam = self._authoritative_camera()

        widget = camera_ui.create_overlay_widget(cam, "My Widget!")

        assert widget.synq_absolute_path in camera_ui._overlay_topic_list(cam)
        cam.stop()

    def test_create_overlay_widget_slugifies_the_name(self) -> None:
        """A human-typed name becomes a topic-safe slug, same idea as
        topic_for_source for a camera's own source string."""
        cam = self._authoritative_camera()

        widget = camera_ui.create_overlay_widget(cam, "My Widget!")

        assert widget.synq_topic == "ui_widgets/my_widget"
        cam.stop()

    def test_add_metrics_widget_adds_it_to_overlay_widgets(self) -> None:
        cam = self._authoritative_camera()

        widget = camera_ui.add_metrics_widget(cam)

        assert widget.synq_absolute_path in camera_ui._overlay_topic_list(cam)
        cam.stop()

    def test_remove_overlay_widget_closes_a_ui_created_widget(self) -> None:
        """This page owns what it creates, so removing one really
        releases its zenoh resources rather than just forgetting it."""
        cam = self._authoritative_camera()
        widget = camera_ui.create_overlay_widget(cam, "temp")

        camera_ui.remove_overlay_widget(cam, widget.synq_absolute_path)

        assert widget.synq_absolute_path not in camera_ui._overlay_topic_list(cam)
        assert widget.synq_is_deleted is True
        cam.stop()

    def test_remove_overlay_widget_only_detaches_a_foreign_one(self) -> None:
        """A widget this page did not create -- one typed in or added
        from discovery -- is never closed out from under whoever does
        own it; it is only detached from overlay_widgets."""
        cam = self._authoritative_camera()
        foreign = HudWidget(synq_topic="someone_elses_widget", synq_authoritive=True)
        cam.overlay_widgets[foreign.synq_absolute_path] = {}

        camera_ui.remove_overlay_widget(cam, foreign.synq_absolute_path)

        assert foreign.synq_absolute_path not in camera_ui._overlay_topic_list(cam)
        assert foreign.synq_is_deleted is False
        foreign.close()
        cam.stop()

    def test_overlay_widgets_keeps_other_entries(self) -> None:
        """Adding or removing one widget leaves the others alone."""
        cam = self._authoritative_camera()
        cam.overlay_widgets["some/other/widget"] = {}

        widget = camera_ui.create_overlay_widget(cam, "temp")
        assert set(camera_ui._overlay_topic_list(cam)) == {
            "some/other/widget",
            widget.synq_absolute_path,
        }

        camera_ui.remove_overlay_widget(cam, widget.synq_absolute_path)
        assert camera_ui._overlay_topic_list(cam) == ["some/other/widget"]
        cam.stop()

    async def test_overlay_panel_shows_in_the_page(self, user: User) -> None:
        await user.open("/")

        await user.should_see("Overlays")
        await user.should_see("Add CameraMetrics")

    async def test_creating_a_widget_from_the_page(self, user: User) -> None:
        """The end-to-end path: type a name, click Create, see it listed."""
        await user.open("/")

        name_field = next(
            element
            for element in user.find(kind=nicegui_ui.input).elements
            if element.props.get("label") == "New widget name"
        )
        name_field.value = "demo"

        user.find("Create & Add").click()

        await user.should_see("ui_widgets/demo")


class TestPage:
    """Building the page itself."""

    async def test_page_builds(self, user: User) -> None:
        """Every binding resolves against the current camera fields.

        This is the regression guard for binding a control to a field
        that no longer exists: NiceGUI raises at build time, so simply
        rendering the page is the test.
        """
        await user.open("/")
        await user.should_see("Start")
        await user.should_see("Resolved Source")

    async def test_shows_the_resolved_source(self, user: User) -> None:
        """The source panel reflects the camera's parsed source."""
        await user.open("/")
        await user.should_see("testimage")

    async def test_shows_the_bandwidth_readout(self, user: User) -> None:
        """The meter renders even before anything has been served."""
        await user.open("/")
        await user.should_see("0.0 fps")


class TestTagDisplay:
    """Rendering the tags read off the current frame."""

    def test_untagged_says_so(self) -> None:
        """An empty display would read as a broken binding."""
        assert camera_ui._format_tags({}) == "untagged"

    def test_one_tag_per_line_sorted(self) -> None:
        """Sorted, so a tag does not move around between frames."""
        rendered = camera_ui._format_tags({"zulu": "1", "alpha": "2"})

        assert rendered == "alpha: 2\nzulu: 1"


class TestExtraTagEntry:
    """Parsing the Extra Tags field onto the camera."""

    @pytest.fixture
    def cam(self) -> Camera:
        """A camera that touches neither a device nor the network."""
        return Camera(
            "testimage://", max_width=160, max_height=120, synq_auto_start=False
        )

    def _tagged(self, cam: Camera) -> dict[str, str]:
        """The tags that would land on the next frame."""
        cam.start(background=False)
        cam.read()
        return cam.exif_tags

    def test_parses_pairs(self, cam: Camera) -> None:
        """Comma-separated name=value, whitespace forgiven."""
        camera_ui._apply_extra_tags(cam, " mission=probe-1, operator = alex ")

        tags = self._tagged(cam)
        assert tags["mission"] == "probe-1"
        assert tags["operator"] == "alex"

    def test_ignores_a_fragment_without_a_separator(self, cam: Camera) -> None:
        """Typing is debounced, not atomic; half an entry is not a tag."""
        camera_ui._apply_extra_tags(cam, "mission=probe-1, operat")

        tags = self._tagged(cam)
        assert tags["mission"] == "probe-1"
        assert "operat" not in tags

    def test_empty_field_clears(self, cam: Camera) -> None:
        """Deleting the text is how the tags are removed."""
        camera_ui._apply_extra_tags(cam, "mission=probe-1")
        camera_ui._apply_extra_tags(cam, "")

        assert "mission" not in self._tagged(cam)
        assert camera_ui._UI_EXIF_PROVIDER not in cam.exif_tag_providers()

    def test_an_empty_value_is_kept(self, cam: Camera) -> None:
        """``name=`` is a tag being typed, not a tag being dropped.

        exif_set_tags (unlike exif_update) keeps an empty value rather
        than filtering it, precisely so a still-being-typed entry is
        visible and a deliberate blank can suppress a built-in tag.
        """
        camera_ui._apply_extra_tags(cam, "mission=")

        assert cam.exif_tags_for_frame(None).get("mission", "") == ""

    def test_does_not_touch_another_providers_tags(self, cam: Camera) -> None:
        """The debug box only ever edits its own bucket."""
        cam.exif_set_tags("logger", {"build": "42"})

        camera_ui._apply_extra_tags(cam, "mission=probe-1")

        assert self._tagged(cam)["build"] == "42"


class TestFrameAge:
    """Capture-to-serve age, measured where frames leave."""

    @staticmethod
    def _jpeg(**tags: str) -> bytes:
        """A tiny JPEG carrying the given tags."""
        raw = cv2.imencode(".jpg", np.zeros((4, 4, 3), np.uint8))[1].tobytes()
        return exif.embed(raw, tags) if tags else raw

    def test_untagged_frames_have_no_knowable_capture_time(self) -> None:
        """Absent a timestamp, zero means unknown, not the epoch."""
        assert camera_ui._frame_timestamp(self._jpeg()) == 0.0

    def test_read_from_the_frame(self) -> None:
        """The capture time is whatever the frame's own EXIF says."""
        captured = time.time() - 0.25

        assert camera_ui._frame_timestamp(
            self._jpeg(timestamp=f"{captured:.6f}")
        ) == pytest.approx(captured)

    def test_an_unreadable_timestamp_is_not_fatal(self) -> None:
        """A tag is free-form text and may hold anything at all."""
        assert camera_ui._frame_timestamp(self._jpeg(timestamp="soon")) == 0.0

    def test_a_clock_ahead_of_ours_is_clamped(self) -> None:
        """A remote camera must not show a frame from the future."""
        meter = camera_ui.RateMeter()
        meter.record(1000, time.time() + 60)

        assert meter.age() == 0.0

    def test_an_untagged_frame_has_no_age_at_all(self) -> None:
        """Nothing to measure against means no reading, not zero seconds."""
        meter = camera_ui.RateMeter()
        meter.record(1000)

        assert meter.age() == 0.0

    def test_the_age_keeps_counting_up_after_frames_stop(self) -> None:
        """The picture on screen really is getting older.

        The window mean decays to nothing once nothing is arriving, so on
        its own it would claim a long-stopped camera was showing a fresh
        frame.
        """
        meter = camera_ui.RateMeter(window=0.2)
        meter.record(1000, time.time() - 5)

        first = meter.age()
        time.sleep(0.3)
        second = meter.age()

        assert first >= 5
        assert second > first

    def test_the_age_never_understates_the_frame_on_screen(self) -> None:
        """Whichever reading is larger wins; neither may hide the other."""
        meter = camera_ui.RateMeter(window=5.0)
        meter.record(1000, time.time() - 10)

        assert meter.age() == pytest.approx(10, abs=0.5)

    def test_the_meter_averages_rather_than_samples(self) -> None:
        """The reported age must not depend on when it is read.

        A frame's age sweeps across a whole frame interval while it waits
        to be fetched, so a single reading beats against the capture loop
        and slides instead of settling.
        """
        meter = camera_ui.RateMeter(window=5.0)
        now = time.time()
        for age in (0.010, 0.020, 0.030):
            meter.record(1000, now - age)

        assert meter.mean_age() == pytest.approx(0.020, abs=0.005)

    def test_untagged_frames_do_not_drag_the_mean_down(self) -> None:
        """An unknown age is excluded, not counted as zero."""
        meter = camera_ui.RateMeter(window=5.0)
        meter.record(1000, time.time() - 0.020)
        meter.record(1000)

        assert meter.mean_age() == pytest.approx(0.020, abs=0.005)

    def test_nothing_served_has_no_age(self) -> None:
        """No frames means no reading, not a division by zero."""
        assert camera_ui.RateMeter().mean_age() == 0.0
        assert camera_ui.RateMeter().age() == 0.0

    @pytest.mark.parametrize(
        ("seconds", "rendered"),
        [
            (0.026, "26 ms old"),
            (0.9994, "999 ms old"),
            (4.25, "4.2 s old"),
            (59.9, "59.9 s old"),
            (61, "1 min old"),
            (3600, "60 min old"),
        ],
    )
    def test_age_is_rendered_at_a_readable_resolution(
        self, seconds: float, rendered: str
    ) -> None:
        """A stopped camera ages out of milliseconds fairly quickly."""
        assert camera_ui._format_age(seconds) == rendered

    async def test_shown_once_a_frame_has_been_served(self, user: User) -> None:
        """The readout appears alongside the bandwidth figures."""
        cam = camera_ui.get_camera()
        cam.start(background=False)
        cam.read()
        camera_ui.serve_frame()
        camera_ui.serve_frame()

        await user.open("/")

        await user.should_see("ms old")


class TestStaleFrames:
    """A camera that has stopped must not read as a busy one."""

    def test_concurrent_polls_of_one_frame_count_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two tabs polling at once must not double-count a single frame.

        The check-and-set on ``_last_counted`` is a classic race: without
        a lock, two threads can both read the old value before either
        writes the new one, and both decide the frame is new.
        """
        cam = Camera(
            "testimage://", max_width=160, max_height=120, synq_auto_start=False
        )
        monkeypatch.setattr(camera_ui, "_camera", cam)
        cam.start(background=False)
        cam.read()

        barrier = threading.Barrier(2)

        def poll() -> None:
            barrier.wait(timeout=2)
            camera_ui.serve_frame()

        threads = [threading.Thread(target=poll) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        frames_per_second, _ = camera_ui.frame_meter.rates()
        # rates() needs >=2 samples to report anything at all; a double
        # count would still show as exactly one sample either way, so
        # assert on the sample count directly instead.
        assert len(camera_ui.frame_meter._samples) == 1

    def test_re_serving_one_frame_counts_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Polling a stopped camera hands out the same frame repeatedly.

        Counting each response would report a busy framerate for a camera
        that has produced nothing since it was stopped.
        """
        cam = Camera(
            "testimage://", max_width=160, max_height=120, synq_auto_start=False
        )
        monkeypatch.setattr(camera_ui, "_camera", cam)
        cam.start(background=False)
        cam.read()
        cam.stop()

        for _ in range(20):
            camera_ui.serve_frame()

        assert camera_ui.frame_meter.rates() == (0.0, 0.0)

    def test_distinct_frames_are_all_counted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """De-duplication is by identity, so real frames still count."""
        cam = Camera(
            "testimage://", max_width=160, max_height=120, synq_auto_start=False
        )
        monkeypatch.setattr(camera_ui, "_camera", cam)
        cam.start(background=False)

        for _ in range(5):
            cam.read()
            camera_ui.serve_frame()
            time.sleep(0.01)

        frames_per_second, bytes_per_second = camera_ui.frame_meter.rates()
        assert frames_per_second > 0
        assert bytes_per_second > 0
        cam.stop()

    async def test_the_readout_reports_zero_not_idle(self, user: User) -> None:
        """Rates fall to zero; the last frame's own figures stay put."""
        cam = camera_ui.get_camera()
        cam.start(background=False)
        cam.read()
        cam.stop()

        await user.open("/")

        # Rolling averages, so they decay to nothing.
        await user.should_see("0 KiB/s")
        await user.should_see("0.0 fps")
        # An account of the last frame, which is still exactly this big.
        await user.should_see("KiB/frame")
        await user.should_see("160x120")
