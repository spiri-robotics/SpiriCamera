"""Tests for NiceGUI UI module."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

from SpiriCamera.ui import state, _State, _bytes_to_data_url, _add_label, _BoundLabel


class TestState:
    """Test the shared UI state object."""

    def test_state_camera_is_none(self) -> None:
        """Test initial camera is None."""
        assert state.camera is None

    def test_state_default_source(self) -> None:
        """Test initial source defaults."""
        assert state.source_str == '/dev/video0'

    def test_state_initial_quality(self) -> None:
        """Test initial quality defaults to 80."""
        assert state.quality == 80

    def test_state_initial_dimensions(self) -> None:
        """Test initial width/height/framerate."""
        assert state.max_width == 1920
        assert state.max_height == 1080
        assert state.max_framerate == 30


class TestBytesToDataURL:
    """Test _bytes_to_data_url conversion."""

    def test_bytes_to_data_url(self) -> None:
        """Test that bytes are converted to a valid data URI."""
        data = b'\xff\xd8\xff\xe0test'
        url = _bytes_to_data_url(data)

        assert url.startswith('data:image/jpeg;base64,')
        b64_part = url.split('base64,')[1]
        assert base64.b64decode(b64_part) == data

    def test_empty_bytes_empty_string(self) -> None:
        """Test empty bytes returns empty string."""
        assert _bytes_to_data_url(b'') == ''


class TestUIHandlers:
    """Test UI handler functions against real Camera dataclass attrs."""

    @patch('SpiriCamera.Camera')
    def test_on_stop_with_camera(self, mock_camera_cls: MagicMock) -> None:
        """Test _on_stop stops an existing camera."""
        mock_cam = MagicMock()
        state.camera = mock_cam

        from SpiriCamera.ui import _on_stop
        _on_stop()

        mock_cam.stop.assert_called_once()
        assert state.camera is None

    def test_on_stop_no_camera(self) -> None:
        """Test _on_stop with no camera is safe."""
        state.camera = None

        from SpiriCamera.ui import _on_stop
        _on_stop()  # Should not raise

    @patch('SpiriCamera.Camera')
    def test_on_start_creates_and_starts(self, mock_camera_cls: MagicMock) -> None:
        """Test _on_start creates Camera, calls start(), assigns to state."""
        mock_cam = MagicMock()
        mock_cam.source_str = '/dev/video0'
        mock_camera_cls.return_value = mock_cam

        state.camera = None
        state.source_str = 'rtsp://host/stream'
        state.quality = 85

        from SpiriCamera.ui import _on_start
        _on_start()

        mock_camera_cls.assert_called_once_with(
            source='rtsp://host/stream', quality=85,
            max_width=1920, max_height=1080, max_framerate=30,
        )
        mock_cam.start.assert_called_once()
        assert state.camera is mock_cam

    @patch('SpiriCamera.Camera')
    def test_on_start_fails_to_create(self, mock_camera_cls: MagicMock) -> None:
        """Test _on_start with invalid source shows error."""
        mock_camera_cls.side_effect = ValueError('invalid source')

        state.camera = None
        state.source_str = 'bad-source'

        from SpiriCamera.ui import _on_start
        _on_start()

        assert state.camera is None

    @patch('SpiriCamera.Camera')
    def test_on_start_fails_to_start(self, mock_camera_cls: MagicMock) -> None:
        """Test _on_start when Camera.start() raises."""
        mock_cam = MagicMock()
        mock_camera_cls.return_value = mock_cam
        mock_cam.start.side_effect = RuntimeError('device busy')

        state.camera = None
        state.source_str = '/dev/video0'

        from SpiriCamera.ui import _on_start
        _on_start()

        assert state.camera is None


class TestBoundLabels:
    """Test _add_label creates bound labels for CameraBase fields."""

    def test_add_label_creates_label(self) -> None:
        """Test _add_label creates a _BoundLabel with correct name."""
        b = _BoundLabel('test_field')
        assert b.name == 'test_field'
        assert b.value == '-'

    def test_add_label_visible_in_bound_labels(self) -> None:
        """Test _add_label adds to global bound_labels list."""
        from SpiriCamera.ui import bound_labels

        count = len(bound_labels)
        _add_label('bound_field')
        assert len(bound_labels) == count + 1
        assert bound_labels[-1].name == 'bound_field'

    @patch('SpiriCamera.Camera')
    def test_bound_label_reads_camera_field(self, mock_camera_cls: MagicMock) -> None:
        """Test bound label reflects CameraBase field values."""
        from SpiriCamera.ui import _cf

        mock_cam = MagicMock()
        mock_cam.synq_topic = 'spiricamera_dev_video0'
        mock_cam.running = True
        mock_cam.vendor = 'v4l2'

        # Check _cf callable
        fn = _cf('synq_topic')
        assert fn(mock_cam) == 'spiricamera_dev_video0'
        assert fn(None) == '-'  # default when camera is None

        fn2 = _cf('vendor')
        assert fn2(mock_cam) == 'v4l2'
        assert fn2(None) == '-'

        fn3 = _cf('running')
        assert fn3(mock_cam) == 'True'

    def test_bytes_to_data_url_valid(self) -> None:
        """Test _bytes_to_data_url with valid JPEG bytes."""
        jpeg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b'test data'
        url = _bytes_to_data_url(jpeg)

        assert url.startswith('data:image/jpeg;base64,')
        decoded = base64.b64decode(url.split(',', 1)[1])
        assert decoded == jpeg


class TestCameraPropertiesExist:
    """Test that real dataclass attrs exist — nothing invented."""

    def test_real_dataclass_attrs_exist(self) -> None:
        """Test that real CameraBase attributes exist."""
        from SpiriCamera import Camera

        cam = Camera(source='/dev/video0', quality=90)

        # CameraBase fields (inherited)
        assert hasattr(cam, 'source_str')
        assert hasattr(cam, 'synq_topic')
        assert hasattr(cam, 'mimetype')
        assert hasattr(cam, 'quality')
        assert hasattr(cam, 'max_width')
        assert hasattr(cam, 'max_height')
        assert hasattr(cam, 'max_framerate')
        assert hasattr(cam, 'max_supported_width')
        assert hasattr(cam, 'max_supported_height')
        assert hasattr(cam, 'max_supported_framerate')
        assert hasattr(cam, 'vendor')
        assert hasattr(cam, 'model')
        assert hasattr(cam, 'serial_number')
        assert hasattr(cam, 'image')

        # Camera-specific fields
        assert hasattr(cam, 'source')
        assert hasattr(cam, 'running')

        # Nothing extra
        assert not hasattr(cam, 'source_type')
        assert not hasattr(cam, 'is_connected')
        assert not hasattr(cam, '_last_frame')
        assert not hasattr(cam, '_poll_state')
