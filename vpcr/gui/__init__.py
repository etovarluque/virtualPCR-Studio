"""PyQt6 user interface."""


def app_settings():
    """The app's QSettings, in QSettings.defaultFormat(): the registry on
    Windows unless a caller (a test) switched the default to an INI file
    under a path of its own. QSettings("IAvH", APP_NAME) would ignore that
    switch and always write the user's real settings."""
    from PyQt6.QtCore import QSettings
    from vpcr import APP_NAME
    return QSettings(QSettings.defaultFormat(), QSettings.Scope.UserScope, "IAvH", APP_NAME)
