"""Conftest for NiceGUI UI testing.

Uses the official ``user_simulation`` with the ``root`` parameter.
The ``root`` function is the page handler — NiceGUI calls it
directly for each simulated browser connection.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from nicegui import ui
from nicegui.testing.user import User
from nicegui.testing.user_simulation import user_simulation


@pytest.fixture
async def user() -> AsyncGenerator[User, None]:
    """NiceGUI user fixture using official user_simulation.

    Imports the ``build_page`` function (the ``@ui.page('/')`` handler)
    and passes it as ``root``. This bypasses the decorator route cache
    issue that happens with ``main_file``.
    """
    from SpiriCamera.ui import build_page

    async with user_simulation(root=build_page) as user:
        yield user
