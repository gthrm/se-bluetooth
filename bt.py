"""Bluetooth RFCOMM transport for macOS, built on IOBluetooth through PyObjC.

IOBluetooth delivers its callbacks through the run loop of the main thread, so every
function here must be called from the main thread. The app keeps the main thread
for Bluetooth and serves HTTP from other threads.
"""
# SPDX-License-Identifier: GPL-2.0-or-later
import threading
import time

import IOBluetooth
import objc
from Foundation import NSDate, NSDefaultRunLoopMode, NSObject, NSRunLoop

FTP_SERVICE = "OBEX File Transfer"
RFCOMM_UUID16 = 0x0003
MAJOR_PHONE = 0x02  # Bluetooth major device class "Phone"
SONY_ERICSSON_OUIS = ("00-0a-d9", "00-0e-07", "00-0f-de", "00-12-ee", "00-13-70", "00-15-e0", "00-16-20",
                      "00-16-b8", "00-18-13", "00-19-63", "00-1a-75", "00-1b-59", "00-1c-a4", "00-1d-28",
                      "00-1e-45", "00-1f-e4", "00-21-9e", "00-22-98", "00-23-45", "00-23-f1", "00-24-ef",
                      "00-25-e7", "00-80-37")


PAGE_TIMEOUT = 0xFFFF  # in 0.625 ms slots: about 41 s
IO_TIMEOUT = 0xE00002D6  # kIOReturnTimeout: the phone didn't answer the page
IO_ERROR = 0xE00002BC    # kIOReturnError: the phone refused the channel (an old session is still open)
UNREACHABLE = ("The phone doesn't answer over Bluetooth. Check that Bluetooth is on in the phone "
               "(Settings > Connectivity > Bluetooth) and that it's close to the Mac.")


class BTError(Exception):
    pass


class ChannelClosed(BTError):
    """The phone closed the connection or stopped answering."""


def pump(seconds=0.05):
    """Let IOBluetooth deliver pending callbacks for up to `seconds`."""
    NSRunLoop.currentRunLoop().runMode_beforeDate_(
        NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(seconds))


# set to abandon every wait at once (the app is quitting)
cancel = threading.Event()


def wait_for(cond, timeout):
    end = time.time() + timeout
    while not cond():
        if time.time() > end or cancel.is_set():
            return False
        pump(0.05)
    return True


def _norm(address):
    return address.strip().lower().replace(":", "-")


def is_phone(dev):
    cod = dev.classOfDevice() or 0
    return (cod >> 8) & 0x1F == MAJOR_PHONE or _norm(dev.addressString() or "")[:8] in SONY_ERICSSON_OUIS


def describe(dev):
    return {"address": _norm(dev.addressString()), "name": dev.name() or dev.addressString(),
            "paired": bool(dev.isPaired())}


def paired_phones():
    return [describe(d) for d in (IOBluetooth.IOBluetoothDevice.pairedDevices() or []) if is_phone(d)]


def preferred_phone(phones):
    """The phone to use when none was picked: a W810i, else any Sony Ericsson, else the first."""
    for p in phones:
        if "W810" in p["name"]:
            return p
    for p in phones:
        if p["address"][:8] in SONY_ERICSSON_OUIS:
            return p
    return phones[0] if phones else None


class _InquiryDelegate(NSObject):
    def init(self):
        self = objc.super(_InquiryDelegate, self).init()
        self.found = {}
        self.done = False
        return self

    def deviceInquiryDeviceFound_device_(self, inquiry, dev):
        self.found[_norm(dev.addressString())] = dev

    def deviceInquiryDeviceNameUpdated_device_devicesRemaining_(self, inquiry, dev, remaining):
        self.found[_norm(dev.addressString())] = dev

    @objc.typedSelector(b"v@:@iZ")
    def deviceInquiryComplete_error_aborted_(self, inquiry, error, aborted):
        self.done = True


def scan(seconds=10):
    """Discoverable phones nearby: [{'address', 'name', 'paired'}]."""
    delegate = _InquiryDelegate.alloc().init()
    inquiry = IOBluetooth.IOBluetoothDeviceInquiry.inquiryWithDelegate_(delegate)
    inquiry.setInquiryLength_(seconds)
    inquiry.setUpdateNewDeviceNames_(True)
    if inquiry.start() != 0:
        raise BTError("Can't search for devices. Is Bluetooth turned on?")
    wait_for(lambda: delegate.done, seconds + 15)
    inquiry.stop()
    return [describe(d) for d in delegate.found.values() if is_phone(d)]


class _PairDelegate(NSObject):
    def init(self):
        self = objc.super(_PairDelegate, self).init()
        self.pin = b"0000"
        self.result = None
        return self

    def devicePairingPINCodeRequest_(self, sender):
        code = self.pin.ljust(16, b"\0")
        sender.replyPINCode_PINCode_(len(self.pin), (tuple(code),))

    @objc.typedSelector(b"v@:@I")
    def devicePairingUserConfirmationRequest_numericValue_(self, sender, value):
        sender.replyUserConfirmation_(True)

    @objc.typedSelector(b"v@:@i")
    def devicePairingFinished_error_(self, sender, error):
        self.result = error


def pair(address, pin="0000", timeout=60):
    """Pair with the phone. The phone asks for the same code; type it there."""
    dev = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(_norm(address))
    if dev is None:
        raise BTError("Bad Bluetooth address: " + address)
    if dev.isPaired():
        return
    delegate = _PairDelegate.alloc().init()
    delegate.pin = pin.encode()
    pairing = IOBluetooth.IOBluetoothDevicePair.pairWithDevice_(dev)
    pairing.setDelegate_(delegate)
    if pairing.start() != 0:
        raise BTError("Can't start pairing")
    if not wait_for(lambda: delegate.result is not None, timeout):
        pairing.stop()
        raise BTError("Pairing timed out. Enter %s on the phone when it asks for a passcode." % pin)
    if delegate.result != 0 or not dev.isPaired():
        raise BTError("Pairing failed (error 0x%x). Try again and enter %s on the phone."
                      % (delegate.result & 0xFFFFFFFF, pin))


class _SDPDelegate(NSObject):
    def init(self):
        self = objc.super(_SDPDelegate, self).init()
        self.status = None
        return self

    @objc.typedSelector(b"v@:@i")
    def sdpQueryComplete_status_(self, dev, status):
        self.status = status

    @objc.typedSelector(b"v@:@i")
    def remoteNameRequestComplete_status_(self, dev, status):
        pass

    @objc.typedSelector(b"v@:@i")
    def connectionComplete_status_(self, dev, status):
        pass


def _rfcomm_channel(record):
    """RFCOMM channel number from a service record's protocol descriptor list."""
    rfcomm = IOBluetooth.IOBluetoothSDPUUID.uuid16_(RFCOMM_UUID16)
    pdl = record.getAttributeDataElement_(0x0004)
    for proto in (pdl.getArrayValue() or []) if pdl is not None else []:
        items = proto.getArrayValue() or []
        uuid = items[0].getUUIDValue() if items else None
        if len(items) > 1 and uuid is not None and uuid.isEqualToUUID_(rfcomm):
            return int(items[1].getNumberValue())
    return None


def find_service(dev, name, refresh=False):
    """RFCOMM channel of the named service, asking the phone (SDP) when not cached."""
    def lookup():
        for rec in dev.services() or []:
            if rec.getServiceName() == name:
                ch = _rfcomm_channel(rec)
                if ch:
                    return ch
        return None

    ch = None if refresh else lookup()
    if ch is None:
        delegate = _SDPDelegate.alloc().init()
        if dev.performSDPQuery_(delegate) != 0 or not wait_for(lambda: delegate.status is not None, 40):
            raise BTError(UNREACHABLE)
        if delegate.status != 0:
            raise BTError("The phone is out of reach (SDP error 0x%x)" % (delegate.status & 0xFFFFFFFF))
        ch = lookup()
    if ch is None:
        raise BTError("The phone doesn't offer %s" % name)
    return ch


class _ChannelDelegate(NSObject):
    def init(self):
        self = objc.super(_ChannelDelegate, self).init()
        self.buf = bytearray()
        self.closed = False
        return self

    def rfcommChannelData_data_length_(self, channel, data, length):
        self.buf += bytes(data[:length])

    def rfcommChannelClosed_(self, channel):
        self.closed = True


class Channel:
    """A connected RFCOMM channel to one service of a device."""

    def __init__(self, address, service=FTP_SERVICE):
        self.dev = IOBluetooth.IOBluetoothDevice.deviceWithAddressString_(_norm(address))
        if self.dev is None:
            raise BTError("Bad Bluetooth address: " + address)
        self.name = self.dev.name() or address
        self.opened_link = False
        try:
            self._open(service)
        except Exception:
            if self.opened_link:
                self.dev.closeConnection()  # don't leave the link up: the phone then can't drop its old session
            raise

    def _open(self, service):
        if not self.dev.isConnected():
            # the W810i answers a page slowly (often 10-20 s, the default limit): allow the maximum
            status = self.dev.openConnection_withPageTimeout_authenticationRequired_(None, PAGE_TIMEOUT, False)
            if status != 0:
                raise BTError("%s (page error 0x%x)" % (UNREACHABLE, status & 0xFFFFFFFF))
            self.opened_link = True
        ch = find_service(self.dev, service)
        self.delegate = _ChannelDelegate.alloc().init()
        status, chan = self.dev.openRFCOMMChannelSync_withChannelID_delegate_(None, ch, self.delegate)
        if status & 0xFFFFFFFF == IO_TIMEOUT:
            raise BTError("%s (RFCOMM timeout)" % UNREACHABLE)
        if status != 0 or chan is None:
            # the channel number may have changed since the last SDP query
            ch = find_service(self.dev, service, refresh=True)
            status, chan = self.dev.openRFCOMMChannelSync_withChannelID_delegate_(None, ch, self.delegate)
        if status & 0xFFFFFFFF == IO_ERROR:
            raise BTError("The phone is still busy with an earlier connection. Wait half a minute and try again.")
        if status != 0 or chan is None:
            raise BTError("Can't connect to the phone (error 0x%x). Is it paired and nearby?"
                          % (status & 0xFFFFFFFF))
        self.chan = chan
        self.mtu = max(int(chan.getMTU() or 0), 64)

    def write(self, data):
        for i in range(0, len(data), self.mtu):
            if self.delegate.closed:
                raise ChannelClosed("The phone closed the connection")
            part = data[i:i + self.mtu]
            if self.chan.writeSync_length_(part, len(part)) != 0:
                raise ChannelClosed("Can't send to the phone")

    def read(self, n, timeout=15):
        buf = self.delegate.buf
        if not wait_for(lambda: len(buf) >= n or self.delegate.closed, timeout):
            raise ChannelClosed("The phone did not respond")
        if len(buf) < n:
            raise ChannelClosed("The phone closed the connection")
        out = bytes(buf[:n])
        del buf[:n]
        return out

    def close(self):
        try:
            self.chan.closeChannel()
            self.dev.closeConnection()
        except Exception:  # noqa: BLE001 - closing a dead link must never fail
            pass
        self.delegate.closed = True
