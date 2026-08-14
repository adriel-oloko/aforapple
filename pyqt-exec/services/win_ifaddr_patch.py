"""Works around a real bug in the `ifaddr` package on Windows.

`aiortc`'s ICE gathering (via `aioice`) calls `ifaddr.get_adapters()` to
list local network interfaces as soon as an `RTCPeerConnection` starts
creating an offer. On Windows, `ifaddr`'s implementation
(`ifaddr/_win32.py`) does:

    name = adapter_info.AdapterName.decode()

with no encoding argument, so it defaults to strict UTF-8, on the
comment's own (not always true) assumption that this Win32
`GetAdaptersAddresses` field is always ASCII. On machines where some
adapter's internal name contains a non-UTF-8 byte (seen in the wild
with certain VPN clients, virtual adapters, or drivers), this raises
and kills ICE gathering entirely -- which looks, from the app's point
of view, like the fal.ai realtime connection itself failed (that's
just where our code happens to catch and log it), even though fal was
never involved.

Calling `apply()` once at startup (before any RTCPeerConnection is
created) replaces `ifaddr.get_adapters` / `ifaddr._win32.get_adapters`
with a copy of the same logic that decodes `AdapterName` leniently
(errors="replace") instead of raising.
"""

from __future__ import annotations

import sys


def apply() -> None:
    if sys.platform != "win32":
        return

    try:
        import ctypes
        from ctypes import wintypes

        import ifaddr
        import ifaddr._shared as shared
        import ifaddr._win32 as win32
    except ImportError:
        return

    if getattr(win32, "_lenient_decode_patch_applied", False):
        return

    def patched_get_adapters(include_unconfigured: bool = False):
        addressbuffersize = wintypes.ULONG(15 * 1024)
        retval = win32.ERROR_BUFFER_OVERFLOW
        while retval == win32.ERROR_BUFFER_OVERFLOW:
            addressbuffer = ctypes.create_string_buffer(addressbuffersize.value)
            retval = win32.iphlpapi.GetAdaptersAddresses(
                wintypes.ULONG(win32.AF_UNSPEC),
                wintypes.ULONG(0),
                None,
                ctypes.byref(addressbuffer),
                ctypes.byref(addressbuffersize),
            )
        if retval != win32.NO_ERROR:
            raise ctypes.WinError()  # type: ignore

        address_infos = []
        address_info = win32.IP_ADAPTER_ADDRESSES.from_buffer(addressbuffer)
        while True:
            address_infos.append(address_info)
            if not address_info.Next:
                break
            address_info = address_info.Next[0]

        result = []
        for adapter_info in address_infos:
            # The only change from upstream ifaddr: decode leniently
            # instead of strict utf-8, so one oddly-named adapter can't
            # take down the whole app.
            name = adapter_info.AdapterName.decode("utf-8", errors="replace")
            nice_name = adapter_info.Description
            index = adapter_info.IfIndex

            if adapter_info.FirstUnicastAddress:
                ips = list(
                    win32.enumerate_interfaces_of_adapter(
                        adapter_info.FriendlyName, adapter_info.FirstUnicastAddress[0]
                    )
                )
                result.append(shared.Adapter(name, nice_name, ips, index=index))
            elif include_unconfigured:
                result.append(shared.Adapter(name, nice_name, [], index=index))

        return result

    win32.get_adapters = patched_get_adapters
    ifaddr.get_adapters = patched_get_adapters
    win32._lenient_decode_patch_applied = True
