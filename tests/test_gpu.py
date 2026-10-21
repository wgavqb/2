import ctypes

from fps_booster import nvidia
from fps_booster.gpu import (
    GPU, display_driver_version, is_integrated, parse_driver_date, pick_best, supports_hags, vendor_of,
)
from fps_booster.tweaks import build_tweaks, merge_dx_settings

GB = 1 << 30


def gpu(name, vendor, vram=8 * GB, integrated=None):
    integ = is_integrated(vendor, name, vram) if integrated is None else integrated
    return GPU(f"id-{name}", name, vendor, 0, 0, vram, integ)


def test_vendor_ids():
    assert vendor_of(0x10DE) == "nvidia"
    assert vendor_of(0x1002) == "amd"
    assert vendor_of(0x8086) == "intel"
    assert vendor_of(0x1234) == "other"


def test_integrated_detection():
    assert is_integrated("intel", "Intel(R) UHD Graphics 770", 128 << 20)
    assert is_integrated("intel", "Intel(R) Iris(R) Xe Graphics", 128 << 20)
    assert not is_integrated("intel", "Intel(R) Arc(TM) A770 Graphics", 16 * GB)
    assert not is_integrated("intel", "Intel(R) Arc(TM) B580 Graphics", 12 * GB)
    assert is_integrated("amd", "AMD Radeon(TM) Graphics", 512 << 20)
    assert is_integrated("amd", "AMD Radeon 780M Graphics", 2 * GB)
    assert not is_integrated("amd", "AMD Radeon RX 7800 XT", 16 * GB)
    assert not is_integrated("nvidia", "NVIDIA GeForce RTX 4060 Laptop GPU", 8 * GB)


def test_driver_formatting():
    assert parse_driver_date("10-8-2025") == "2025-10-08"
    assert parse_driver_date("garbage") == ""
    assert display_driver_version("nvidia", "32.0.15.6094") == "560.94"
    assert display_driver_version("nvidia", "31.0.15.3623") == "536.23"
    assert display_driver_version("amd", "31.0.24027.1012") == "31.0.24027.1012"


def test_hags_support():
    assert supports_hags(gpu("NVIDIA GeForce RTX 3060", "nvidia"))
    assert supports_hags(gpu("NVIDIA GeForce GTX 1660 SUPER", "nvidia"))
    assert not supports_hags(gpu("NVIDIA GeForce GTX 960", "nvidia"))
    assert supports_hags(gpu("AMD Radeon RX 6700 XT", "amd"))
    assert not supports_hags(gpu("Radeon RX 580 Series", "amd"))
    assert supports_hags(gpu("Intel(R) Arc(TM) A750 Graphics", "intel"))


def test_pick_best_prefers_discrete():
    igpu = gpu("Intel(R) UHD Graphics 630", "intel", 128 << 20)
    dgpu = gpu("NVIDIA GeForce RTX 4070", "nvidia", 12 * GB)
    assert pick_best([igpu, dgpu]) is dgpu
    assert pick_best([igpu]) is igpu
    assert pick_best([]) is None


def test_merge_dx_settings_keeps_existing():
    assert merge_dx_settings("", {"SwapEffectUpgradeEnable": "1"}) == "SwapEffectUpgradeEnable=1;"
    assert (merge_dx_settings("VRROptimizeEnable=0;SwapEffectUpgradeEnable=0;", {"SwapEffectUpgradeEnable": "1"})
            == "VRROptimizeEnable=0;SwapEffectUpgradeEnable=1;")


def test_tweaks_follow_selected_gpu(tmp_path):
    from fps_booster.games import CS2, InstalledGame

    games = [InstalledGame(CS2, tmp_path)]
    nv = {t.id: t for t in build_tweaks(games, gpu("NVIDIA GeForce RTX 4070", "nvidia"))}
    amd = {t.id: t for t in build_tweaks(games, gpu("AMD Radeon RX 7800 XT", "amd"))}
    old = {t.id: t for t in build_tweaks(games, gpu("NVIDIA GeForce GTX 960", "nvidia"))}
    igpu = {t.id: t for t in build_tweaks(games, gpu("Intel(R) UHD Graphics 770", "intel", 128 << 20))}

    assert nv["nvidia_profile"].available() and not amd["nvidia_profile"].available()
    assert nv["hags"].default and amd["hags"].default and not old["hags"].default
    assert nv["gpu_pref"].values[0].value == "GpuPreference=2;"
    assert igpu["gpu_pref"].values[0].value == "GpuPreference=1;"
    assert "RTX 4070" in nv["gpu_pref"].title


def test_nvapi_struct_versions_match_sdk():
    # значения из nvapi.h: NVDRS_SETTING_VER, NVDRS_PROFILE_VER, NVDRS_APPLICATION_VER_V1..V4
    assert ctypes.sizeof(nvidia.NVDRS_SETTING) == 12320
    assert nvidia.SETTING_VER == 0x13020
    assert nvidia.PROFILE_VER == 0x11014
    assert nvidia.APPLICATION_VERS == [0x4500C, 0x3400C, 0x24008, 0x13008]
    assert bytes(nvidia.to_unicode("cs2.exe"))[:16] == "cs2.exe\0".encode("utf-16-le")
