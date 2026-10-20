"""macOS power and window integration, imported only on Darwin."""
_activity = None


def keep_awake():
    global _activity
    from Foundation import NSProcessInfo, NSActivityUserInitiatedAllowingIdleSystemSleep
    if _activity is None:
        _activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep, "Real-time voice translation")


def configure_overlay(widget):
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() != "cocoa":
        return
    import objc
    from AppKit import (NSWindowCollectionBehaviorCanJoinAllSpaces,
                        NSWindowCollectionBehaviorFullScreenAuxiliary, NSFloatingWindowLevel)
    view = objc.objc_object(c_void_p=int(widget.winId()))
    window = view.window()
    window.setCollectionBehavior_(window.collectionBehavior()
        | NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary)
    window.setLevel_(NSFloatingWindowLevel)
