"""Main file for user_simulation.

Imports the SpiriCamera UI module (registering @ui.page('/'))
and starts the NiceGUI server.
"""

# Import to register @ui.page('/') route
import SpiriCamera.ui

# Start the server
from nicegui import ui
ui.run(title='TestApp', reload=False)
