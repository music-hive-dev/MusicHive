"""Which sound device Windows plays to by default.

GStreamer's wasapisink stays with the device it opened, so AudioPlayer asks this
from time to time and moves the sound when the answer changes. Qt could tell, but
the Windows build leaves Qt's media backends out, so this asks Windows itself.
"""

import ctypes
import sys

_E_RENDER = 0  # sound going out
_E_CONSOLE = 0  # the role wasapisink asks for by default
_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_ALL = 0x17
# vtable slots, counting IUnknown's three
_GET_DEFAULT_AUDIO_ENDPOINT = 4  # IMMDeviceEnumerator
_GET_ID = 5  # IMMDevice
_RELEASE = 2

_enumerator: ctypes.c_void_p | None = None


def _guid(text: str) -> ctypes.c_byte * 16:
    guid = (ctypes.c_byte * 16)()
    ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid))
    return guid


def _method(pointer: ctypes.c_void_p, slot: int, *argtypes):
    """The COM method in `slot` of the object `pointer` points to."""
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)
    return prototype(vtable[slot])


def _get_enumerator() -> ctypes.c_void_p:
    global _enumerator
    if _enumerator is None:
        ole32 = ctypes.oledll.ole32
        try:  # Qt has usually done this already on the main thread
            ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        except OSError:
            pass
        enumerator = ctypes.c_void_p()
        ole32.CoCreateInstance(
            ctypes.byref(_guid("{BCDE0395-E52F-467C-8E3D-C4579291692E}")),  # MMDeviceEnumerator
            None,
            _CLSCTX_ALL,
            ctypes.byref(_guid("{A95664D2-9614-4F35-A746-DE8DB63617E6}")),  # IMMDeviceEnumerator
            ctypes.byref(enumerator),
        )
        _enumerator = enumerator
    return _enumerator


def default_output_id() -> str | None:
    """Windows' id of the default playback device, or None if there is none or this
    is not Windows."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes  # Windows only

    try:
        enumerator = _get_enumerator()
        device = ctypes.c_void_p()
        _method(
            enumerator,
            _GET_DEFAULT_AUDIO_ENDPOINT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )(enumerator, _E_RENDER, _E_CONSOLE, ctypes.byref(device))
        try:
            text = wintypes.LPWSTR()
            _method(device, _GET_ID, ctypes.POINTER(wintypes.LPWSTR))(device, ctypes.byref(text))
            try:
                return text.value
            finally:
                ctypes.windll.ole32.CoTaskMemFree(text)
        finally:
            vtable = ctypes.cast(device, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
            ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[_RELEASE])(device)
    except OSError:  # no playback device at all, or COM is not available
        return None
