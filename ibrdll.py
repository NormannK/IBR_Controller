import os
import time
import ctypes
import threading
import traceback
from ctypes import c_short, c_double, c_char_p, POINTER, byref
from ctypes import wintypes

# CALLBACK == __stdcall (unused here but kept for completeness / future streaming API)
DDK_FctPtr = ctypes.WINFUNCTYPE(
    None,              # void
    c_short,           # devicenr
    c_short,           # address
    c_short,           # type
    wintypes.DWORD,    # unsigned long intval
    c_double,          # double floatval
)

# ---- define missing Win32 types (some Python builds omit these in wintypes) ----
if ctypes.sizeof(ctypes.c_void_p) == 8:
    LRESULT = ctypes.c_longlong
else:
    LRESULT = ctypes.c_long

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PM_REMOVE = 0x0001
ERROR_CLASS_ALREADY_EXISTS = 1410

WNDPROCTYPE = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROCTYPE),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", POINT),
    ]


# Prototypes used by the pump
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM

user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT

user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
user32.PeekMessageW.restype = wintypes.BOOL

user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.TranslateMessage.restype = wintypes.BOOL

user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE


class IbrDll:
    """Thin ctypes wrapper with a lightweight message pump + a faster multi-read helper."""

    def __init__(self, dll_path: str):
        self.initialized = False

        dll_path = os.path.abspath(dll_path)
        self._dll_path = dll_path
        self._dll_dir = os.path.dirname(dll_path)

        # Ensure the DLL + its dependencies are discoverable
        self._dll_dir_handle = None
        if hasattr(os, "add_dll_directory"):
            # The returned handle must stay alive or Windows removes the search path.
            self._dll_dir_handle = os.add_dll_directory(self._dll_dir)
        os.environ["PATH"] = self._dll_dir + os.pathsep + os.environ.get("PATH", "")

        # IMPORTANT: __stdcall DLL => WinDLL
        self.dll = ctypes.WinDLL(dll_path, use_last_error=True)

        # Prototypes per the .h
        self.Device_Init = self.dll.Device_Init
        self.Device_Init.restype = c_short
        self.Device_Init.argtypes = [c_short, c_char_p, wintypes.HWND, wintypes.HWND]

        # Optional export
        if hasattr(self.dll, "Device_PreInit"):
            self.Device_PreInit = self.dll.Device_PreInit
            self.Device_PreInit.restype = None
            self.Device_PreInit.argtypes = [c_short, c_short, c_short, c_short, c_short, c_short, c_short]
        else:
            self.Device_PreInit = None

        self.Device_GetVersion = self.dll.Device_GetVersion
        self.Device_GetVersion.restype = None
        self.Device_GetVersion.argtypes = [POINTER(c_short), POINTER(c_short)]

        self.Device_Value = self.dll.Device_Value
        self.Device_Value.restype = c_short
        self.Device_Value.argtypes = [c_short, c_short, POINTER(c_double)]

        self.Device_DeInit = self.dll.Device_DeInit
        self.Device_DeInit.restype = c_short
        self.Device_DeInit.argtypes = []

        self._wndproc_ref = None
        self._msg = MSG()  # reusable

    def get_version(self) -> tuple[int, int]:
        major = c_short()
        minor = c_short()
        self.Device_GetVersion(byref(major), byref(minor))
        return major.value, minor.value

    def _create_hidden_message_window(self) -> wintypes.HWND:
        # Each instance needs its own class because the class owns the WNDPROC callback.
        class_name = f"IBR_DDK_PY_MSGWND_{os.getpid()}_{id(self):x}"

        @WNDPROCTYPE
        def wndproc(hwnd, msg, wparam, lparam):
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = wndproc
        hinst = kernel32.GetModuleHandleW(None)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = wndproc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinst
        wc.hIcon = 0
        wc.hCursor = 0
        wc.hbrBackground = 0
        wc.lpszMenuName = None
        wc.lpszClassName = class_name
        wc.hIconSm = 0

        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            if err != ERROR_CLASS_ALREADY_EXISTS:
                raise OSError(f"RegisterClassExW failed: {err} / {ctypes.FormatError(err).strip()}")

        hwnd = user32.CreateWindowExW(0, class_name, "IBRHidden", 0, 0, 0, 0, 0, 0, 0, hinst, None)
        if not hwnd:
            err = ctypes.get_last_error()
            raise OSError(f"CreateWindowExW failed: {err} / {ctypes.FormatError(err).strip()}")

        return hwnd

    def pump_messages(self, *, limit: int = 50) -> int:
        """Pump Win32 messages on the current thread. Cheap; helps DLLs that signal via messages."""
        n = 0
        while n < int(limit) and user32.PeekMessageW(ctypes.byref(self._msg), 0, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(self._msg))
            user32.DispatchMessageW(ctypes.byref(self._msg))
            n += 1
        return n

    def init_device(self, setup_filename: str, *, timeout_s: float = 30.0, imb_control: int = 1) -> int:
        prev_cwd = os.getcwd()
        try:
            os.chdir(self._dll_dir)
        except Exception:
            pass

        try:
            if self.Device_PreInit is not None:
                self.Device_PreInit(c_short(1), c_short(0), c_short(0), c_short(int(imb_control)), c_short(0), c_short(0), c_short(0))

            language = c_short(1)
            setup_bytes = os.fspath(setup_filename).encode("mbcs")
            setup_c = c_char_p(setup_bytes)

            hwnd = self._create_hidden_message_window()
            parent = wintypes.HWND(hwnd)
            wh = wintypes.HWND(hwnd)

            done = threading.Event()
            result = {"rc": None, "exc": None}

            def init_thread():
                try:
                    rc = self.Device_Init(language, setup_c, parent, wh)
                    result["rc"] = int(rc)
                except Exception:
                    result["exc"] = traceback.format_exc()
                finally:
                    done.set()

            t = threading.Thread(target=init_thread, daemon=True)
            t.start()

            start = time.time()
            while not done.is_set():
                self.pump_messages(limit=200)
                if timeout_s is not None and (time.time() - start) > float(timeout_s):
                    return 124
                time.sleep(0.01)

            if result["exc"]:
                return 998

            rc = int(result["rc"]) if result["rc"] is not None else 999
            if rc in (0, -1):
                self.initialized = True
                return 0
            return rc
        finally:
            try:
                os.chdir(prev_cwd)
            except Exception:
                pass

    def make_fast_reader(self, devicenr: int, addresses: list[int]):
        """
        Create a high-throughput reader for a fixed device + address list.
        Avoids per-call ctypes allocations seen in get_value().

        Returns a callable: () -> (rcs: list[int], vals: list[float])
        """
        dev_c = c_short(int(devicenr))
        addr_cs = [c_short(int(a)) for a in addresses]
        vals = [c_double() for _ in addresses]
        dev_value = self.Device_Value

        def _read_all():
            rcs = [0] * len(addr_cs)
            out = [0.0] * len(addr_cs)
            for i, addr_c in enumerate(addr_cs):
                rc = int(dev_value(dev_c, addr_c, byref(vals[i])))
                rcs[i] = rc
                out[i] = float(vals[i].value)
            return rcs, out

        return _read_all

    def get_value(self, devicenr: int, address: int) -> tuple[int, float]:
        val = c_double()
        rc = int(self.Device_Value(c_short(devicenr), c_short(address), byref(val)))
        return rc, float(val.value)

    def deinit_device(self) -> int:
        if not self.initialized:
            return -1
        rc = int(self.Device_DeInit())
        self.initialized = False
        return rc
