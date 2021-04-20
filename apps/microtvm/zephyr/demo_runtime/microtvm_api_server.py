import fcntl
import logging
import os
import os.path
import pathlib
import re
import select
import shutil
import subprocess
import tarfile
import time
from tvm.micro.project_api import server
from tvm.micro.transport import Transport
from tvm.micro.transport import file_descriptor
from tvm.micro.transport import serial
from tvm.micro.transport import wakeup


_LOG = logging.getLogger(__name__)


API_SERVER_DIR = pathlib.Path(os.path.dirname(__file__) or os.path.getcwd())


BUILD_DIR = API_SERVER_DIR / "build"


MODEL_LIBRARY_FORMAT_RELPATH = "model.tar"


IS_TEMPLATE = not (API_SERVER_DIR / MODEL_LIBRARY_FORMAT_RELPATH).exists()


def check_call(cmd_args, *args, **kwargs):
    cwd_str_ = '' if 'cwd' not in kwargs else f" (in cwd: {kwargs['cwd']}"
    _LOG.info("run%s: %s", cwd_str, " ".join(shlex.quote(a) for a in cmd_args))
    return subprocess.check_call(cmd_args, *args, **kwargs)


CACHE_ENTRY_RE = re.compile(r"(?P<name>[^:]+):(?P<type>[^=]+)=(?P<value>.*)")


CMAKE_BOOL_MAP = dict(
    [(k, True) for k in ("1", "ON", "YES", "TRUE", "Y")]
    + [(k, False) for k in ("0", "OFF", "NO", "FALSE", "N", "IGNORE", "NOTFOUND", "")]
)


def read_cmake_cache():
    """Read a CMakeCache.txt-like file and return a dictionary of values."""
    entries = collections.OrderedDict()
    with open(BUILD_DIR / "CMakeCache.txt", encoding="utf-8") as f:
        for line in f:
            m = CACHE_ENTRY_RE.match(line.rstrip("\n"))
            if not m:
                continue

            if m.group("type") == "BOOL":
                value = CMAKE_BOOL_MAP[m.group("value").upper()]
            else:
                value = m.group("value")

            entries[m.group("name")] = value

    return entries


PROJECT_OPTIONS = [
    server.ProjectOption("verbose", help="Run build with verbose output"),
    server.ProjectOption("west_cmd",
                         help=("Path to the west tool. If given, supersedes both the zephyr_base "
                               "option and ZEPHYR_BASE environment variable.")),
    server.ProjectOption("zephyr_base",
                         help="Path to the zephyr base directory."),
    server.ProjectOption("zephyr_board", help="Name of the Zephyr board to build for"),
]


class Handler(server.ProjectAPIHandler):

    def __init__(self):
        super(Handler, self).__init__()
        self._proc = None

    def server_info_query(self):
        return server.ServerInfo(
            platform_name="zephyr",
            is_template=IS_TEMPLATE,
            model_library_format_path="" if IS_TEMPLATE else (API_SERVER_DIR / MODEL_LIBRARY_FORMAT_RELPATH),
            project_options=PROJECT_OPTIONS)

    # These files and directories will be recursively copied into generated projects from the CRT.
    CRT_COPY_ITEMS = ("include", "Makefile", "src")

    def generate_project(self, model_library_format_path, standalone_crt_dir, project_dir, options):
        project_dir = pathlib.Path(project_dir)
        # Make project directory.
        project_dir.mkdir()

        # Copy ourselves to the generated project. TVM may perform further build steps on the generated project
        # by launching the copy.
        shutil.copy2(__file__, project_dir / os.path.basename(__file__))

        # Place Model Library Format tarball in the special location, which this script uses to decide
        # whether it's being invoked in a template or generated project.
        project_model_library_format_tar_path = project_dir / MODEL_LIBRARY_FORMAT_RELPATH
        shutil.copy2(model_library_format_path, project_model_library_format_tar_path)

        # Extract Model Library Format tarball.into <project_dir>/model.
        extract_path = os.path.splitext(project_model_library_format_tar_path)[0]
        with tarfile.TarFile(project_model_library_format_tar_path) as tf:
            os.makedirs(extract_path)
            tf.extractall(path=extract_path)

        # Populate CRT.
        crt_path = project_dir / "crt"
        crt_path.mkdir()
        for item in self.CRT_COPY_ITEMS:
            src_path = os.path.join(standalone_crt_dir, item)
            dst_path = crt_path / item
            if os.path.isdir(src_path):
                shutil.copytree(src_path, dst_path)
            else:
                shutil.copy2(src_path, dst_path)


        # Populate Makefile.
        shutil.copy2(API_SERVER_DIR / "Makefile", project_dir / "Makefile")

        # Populate crt-config.h
        crt_config_dir = project_dir / "crt_config"
        crt_config_dir.mkdir()
        shutil.copy2(API_SERVER_DIR / ".." / "crt_config-template.h", crt_config_dir / "crt_config.h")

        # Populate src/
        src_dir = project_dir / "src"
        src_dir.mkdir()
        shutil.copy2(API_SERVER_DIR / "main.cc", src_dir / "main.cc")

    def build(self, options):
        build_dir.mkdir()

        cmake_args = ["cmake", ".."]
        if options.get("verbose"):
            cmake_args.append("-DCMAKE_VERBOSE_MAKEFILE:BOOL=TRUE")

        if options.get("zephyr_base"):
            cmake_args.append(f"-DZEPHYR_BASE:STRING={options['zephyr_base']}")

        check_call(cmake_args, cwd=API_SERVER_DIR)

        args = ["make"]
        check_call(args)

    def flash(self, options):
        zephyr_board = options["zephyr_board"]
        self._qemu = "qemu" in zephyr_board

        # For Zephyr boards that run emulated by default but don't have the prefix "qemu_" in their
        # board names, a suffix "-qemu" is added by users of µTVM when specifying the board name to
        # inform that the QEMU transporter must be used just like for the boards with the prefix.
        # Zephyr does not recognize the suffix, so we trim it off before passing it.
        if "-qemu" in zephyr_board:
            zephyr_board = zephyr_board.replace("-qemu", "")

        # The nRF5340DK requires an additional `nrfjprog --recover` before each flash cycle.
        # This is because readback protection is enabled by default when this device is flashed.
        # Otherwise, flashing may fail with an error such as the following:
        #  ERROR: The operation attempted is unavailable due to readback protection in
        #  ERROR: your device. Please use --recover to unlock the device.
        if (
            zephyr_board.startswith("nrf5340dk")
            and self._get_flash_runner(cmake_entries) == "nrfjprog"
        ):
            recover_args = ["nrfjprog", "--recover"]
            recover_args.extend(self._get_nrf_device_args())
            self._subprocess_env.run(recover_args, cwd=build_dir)

        check_call(["make", "flash"], cwd=API_SERVER_DIR / "build")

    def _set_nonblock(self, fd):
        flag = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)
        new_flag = fcntl.fcntl(fd, fcntl.F_GETFL)
        assert (new_flag & os.O_NONBLOCK) != 0, "Cannot set file descriptor {fd} to non-blocking"

    def connect_transport(self, options):

        self._proc = subprocess.Popen([self.BUILD_TARGET], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self._set_nonblock(self._proc.stdin.fileno())
        self._set_nonblock(self._proc.stdout.fileno())
        return server.TransportTimeouts(session_start_retry_timeout_sec=0,
                                        session_start_timeout_sec=0,
                                        session_established_timeout_sec=0)

    def disconnect_transport(self):
        if self._proc is not None:
            proc = self._proc
            self._proc = None
            proc.terminate()
            proc.wait()

    def _await_ready(self, rlist, wlist, timeout_sec=None, end_time=None):
        if timeout_sec is None and end_time is not None:
            timeout_sec = max(0, end_time - time.monotonic())

        rlist, wlist, xlist = select.select(rlist, wlist, rlist + wlist, timeout_sec)
        if not rlist and not wlist and not xlist:
            raise server.IoTimeoutError()

        return True

    def read_transport(self, n, timeout_sec):
        if self._proc is None:
            raise server.TransportClosedError()

        fd = self._proc.stdout.fileno()
        end_time = None if timeout_sec is None else time.monotonic() + timeout_sec

        self._await_ready([fd], [], end_time=end_time)
        to_return = os.read(fd, n)

        if not to_return:
            self.disconnect_transport()
            raise server.TransportClosedError()

        return {"data": to_return}

    def write_transport(self, data, timeout_sec):
        if self._proc is None:
            raise server.TransportClosedError()

        fd = self._proc.stdin.fileno()
        end_time = None if timeout_sec is None else time.monotonic() + timeout_sec

        data_len = len(data)
        while data:
            self._await_ready([], [fd], end_time=end_time)
            num_written = os.write(fd, data)
            if not num_written:
                self.disconnect_transport()
                raise server.TransportClosedError()

            data = data[num_written:]

        return {"bytes_written": data_len}

    def _serial_transport(self, options):
        zephyr_base = options.get("zephyr_base", os.environ["ZEPHYR_BASE"])

        sys.path.insert(0, os.path.join(zephyr_base, "scripts", "dts"))
        try:
            import dtlib  # pylint: disable=import-outside-toplevel
        finally:
            sys.path.pop(0)

        """Instantiate the transport for use with non-QEMU Zephyr."""
        dt_inst = dtlib.DT(
            micro_binary.abspath(micro_binary.labelled_files["device_tree"][0])
        )
        uart_baud = (
            dt_inst.get_node("/chosen")
            .props["zephyr,console"]
            .to_path()
            .props["current-speed"]
            .to_num()
        )
        _LOG.debug("zephyr transport: found UART baudrate from devicetree: %d", uart_baud)

        port_kwargs = self._find_serial_port()
        serial_transport = serial.SerialTransport(
            timeouts=self._serial_timeouts, baudrate=uart_baud, **port_kwargs
        )
        if self._debug_rpc_session is None:
            return serial_transport

        return debug.DebugWrapperTransport(
            debugger.RpcDebugger(
                self._debug_rpc_session,
                debugger.DebuggerFactory(
                    ZephyrDebugger,
                    (
                        " ".join(shlex.quote(x) for x in self._west_cmd),
                        os.path.dirname(micro_binary.abspath(micro_binary.label("cmake_cache")[0])),
                        micro_binary.abspath(micro_binary.debug_files[0]),
                        self._zephyr_base,
                    ),
                    {},
                ),
            ),
            serial_transport,
        )

    def _find_nrf_serial_port(self, cmake_entries):
        com_ports = subprocess.check_output(
            ["nrfjprog", "--com"] + self._get_device_args(cmake_entries), encoding="utf-8"
        )
        ports_by_vcom = {}
        for line in com_ports.split("\n")[:-1]:
            parts = line.split()
            ports_by_vcom[parts[2]] = parts[1]

        return {"port_path": ports_by_vcom["VCOM2"]}

    # kwargs passed to usb.core.find to find attached boards for the openocd flash runner.
    BOARD_USB_FIND_KW = {
        "nucleo_f746zg": {"idVendor": 0x0483, "idProduct": 0x374B},
        "stm32f746g_disco": {"idVendor": 0x0483, "idProduct": 0x374B},
    }

    def openocd_serial(self, cmake_entries):
        """Find the serial port to use for a board with OpenOCD flash strategy."""
        if self._openocd_serial is not None:
            return self._openocd_serial

        if self._autodetected_openocd_serial is None:
            import usb  # pylint: disable=import-outside-toplevel

            find_kw = self.BOARD_USB_FIND_KW[cmake_entries["BOARD"]]
            boards = usb.core.find(find_all=True, **find_kw)
            serials = []
            for b in boards:
                serials.append(b.serial_number)

            if len(serials) == 0:
                raise BoardAutodetectFailed(f"No attached USB devices matching: {find_kw!r}")
            serials.sort()

            self._autodetected_openocd_serial = serials[0]
            _LOG.debug("zephyr openocd driver: autodetected serial %s", serials[0])

        return self._autodetected_openocd_serial

    def _find_openocd_serial_port(self, cmake_entries):
        return {"grep": self.openocd_serial(cmake_entries)}

    def _find_serial_port(self):
        cmake_entries = read_cmake_cache()
        flash_runner = self._get_flash_runner(cmake_entries)

        if flash_runner == "nrfjprog":
            return self._find_nrf_serial_port(cmake_entries)

        if flash_runner == "openocd":
            return self._find_openocd_serial_port(cmake_entries)

        raise FlashRunnerNotSupported(
            f"Don't know how to deduce serial port for flash runner {flash_runner}"
        )

    @classmethod
    def _get_flash_runner(cls, cmake_entries):
        flash_runner = cmake_entries.get("ZEPHYR_BOARD_FLASH_RUNNER")
        if flash_runner is not None:
            return flash_runner

        with open(cmake_entries["ZEPHYR_RUNNERS_YAML"]) as f:
            doc = yaml.load(f, Loader=yaml.FullLoader)
        return doc["flash-runner"]

    def _get_device_args(self, cmake_entries):
        flash_runner = self._get_flash_runner(cmake_entries)

        if flash_runner == "nrfjprog":
            return self._get_nrf_device_args()
        if flash_runner == "openocd":
            return self._get_openocd_device_args(cmake_entries)

        raise BoardError(
            f"Don't know how to find serial terminal for board {cmake_entries['BOARD']} with flash "
            f"runner {flash_runner}"
        )


class QemuStartupFailureError(Exception):
    """Raised when the qemu pipe is not present within startup_timeout_sec."""


class QemuFdTransport(file_descriptor.FdTransport):
    """An FdTransport subclass that escapes written data to accommodate the QEMU monitor.

    It's supposedly possible to disable the monitor, but Zephyr controls most of the command-line
    arguments for QEMU and there are too many options which implictly enable the monitor, so this
    approach seems more robust.
    """

    def write_monitor_quit(self):
        file_descriptor.FdTransport.write(self, b"\x01x", 1.0)

    def close(self):
        file_descriptor.FdTransport.close(self)

    def timeouts(self):
        assert False, "should not get here"

    def write(self, data, timeout_sec):
        """Write data, escaping for QEMU monitor."""
        to_write = bytearray()
        escape_pos = []
        for i, b in enumerate(data):
            if b == 0x01:
                to_write.append(b)
                escape_pos.append(i)
            to_write.append(b)

        num_written = file_descriptor.FdTransport.write(self, to_write, timeout_sec)
        num_written -= sum(1 if x < num_written else 0 for x in escape_pos)
        return num_written


class ZephyrQemuTransport(Transport):
    """The user-facing Zephyr QEMU transport class."""

    def __init__(self, startup_timeout_sec=5.0, **kwargs):
        self.startup_timeout_sec = startup_timeout_sec
        self.kwargs = kwargs
        self.proc = None
        self.fd_transport = None
        self.pipe_dir = None

    def timeouts(self):
        return TransportTimeouts(
            session_start_retry_timeout_sec=2.0,
            session_start_timeout_sec=self.startup_timeout_sec,
            session_established_timeout_sec=5.0,
        )

    def open(self):
        self.pipe_dir = pathlib.Path(tempfile.mkdtemp())
        self.pipe = self.pipe_dir / "fifo"
        self.write_pipe = self.pipe_dir / "fifo.in"
        self.read_pipe = self.pipe_dir / "fifo.out"
        os.mkfifo(self.write_pipe)
        os.mkfifo(self.read_pipe)
        self.proc = subprocess.Popen(
            ["make", "run", f"QEMU_PIPE={self.pipe}"],
            cwd=BUILD_DIR,
            **self.kwargs,
        )
        # NOTE: although each pipe is unidirectional, open both as RDWR to work around a select
        # limitation on linux. Without this, non-blocking I/O can't use timeouts because named
        # FIFO are always considered ready to read when no one has opened them for writing.
        self.fd_transport = wakeup.WakeupTransport(
            QemuFdTransport(
                os.open(self.read_pipe, os.O_RDWR | os.O_NONBLOCK),
                os.open(self.write_pipe, os.O_RDWR | os.O_NONBLOCK),
                self.timeouts(),
            ),
            b"\xfe\xff\xfd\x03\0\0\0\0\0\x02" b"fw",
        )
        self.fd_transport.open()

    def close(self):
        if self.fd_transport is not None:
            self.fd_transport.child_transport.write_monitor_quit()
            self.proc.wait()
            self.fd_transport.close()
            self.fd_transport = None

        if self.proc is not None:
            self.proc = None

        if self.pipe_dir is not None:
            shutil.rmtree(self.pipe_dir)
            self.pipe_dir = None

    def read(self, n, timeout_sec):
        if self.fd_transport is None:
            raise TransportClosedError()
        return self.fd_transport.read(n, timeout_sec)

    def write(self, data, timeout_sec):
        if self.fd_transport is None:
            raise TransportClosedError()
        return self.fd_transport.write(data, timeout_sec)


if __name__ == '__main__':
    server.main(Handler())
