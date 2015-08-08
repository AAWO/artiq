import struct
import logging
import traceback
from enum import Enum
from fractions import Fraction

from artiq.language import core as core_language


logger = logging.getLogger(__name__)


class _H2DMsgType(Enum):
    LOG_REQUEST = 1
    IDENT_REQUEST = 2
    SWITCH_CLOCK = 3

    LOAD_LIBRARY = 4
    RUN_KERNEL = 5

    RPC_REPLY = 6
    RPC_EXCEPTION = 7

    FLASH_READ_REQUEST = 8
    FLASH_WRITE_REQUEST = 9
    FLASH_ERASE_REQUEST = 10
    FLASH_REMOVE_REQUEST = 11


class _D2HMsgType(Enum):
    LOG_REPLY = 1
    IDENT_REPLY = 2
    CLOCK_SWITCH_COMPLETED = 3
    CLOCK_SWITCH_FAILED = 4

    LOAD_COMPLETED = 5
    LOAD_FAILED = 6

    KERNEL_FINISHED = 7
    KERNEL_STARTUP_FAILED = 8
    KERNEL_EXCEPTION = 9

    RPC_REQUEST = 10

    FLASH_READ_REPLY = 11
    FLASH_OK_REPLY = 12
    FLASH_ERROR_REPLY = 13


class UnsupportedDevice(Exception):
    pass


class CommGeneric:
    def __init__(self):
        self._read_type = self._write_type = None
        self._read_length = 0
        self._write_buffer = []

    def open(self):
        """Opens the communication channel.
        Must do nothing if already opened."""
        raise NotImplementedError

    def close(self):
        """Closes the communication channel.
        Must do nothing if already closed."""
        raise NotImplementedError

    def read(self, length):
        """Reads exactly length bytes from the communication channel.
        The channel is assumed to be opened."""
        raise NotImplementedError

    def write(self, data):
        """Writes exactly length bytes to the communication channel.
        The channel is assumed to be opened."""
        raise NotImplementedError

    #
    # Reader interface
    #

    def _read_header(self):
        self.open()

        if self._read_length > 0:
            raise IOError("Read underrun ({} bytes remaining)".
                          format(self._read_length))

        # Wait for a synchronization sequence, 5a 5a 5a 5a.
        sync_count = 0
        while sync_count < 4:
            (sync_byte, ) = struct.unpack("B", self.read(1))
            if sync_byte == 0x5a:
                sync_count += 1
            else:
                sync_count = 0

        # Read message header.
        (self._read_length, ) = struct.unpack(">l", self.read(4))
        if not self._read_length:  # inband connection close
            raise OSError("Connection closed")

        (raw_type, ) = struct.unpack("B", self.read(1))
        self._read_type = _D2HMsgType(raw_type)

        if self._read_length < 9:
            raise IOError("Read overrun in message header ({} remaining)".
                          format(self._read_length))
        self._read_length -= 9

        logger.debug("receiving message: type=%r length=%d",
                     self._read_type, self._read_length)

    def _read_expect(self, ty):
        if self._read_type != ty:
            raise IOError("Incorrect reply from device: {} (expected {})".
                          format(self._read_type, ty))

    def _read_empty(self, ty):
        self._read_header()
        self._read_expect(ty)

    def _read_chunk(self, length):
        if self._read_length < length:
            raise IOError("Read overrun while trying to read {} bytes ({} remaining)"
                          " in packet {}".
                          format(length, self._read_length, self._read_type))

        self._read_length -= length
        return self.read(length)

    def _read_int8(self):
        (value, ) = struct.unpack("B",  self._read_chunk(1))
        return value

    def _read_int32(self):
        (value, ) = struct.unpack(">l", self._read_chunk(4))
        return value

    def _read_int64(self):
        (value, ) = struct.unpack(">q", self._read_chunk(8))
        return value

    def _read_float64(self):
        (value, ) = struct.unpack(">d", self._read_chunk(8))
        return value

    def _read_bytes(self):
        return self._read_chunk(self._read_int32())

    def _read_string(self):
        return self._read_bytes()[:-1].decode('utf-8')

    #
    # Writer interface
    #

    def _write_header(self, ty):
        self.open()

        logger.debug("preparing to send message: type=%r", ty)
        self._write_type   = ty
        self._write_buffer = []

    def _write_flush(self):
        # Calculate message size.
        length = sum([len(chunk) for chunk in self._write_buffer])
        logger.debug("sending message: type=%r length=%d", self._write_type, length)

        # Write synchronization sequence, header and body.
        self.write(struct.pack(">llB", 0x5a5a5a5a,
                                       9 + length, self._write_type.value))
        for chunk in self._write_buffer:
            self.write(chunk)

    def _write_empty(self, ty):
        self._write_header(ty)
        self._write_flush()

    def _write_chunk(self, chunk):
        self._write_buffer.append(chunk)

    def _write_int8(self, value):
        self._write_buffer.append(struct.pack("B", value))

    def _write_int32(self, value):
        self._write_buffer.append(struct.pack(">l", value))

    def _write_int64(self, value):
        self._write_buffer.append(struct.pack(">q", value))

    def _write_float64(self, value):
        self._write_buffer.append(struct.pack(">d", value))

    def _write_bytes(self, value):
        self._write_int32(len(value))
        self._write_buffer.append(value)

    def _write_string(self, value):
        self._write_bytes(value.encode("utf-8") + b"\0")

    #
    # Exported APIs
    #

    def reset_session(self):
        self.write(struct.pack(">ll", 0x5a5a5a5a, 0))

    def check_ident(self):
        self._write_empty(_H2DMsgType.IDENT_REQUEST)

        self._read_header()
        self._read_expect(_D2HMsgType.IDENT_REPLY)
        runtime_id = self._read_chunk(4)
        if runtime_id != b"AROR":
            raise UnsupportedDevice("Unsupported runtime ID: {}"
                                    .format(runtime_id))

    def switch_clock(self, external):
        self._write_header(_H2DMsgType.SWITCH_CLOCK)
        self._write_int8(external)
        self._write_flush()

        self._read_empty(_D2HMsgType.CLOCK_SWITCH_COMPLETED)

    def get_log(self):
        self._write_empty(_H2DMsgType.LOG_REQUEST)

        self._read_header()
        self._read_expect(_D2HMsgType.LOG_REPLY)
        return self._read_chunk(self._read_length).decode('utf-8')

    def flash_storage_read(self, key):
        self._write_header(_H2DMsgType.FLASH_READ_REQUEST)
        self._write_string(key)
        self._write_flush()

        self._read_header()
        self._read_expect(_D2HMsgType.FLASH_READ_REPLY)
        return self._read_chunk(self._read_length)

    def flash_storage_write(self, key, value):
        self._write_header(_H2DMsgType.FLASH_WRITE_REQUEST)
        self._write_string(key)
        self._write_bytes(value)
        self._write_flush()

        self._read_header()
        if self._read_type == _D2HMsgType.FLASH_ERROR_REPLY:
            raise IOError("Flash storage is full")
        else:
            self._read_expect(_D2HMsgType.FLASH_OK_REPLY)

    def flash_storage_erase(self):
        self._write_empty(_H2DMsgType.FLASH_ERASE_REQUEST)

        self._read_empty(_D2HMsgType.FLASH_OK_REPLY)

    def flash_storage_remove(self, key):
        self._write_header(_H2DMsgType.FLASH_REMOVE_REQUEST)
        self._write_string(key)
        self._write_flush()

        self._read_empty(_D2HMsgType.FLASH_OK_REPLY)

    def load(self, kernel_library):
        self._write_header(_H2DMsgType.LOAD_LIBRARY)
        self._write_chunk(kernel_library)
        self._write_flush()

        self._read_empty(_D2HMsgType.LOAD_COMPLETED)

    def run(self):
        self._write_empty(_H2DMsgType.RUN_KERNEL)
        logger.debug("running kernel")

    _rpc_sentinel = object()

    def _receive_rpc_value(self, rpc_map):
        tag = chr(self._read_int8())
        if tag == "\x00":
            return self._rpc_sentinel
        elif tag == "t":
            length = self._read_int8()
            return tuple(self._receive_rpc_value(rpc_map) for _ in range(length))
        elif tag == "n":
            return None
        elif tag == "b":
            return bool(self._read_int8())
        elif tag == "i":
            return self._read_int32()
        elif tag == "I":
            return self._read_int64()
        elif tag == "f":
            return self._read_float64()
        elif tag == "F":
            numerator   = self._read_int64()
            denominator = self._read_int64()
            return Fraction(numerator, denominator)
        elif tag == "s":
            return self._read_string()
        elif tag == "l":
            length = self._read_int32()
            return [self._receive_rpc_value(rpc_map) for _ in range(length)]
        elif tag == "r":
            lower = self._receive_rpc_value(rpc_map)
            upper = self._receive_rpc_value(rpc_map)
            step  = self._receive_rpc_value(rpc_map)
            return range(lower, upper, step)
        elif tag == "o":
            return rpc_map[self._read_int32()]
        else:
            raise IOError("Unknown RPC value tag: {}".format(repr(tag)))

    def _receive_rpc_args(self, rpc_map):
        args = []
        while True:
            value = self._receive_rpc_value(rpc_map)
            if value is self._rpc_sentinel:
                return args
            args.append(value)

    def _serve_rpc(self, rpc_map):
        service = self._read_int32()
        args = self._receive_rpc_args(rpc_map)
        logger.debug("rpc service: %d %r", service, args)

        try:
            result = rpc_map[service](*args)
            if not isinstance(result, int) or not (-2**31 < result < 2**31-1):
                raise ValueError("An RPC must return an int(width=32)")
        except ARTIQException as exn:
            logger.debug("rpc service: %d %r ! %r", service, args, exn)

            self._write_header(_H2DMsgType.RPC_EXCEPTION)
            self._write_string(exn.name)
            self._write_string(exn.message)
            for index in range(3):
                self._write_int64(exn.param[index])

            self._write_string(exn.filename)
            self._write_int32(exn.line)
            self._write_int32(exn.column)
            self._write_string(exn.function)

            self._write_flush()
        except Exception as exn:
            logger.debug("rpc service: %d %r ! %r", service, args, exn)

            self._write_header(_H2DMsgType.RPC_EXCEPTION)
            self._write_string(type(exn).__name__)
            self._write_string(str(exn))
            for index in range(3):
                self._write_int64(0)

            ((filename, line, function, _), ) = traceback.extract_tb(exn.__traceback__)
            self._write_string(filename)
            self._write_int32(line)
            self._write_int32(-1) # column not known
            self._write_string(function)

            self._write_flush()
        else:
            logger.debug("rpc service: %d %r == %r", service, args, result)

            self._write_header(_H2DMsgType.RPC_REPLY)
            self._write_int32(result)
            self._write_flush()

    def _serve_exception(self):
        name      = self._read_string()
        message   = self._read_string()
        params    = [self._read_int64() for _ in range(3)]

        filename  = self._read_string()
        line      = self._read_int32()
        column    = self._read_int32()
        function  = self._read_string()

        backtrace = [self._read_int32() for _ in range(self._read_int32())]
        # we don't have debug information yet.
        # print("exception backtrace:", [hex(x) for x in backtrace])

        raise core_language.ARTIQException(name, message, params,
                                           filename, line, column, function)

    def serve(self, rpc_map):
        while True:
            self._read_header()
            if self._read_type == _D2HMsgType.RPC_REQUEST:
                self._serve_rpc(rpc_map)
            elif self._read_type == _D2HMsgType.KERNEL_EXCEPTION:
                self._serve_exception()
            else:
                self._read_expect(_D2HMsgType.KERNEL_FINISHED)
                return
