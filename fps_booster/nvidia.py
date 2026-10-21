"""Настройки драйвера NVIDIA для профилей игр через официальный NVAPI (DRS).

Меняем то же, что в «Панели управления NVIDIA → Программные настройки», и
только для cs2.exe / dota2.exe:

* Режим управления электропитанием — «Максимальная производительность»
  (карта не сбрасывает частоты между кадрами — нет просадок в начале раунда);
* Режим низкой задержки — «Вкл.» (очередь кадров 1 — меньше input lag).

Качество картинки эти параметры не трогают. Прежние значения запоминаются,
откат возвращает профиль ровно в исходное состояние.
"""

from __future__ import annotations

import ctypes
import os

from .winutil import IS_WINDOWS

# Идентификаторы настроек (NvApiDriverSettings.h)
PREFERRED_PSTATE_ID = 0x1057EB71
PREFERRED_PSTATE_PREFER_MAX = 1
PRERENDERLIMIT_ID = 0x007BA09E
LOW_LATENCY_ON = 1

GAME_SETTINGS = {
    PREFERRED_PSTATE_ID: PREFERRED_PSTATE_PREFER_MAX,
    PRERENDERLIMIT_ID: LOW_LATENCY_ON,
}

# Коды NvAPI_Status
NVAPI_OK = 0
NVAPI_INCOMPATIBLE_STRUCT_VERSION = -9
NVAPI_SETTING_NOT_FOUND = -160
NVAPI_PROFILE_NOT_FOUND = -163
NVAPI_EXECUTABLE_NOT_FOUND = -166

NVDRS_CURRENT_PROFILE_LOCATION = 0
NVDRS_DWORD_TYPE = 0

UNICODE_MAX = 2048
UnicodeString = ctypes.c_uint16 * UNICODE_MAX  # NvU16[2048], всегда 2 байта на символ


class NVDRS_BINARY_SETTING(ctypes.Structure):
    _fields_ = [("valueLength", ctypes.c_uint32), ("valueData", ctypes.c_uint8 * 4096)]


class NVDRS_VALUE(ctypes.Union):
    _fields_ = [("u32", ctypes.c_uint32), ("binary", NVDRS_BINARY_SETTING), ("wsz", UnicodeString)]


class NVDRS_SETTING(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("settingName", UnicodeString),
        ("settingId", ctypes.c_uint32),
        ("settingType", ctypes.c_uint32),
        ("settingLocation", ctypes.c_uint32),
        ("isCurrentPredefined", ctypes.c_uint32),
        ("isPredefinedValid", ctypes.c_uint32),
        ("predefined", NVDRS_VALUE),
        ("current", NVDRS_VALUE),
    ]


class NVDRS_PROFILE(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("profileName", UnicodeString),
        ("gpuSupport", ctypes.c_uint32),
        ("isPredefined", ctypes.c_uint32),
        ("numOfApps", ctypes.c_uint32),
        ("numOfSettings", ctypes.c_uint32),
    ]


class NVDRS_APPLICATION(ctypes.Structure):
    """Самая новая версия структуры (V4); старые версии — её начало."""
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("isPredefined", ctypes.c_uint32),
        ("appName", UnicodeString),
        ("userFriendlyName", UnicodeString),
        ("launcher", UnicodeString),
        ("fileInFolder", UnicodeString),   # с V2
        ("flags", ctypes.c_uint32),        # с V3: isMetro, isCommandLine
        ("commandLine", UnicodeString),    # с V4
    ]


def make_version(struct_size: int, ver: int) -> int:
    return struct_size | (ver << 16)


SETTING_VER = make_version(ctypes.sizeof(NVDRS_SETTING), 1)
PROFILE_VER = make_version(ctypes.sizeof(NVDRS_PROFILE), 1)
_S = ctypes.sizeof(UnicodeString)
# версии NVDRS_APPLICATION от новой к старой: драйвер принимает ту, которую знает
APPLICATION_VERS = [
    make_version(8 + 4 * _S + 4 + _S, 4),
    make_version(8 + 4 * _S + 4, 3),
    make_version(8 + 4 * _S, 2),
    make_version(8 + 3 * _S, 1),
]


def to_unicode(text: str) -> UnicodeString:
    buf = UnicodeString()
    data = text.encode("utf-16-le")[: (UNICODE_MAX - 1) * 2]
    for i in range(0, len(data), 2):
        buf[i // 2] = data[i] | (data[i + 1] << 8)
    return buf


class NvApiError(RuntimeError):
    pass


# Идентификаторы функций для nvapi_QueryInterface
_FN = {
    "Initialize": 0x0150E828,
    "Unload": 0xD22BDD7E,
    "GetErrorMessage": 0x6C2D048C,
    "DRS_CreateSession": 0x0694D52E,
    "DRS_DestroySession": 0xDAD9CFF8,
    "DRS_LoadSettings": 0x375DBD6B,
    "DRS_SaveSettings": 0xFCBC7E14,
    "DRS_FindApplicationByName": 0xEEE566B2,
    "DRS_FindProfileByName": 0x7E4A9A0B,
    "DRS_CreateProfile": 0xCC176068,
    "DRS_DeleteProfile": 0x17093206,
    "DRS_CreateApplication": 0x4347A9DE,
    "DRS_GetSetting": 0x73BF8338,
    "DRS_SetSetting": 0x577DD202,
    "DRS_DeleteProfileSetting": 0xE4A26362,
}

_P = ctypes.c_void_p
_SIGS = {
    "Initialize": (),
    "Unload": (),
    "GetErrorMessage": (ctypes.c_int, ctypes.c_char_p),
    "DRS_CreateSession": (ctypes.POINTER(_P),),
    "DRS_DestroySession": (_P,),
    "DRS_LoadSettings": (_P,),
    "DRS_SaveSettings": (_P,),
    "DRS_FindApplicationByName": (_P, ctypes.POINTER(UnicodeString), ctypes.POINTER(_P),
                                  ctypes.POINTER(NVDRS_APPLICATION)),
    "DRS_FindProfileByName": (_P, ctypes.POINTER(UnicodeString), ctypes.POINTER(_P)),
    "DRS_CreateProfile": (_P, ctypes.POINTER(NVDRS_PROFILE), ctypes.POINTER(_P)),
    "DRS_DeleteProfile": (_P, _P),
    "DRS_CreateApplication": (_P, _P, ctypes.POINTER(NVDRS_APPLICATION)),
    "DRS_GetSetting": (_P, _P, ctypes.c_uint32, ctypes.POINTER(NVDRS_SETTING)),
    "DRS_SetSetting": (_P, _P, ctypes.POINTER(NVDRS_SETTING)),
    "DRS_DeleteProfileSetting": (_P, _P, ctypes.c_uint32),
}


def available() -> bool:
    """Есть ли драйвер NVIDIA с NVAPI."""
    if not IS_WINDOWS:
        return False
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    return os.path.isfile(os.path.join(system32, "nvapi64.dll"))


class DrsSession:
    """Сессия базы профилей драйвера: with DrsSession() as drs: ..."""

    def __init__(self):
        dll = ctypes.CDLL("nvapi64.dll")
        query = dll.nvapi_QueryInterface
        query.restype = ctypes.c_void_p
        query.argtypes = [ctypes.c_uint32]
        self.fn = {}
        for name, fid in _FN.items():
            addr = query(fid)
            if not addr:
                raise NvApiError(f"NVAPI: нет функции {name} (слишком старый драйвер)")
            self.fn[name] = ctypes.CFUNCTYPE(ctypes.c_int, *_SIGS[name])(addr)
        self.session = _P()
        self.dirty = False

    def check(self, status: int, what: str) -> int:
        if status != NVAPI_OK:
            msg = (ctypes.c_char * 64)()
            try:
                self.fn["GetErrorMessage"](status, msg)
                text = msg.value.decode("ascii", "replace")
            except OSError:
                text = ""
            raise NvApiError(f"{what}: {text or status}")
        return status

    def __enter__(self) -> "DrsSession":
        self.check(self.fn["Initialize"](), "NvAPI_Initialize")
        self.check(self.fn["DRS_CreateSession"](ctypes.byref(self.session)), "DRS_CreateSession")
        self.check(self.fn["DRS_LoadSettings"](self.session), "DRS_LoadSettings")
        return self

    def __exit__(self, exc_type, *_):
        try:
            if self.dirty and exc_type is None:
                self.check(self.fn["DRS_SaveSettings"](self.session), "DRS_SaveSettings")
        finally:
            self.fn["DRS_DestroySession"](self.session)

    # --- профили ------------------------------------------------------------------

    def _with_app_versions(self, call, what: str) -> int:
        for ver in APPLICATION_VERS:
            app = NVDRS_APPLICATION()
            app.version = ver
            status = call(app)
            if status != NVAPI_INCOMPATIBLE_STRUCT_VERSION:
                return status
        raise NvApiError(f"{what}: драйвер не поддерживает ни одну версию структуры")

    def find_app_profile(self, exe: str) -> _P | None:
        profile = _P()
        name = to_unicode(exe)
        status = self._with_app_versions(
            lambda app: self.fn["DRS_FindApplicationByName"](self.session, ctypes.byref(name),
                                                             ctypes.byref(profile), ctypes.byref(app)),
            "DRS_FindApplicationByName")
        if status == NVAPI_EXECUTABLE_NOT_FOUND:
            return None
        self.check(status, "DRS_FindApplicationByName")
        return profile

    def find_profile(self, profile_name: str) -> _P | None:
        profile = _P()
        status = self.fn["DRS_FindProfileByName"](self.session, ctypes.byref(to_unicode(profile_name)),
                                                  ctypes.byref(profile))
        if status == NVAPI_PROFILE_NOT_FOUND:
            return None
        self.check(status, "DRS_FindProfileByName")
        return profile

    def create_app_profile(self, profile_name: str, exe: str) -> _P:
        prof = NVDRS_PROFILE()
        prof.version = PROFILE_VER
        prof.profileName = to_unicode(profile_name)
        handle = _P()
        self.check(self.fn["DRS_CreateProfile"](self.session, ctypes.byref(prof), ctypes.byref(handle)),
                   "DRS_CreateProfile")

        def create(app):
            app.appName = to_unicode(exe)
            return self.fn["DRS_CreateApplication"](self.session, handle, ctypes.byref(app))

        self.check(self._with_app_versions(create, "DRS_CreateApplication"), "DRS_CreateApplication")
        self.dirty = True
        return handle

    def delete_profile(self, handle: _P) -> None:
        self.check(self.fn["DRS_DeleteProfile"](self.session, handle), "DRS_DeleteProfile")
        self.dirty = True

    # --- настройки -----------------------------------------------------------------

    def get_own(self, handle: _P, setting_id: int) -> int | None:
        """Значение, заданное пользователем именно в этом профиле, иначе None."""
        s = NVDRS_SETTING()
        s.version = SETTING_VER
        status = self.fn["DRS_GetSetting"](self.session, handle, setting_id, ctypes.byref(s))
        if status == NVAPI_SETTING_NOT_FOUND:
            return None
        self.check(status, "DRS_GetSetting")
        if s.settingLocation == NVDRS_CURRENT_PROFILE_LOCATION and not s.isCurrentPredefined:
            return int(s.current.u32)
        return None

    def set_dword(self, handle: _P, setting_id: int, value: int) -> None:
        s = NVDRS_SETTING()
        s.version = SETTING_VER
        s.settingId = setting_id
        s.settingType = NVDRS_DWORD_TYPE
        s.current.u32 = value
        self.check(self.fn["DRS_SetSetting"](self.session, handle, ctypes.byref(s)), "DRS_SetSetting")
        self.dirty = True

    def reset(self, handle: _P, setting_id: int) -> None:
        status = self.fn["DRS_DeleteProfileSetting"](self.session, handle, setting_id)
        if status != NVAPI_SETTING_NOT_FOUND:
            self.check(status, "DRS_DeleteProfileSetting")
        self.dirty = True


def own_profile_name(exe: str) -> str:
    return f"FPS Booster - {exe}"


def apply_game(exe: str) -> dict:
    """Ставит игровые настройки в профиль игры. Возвращает данные для отката."""
    with DrsSession() as drs:
        handle = drs.find_app_profile(exe)
        created = handle is None
        if created:  # у CS2 и Dota 2 профиль в драйвере есть, это запасной путь
            handle = drs.create_app_profile(own_profile_name(exe), exe)
        previous = {str(sid): (None if created else drs.get_own(handle, sid)) for sid in GAME_SETTINGS}
        for sid, value in GAME_SETTINGS.items():
            drs.set_dword(handle, sid, value)
    return {"created": created, "previous": previous}


def revert_game(exe: str, info: dict) -> None:
    with DrsSession() as drs:
        if info.get("created"):
            handle = drs.find_profile(own_profile_name(exe))
            if handle is not None:
                drs.delete_profile(handle)
            return
        handle = drs.find_app_profile(exe)
        if handle is None:
            return
        for sid, value in (info.get("previous") or {}).items():
            if value is None:
                drs.reset(handle, int(sid))  # вернуть значение по умолчанию от NVIDIA
            else:
                drs.set_dword(handle, int(sid), int(value))
