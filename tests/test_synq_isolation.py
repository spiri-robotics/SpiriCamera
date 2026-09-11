"""Guards on the test suite's own zenoh isolation.

These assert on the fixtures rather than on SpiriCamera.  They exist
because the failure they catch is silent and remote: a suite that has
quietly rejoined the network writes over a real camera's topics on
whatever LAN the developer happens to be on, and nothing here goes red.
"""

from __future__ import annotations

import pytest
from SpiriSynq.session import Session, current_session

from SpiriCamera.camera import Camera

from .conftest import SYNQ_NAMESPACE


class TestNamespace:
    """Topics are scoped to this run."""

    def test_the_namespace_is_unique_to_this_run(self) -> None:
        """A crashed run must leave nothing for the next one to find."""
        assert SYNQ_NAMESPACE.startswith("spiricamera-test-")
        assert SYNQ_NAMESPACE != "spiricamera-test-"

    def test_the_session_is_namespaced(self, synq_session: Session) -> None:
        """Authoritative topics get the namespace as their prefix."""
        assert synq_session.base_topic == SYNQ_NAMESPACE

    def test_the_default_session_is_namespaced_too(self) -> None:
        """The import-time session reads the environment as well.

        Anything constructed before the fixtures run, or in a thread with
        a fresh context, falls back to it — so it must be namespaced by
        the environment variable, not only by the fixture.
        """
        assert Session().base_topic == SYNQ_NAMESPACE

    def test_a_camera_lands_in_the_namespace(self) -> None:
        """The prefix reaches the topic a camera would actually publish."""
        cam = Camera("testimage://", synq_topic="probe", synq_auto_start=False)
        cam.synq_authoritive = True
        cam.sync()

        try:
            assert cam.synq_absolute_path.startswith(SYNQ_NAMESPACE)
        finally:
            cam.close()


class TestIsolation:
    """The peer talks to nobody."""

    def test_objects_use_the_isolated_session(self, synq_session: Session) -> None:
        """A camera picks up the session from the context variable."""
        cam = Camera("testimage://", synq_auto_start=False)

        assert cam.synq_session is synq_session

    def test_it_is_the_current_session(self, synq_session: Session) -> None:
        """Set for the whole run, so no test has to pass it in."""
        assert current_session.get() is synq_session

    @pytest.mark.parametrize(
        ("section", "key"),
        [("scouting/multicast", "enabled"), ("scouting/gossip", "enabled")],
    )
    def test_discovery_is_off(
        self, synq_session: Session, section: str, key: str
    ) -> None:
        """Nothing announces itself to the local network."""
        import json

        config = json.loads(synq_session.config.get_json(section))

        assert config[key] is False

    @pytest.mark.parametrize("section", ["listen/endpoints", "connect/endpoints"])
    def test_no_endpoints(self, synq_session: Session, section: str) -> None:
        """No listening, no dialling out."""
        import json

        assert json.loads(synq_session.config.get_json(section)) == []
