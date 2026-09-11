"""Main file for SpiriCamera NiceGUI test UI."""

if __name__ == "__main__":
    from nicegui import ui

    # Import to register @ui.page('/') route
    import SpiriCamera.ui  # noqa: F401

    ui.run(title='SpiriCamera Test UI', reload=False)
