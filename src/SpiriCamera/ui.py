"""NiceGUI test UI for SpiriCamera."""

from __future__ import annotations

import base64

from nicegui import ui
from SpiriCamera import Camera


def _image_to_data_url(jpeg_bytes: bytes) -> str:
    """Bytes → base64 data URL for the image element."""
    if not jpeg_bytes:
        return ''
    return f'data:image/jpeg;base64,{base64.b64encode(jpeg_bytes).decode()}'


@ui.page('/')
def build_page():
    """Build the camera test UI page."""

    cam = Camera('testimage://')

    with ui.card():
        ui.label('Start / Stop')
        with ui.row():
            ui.button('Start', on_click=cam.start)
            ui.button('Stop', on_click=cam.stop)

    with ui.card():
        ui.label('Source')
        ui.input('Source string').bind_value(cam, 'source_str')

    with ui.card():
        ui.label('Image Settings')
        ui.label('Quality')
        ui.slider(min=1, max=100).bind_value(cam, 'quality')
        ui.number('Width').bind_value(cam, 'max_width')
        ui.number('Height').bind_value(cam, 'max_height')
        ui.number('Framerate').bind_value(cam, 'max_framerate')

    with ui.card():
        ui.label('Metadata')
        ui.input('Vendor').bind_value(cam, 'vendor')
        ui.input('Model').bind_value(cam, 'model')
        ui.input('Serial Number').bind_value(cam, 'serial_number')

    with ui.card():
        ui.label('Capabilities')
        ui.number('Max Supported Width').bind_value(cam, 'max_supported_width').props('suffix="px"')
        ui.number('Max Supported Height').bind_value(cam, 'max_supported_height').props('suffix="px"')
        ui.number('Max Supported Framerate').bind_value(cam, 'max_supported_framerate').props('suffix="fps"')
        ui.input('Mimetype').bind_value(cam, 'mimetype')
        ui.input('Synq Topic').bind_value(cam, 'synq_topic')

    with ui.card():
        ui.label('Parsed Source')
        ui.input('Source Type').bind_value(cam.source, 'source_type')
        ui.input('Scheme').bind_value(cam.source, 'scheme')
        ui.input('Path').bind_value(cam.source, 'path')
        ui.number('Camera Index').bind_value(cam.source, 'camera_index').props('step-controls')
        ui.checkbox('Is Valid').bind_value(cam.source, 'is_valid')

    with ui.card():
        ui.label('Live Preview')
        img = ui.image()
        img.bind_source_from(cam, 'image', backward=_image_to_data_url)




if __name__ == '__main__':
    ui.run(title='SpiriCamera Test UI', reload=False, show=False, dark=None)


__all__ = ['build_page', '_image_to_data_url']
