"""Windows system tray that owns the FastAPI webapp lifecycle.

Launched via ``tray.bat`` (which runs ``python -m app.tray``) on login,
so the home-automation webapp comes up always-on without a console
window. See ``run_tray`` in :mod:`app.tray.tray`.
"""
