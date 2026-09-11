"""NiceGUI web UI for SpiriCamera testing and inspection.

Run with::

    uv run python -m SpiriCamera.ui

Binds directly to :py:class:`~SpiriCamera.Camera` dataclass fields via
NiceGUI's native binding system.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nicegui import ui

if TYPE_CHECKING:
    from SpiriCamera.camera import Camera


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class _State:
    """Mutable container for the Camera instance."""

    camera: Camera | str | None = None
    # Editable UI fields
    source_str: str = 'testimage://'
    quality: int = 80
    max_width: int = 1920
    max_height: int = 1080
    max_framerate: int = 30


@dataclass
class _BoundLabel:
    """Helper to create bindable labels for CameraBase fields."""

    name: str
    value: str = '-'


# Simple forward lambda: Camera → value
def _cf(field: str, default: str = '-') -> Any:
    """Return a backward lambda that reads *field* from Camera or *default*."""

    def backward(camera: Any) -> str:
        if camera is None:
            return default
        val = getattr(camera, field, None)
        if val is None:
            return default
        return str(val)

    return backward


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


state = _State()
bound_labels: list[_BoundLabel] = []


@ui.page('/')
def build_page() -> None:
    """Build the full UI page."""
    # ------------------------------------------------------------------
    # Control panel
    # ------------------------------------------------------------------
    with ui.card().classes('p-6 w-full max-w-4xl mx-auto'):
        ui.label('Source').classes('text-xl font-bold mt-2')
        source_in = ui.input(label='source_str') \
            .classes('w-full') \
            .bind_value(state, 'source_str')

        with ui.row().classes('gap-6 mt-4'):
            q_in = ui.slider(min=1, max=100).classes('grow') \
                .bind_value(state, 'quality')
            q_in.show_value = True

            w_in = ui.number(label='Max Width').classes('w-32') \
                .bind_value(state, 'max_width')

            h_in = ui.number(label='Max Height').classes('w-32') \
                .bind_value(state, 'max_height')

            f_in = ui.number(label='Max Framerate').classes('w-36') \
                .bind_value(state, 'max_framerate')

        with ui.row().classes('gap-4 mt-6'):
            ui.button('Start', on_click=_on_start) \
                .classes('bg-green-500 text-white').props('rounded')
            ui.button('Stop', on_click=_on_stop) \
                .classes('bg-red-500 text-white').props('rounded')

    # ------------------------------------------------------------------
    # Parsed source info (read-only)
    # ------------------------------------------------------------------
    with ui.card().classes('p-6 w-full max-w-4xl mx-auto mt-4'):
        ui.label('Parsed Source').classes('text-xl font-bold mb-4')
        for field in ('source_type', 'scheme', 'path'):
            _add_label(f'Source.{field}')

    # ------------------------------------------------------------------
    # Live preview (image)
    # ------------------------------------------------------------------
    with ui.card().classes('p-6 w-full max-w-4xl mx-auto mt-4'):
        ui.label('Live Preview').classes('text-xl font-bold mb-4')
        preview = ui.image().classes('w-full').style('max-height: 600px;')
        preview.bind_source_from(
            state, 'camera', backward=_image_backward)  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Status (read-only)
    # ------------------------------------------------------------------
    with ui.card().classes('p-6 w-full max-w-4xl mx-auto mt-4'):
        ui.label('Camera Fields').classes('text-xl font-bold mb-4')
        with ui.column().classes('gap-1'):
            for field in (
                'synq_topic', 'source_str', 'mimetype', 'quality',
                'max_width', 'max_height', 'max_framerate',
                'max_supported_width', 'max_supported_height',
                'max_supported_framerate',
                'vendor', 'model', 'serial_number', 'running',
            ):
                _add_label(field)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_label(field: str) -> None:
    """Create a label row bound to *field* on the Camera."""
    b = _BoundLabel(field)
    bound_labels.append(b)
    ui.label(f'{b.name}').classes('text-sm text-grey-7')
    ui.label(b.value).bind_text_from(state, 'camera', backward=_cf(field))  # type: ignore[misc]


def _bytes_to_data_url(data: bytes) -> str:
    """Convert image bytes to a data URI."""
    if not data:
        return ''
    b64 = __import__('base64').b64encode(data).decode('utf-8')
    return f'data:image/jpeg;base64,{b64}'


def _image_backward(camera: Any) -> str:
    """Backward lambda for image preview."""
    if camera is None:
        return ''
    return _bytes_to_data_url(camera.image if hasattr(camera, 'image') else b'')


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _on_start() -> None:
    """Create a Camera with current UI values and start it."""
    from SpiriCamera import Camera

    # Stop any existing camera first
    _on_stop()

    try:
        state.camera = None
        cam = Camera(
            source=state.source_str,
            quality=state.quality,
            max_width=state.max_width,
            max_height=state.max_height,
            max_framerate=state.max_framerate,
        )
        cam.start()
    except Exception as exc:
        ui.notify(f'Error: {exc}', type='negative')
        return

    state.camera = cam

    # Update all bound labels immediately (no need to wait for binding refresh)
    for b in bound_labels:
        try:
            val = getattr(cam, b.name, None)
            b.value = str(val) if val is not None else '-'
        except Exception:
            pass


def _on_stop() -> None:
    """Stop the current camera and reset state."""
    camera = state.camera
    if camera is not None:
        camera.stop()  # type: ignore[union-attr]
    state.camera = None
    for b in bound_labels:
        b.value = '-'


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the NiceGUI app."""
    ui.run(title='SpiriCamera', reload=False)


if __name__ in ('__main__', '__mp_main__'):
    main()
