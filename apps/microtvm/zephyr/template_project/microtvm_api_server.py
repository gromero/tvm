import collections
import fcntl
import logging
import os
import os.path
import pathlib
import re
import select
import shlex
import shutil
import subprocess
import tarfile
import tempfile
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
    cwd_str = '' if 'cwd' not in kwargs else f" (in cwd: {kwargs['cwd']})"
    _LOG.info("run%s: %s", cwd_str, " ".join(shlex.quote(a) for a in cmd_args))
    return subprocess.check_call(cmd_args, *args, **kwargs)


CACHE_ENTRY_RE = re.compile(r"(?P<name>[^:]+):(?P<type>[^=]+)=(?P<value>.*)")


CMAKE_BOOL_MAP = dict(
    [(k, True) for k in ("1", "ON", "YES", "TRUE", "Y")]
    + [(k, False) for k in ("0", "OFF", "NO", "FALSE", "N", "IGNORE", "NOTFOUND", "")]
)


class CMakeCache(collections.Mapping):

    def __init__(self, path):
        self._path = path
        self._dict = None

    def __iter__(self):
        return iter(self._dict)

    def __getitem__(self, key):
        if self._dict is None:
            self._read_cmake_cache()

        return self._dict[key]

    def __len__(self):
        return len(self._dict)

    def _read_cmake_cache(self):
        """Read a CMakeCache.txt-like file and return a dictionary of values."""
        entries = collections.OrderedDict()
        with open(self._path, encoding="utf-8") as f:
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


CMAKE_CACHE = CMakeCache(BUILD_DIR / "CMakeCache.txt")


class BoardError(Exception):
    """Raised when an attached board cannot be opened (i.e. missing /dev nodes, etc)."""


class BoardAutodetectFailed(Exception):
    """Raised when no attached hardware is found matching the board= given to ZephyrCompiler."""


def _get_flash_runner():
    flash_runner = CMAKE_CACHE.get("ZEPHYR_BOARD_FLASH_RUNNER")
    if flash_runner is not None:
        return flash_runner

    with open(CMAKE_CACHE["ZEPHYR_RUNNERS_YAML"]) as f:
        doc = yaml.load(f, Loader=yaml.FullLoader)
    return doc["flash-runner"]


def _get_device_args(options, cmake_entries):
    flash_runner = _get_flash_runner()

    if flash_runner == "nrfjprog":
        return _get_nrf_device_args()

    if flash_runner == "openocd":
        return _get_openocd_device_args(options)

    raise BoardError(
        f"Don't know how to find serial terminal for board {CMAKE_CACHE['BOARD']} with flash "
        f"runner {flash_runner}")

# kwargs passed to usb.core.find to find attached boards for the openocd flash runner.
BOARD_USB_FIND_KW = {
    "nucleo_f746zg": {"idVendor": 0x0483, "idProduct": 0x374B},
    "stm32f746g_disco": {"idVendor": 0x0483, "idProduct": 0x374B},
}

def openocd_serial(options):
    """Find the serial port to use for a board with OpenOCD flash strategy."""
    if "openocd_serial" in options:
        return options["openocd_serial"]

    import usb  # pylint: disable=import-outside-toplevel

    find_kw = BOARD_USB_FIND_KW[CMAKE_CACHE["BOARD"]]
    boards = usb.core.find(find_all=True, **find_kw)
    serials = []
    for b in boards:
        serials.append(b.serial_number)

    if len(serials) == 0:
        raise BoardAutodetectFailed(f"No attached USB devices matching: {find_kw!r}")
    serials.sort()

    autodetected_openocd_serial = serials[0]
    _LOG.debug("zephyr openocd driver: autodetected serial %s", serials[0])

    return autodetected_openocd_serial


def _get_openocd_device_args(options):
    return ["--serial", openocd_serial(options)]


def _get_nrf_device_args(options):
    nrfjprog_args = ["nrfjprog", "--ids"]
    nrfjprog_ids = subprocess.check_output(nrfjprog_args, encoding="utf-8")
    if not nrfjprog_ids.strip("\n"):
        raise BoardAutodetectFailed(
            f'No attached boards recognized by {" ".join(nrfjprog_args)}'
        )

    boards = nrfjprog_ids.split("\n")[:-1]
    if len(boards) > 1:
        if options['nrfjprog_snr'] is None:
            raise BoardError(
                "Multiple boards connected; specify one with nrfjprog_snr=: "
                f'{", ".join(boards)}'
            )

        if str(options['nrfjprog_snr']) not in boards:
            raise BoardError(
                f"nrfjprog_snr ({options['nrfjprog_snr']}) not found in {nrfjprog_args}: {boards}"
            )

        return ["--snr", options['nrfjprog_snr']]

    if not boards:
        return []

    return ["--snr", boards[0]]


PROJECT_OPTIONS = [
    server.ProjectOption("gdbserver_port", help=("If given, port number to use when running the "
                                                 "local gdbserver")),
    server.ProjectOption("openocd_serial", help=("When used with OpenOCD targets, serial # of the "
                                                 "attached board to use")),
    server.ProjectOption("nrfjprog_snr", help=("When used with nRF targets, serial # of the "
                                               "attached board to use, from nrfjprog")),
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

        if self._is_qemu(options):
            shutil.copytree(API_SERVER_DIR / "qemu-hack", project_dir / "qemu-hack")

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
        shutil.copy2(API_SERVER_DIR / "CMakeLists.txt", project_dir / "CMakeLists.txt")
        shutil.copy2(API_SERVER_DIR / "prj.conf", project_dir / "prj.conf")

        # Populate per board configs.
        boards_dir = project_dir / "boards"
        boards_dir.mkdir()
        shutil.copytree(API_SERVER_DIR / "boards", boards_dir, dirs_exist_ok=True)

        # Populate crt-config.h
        crt_config_dir = project_dir / "crt_config"
        crt_config_dir.mkdir()
        shutil.copy2(API_SERVER_DIR / "crt_config" / "crt_config.h", crt_config_dir / "crt_config.h")

        # Populate src/
        src_dir = project_dir / "src"
        src_dir.mkdir()
        shutil.copy2(API_SERVER_DIR / "src" / "main.c", src_dir / "main.c")

    def build(self, options):
        BUILD_DIR.mkdir()

        cmake_args = ["cmake", ".."]
        if options.get("verbose"):
            cmake_args.append("-DCMAKE_VERBOSE_MAKEFILE:BOOL=TRUE")

        if options.get("zephyr_base"):
            cmake_args.append(f"-DZEPHYR_BASE:STRING={options['zephyr_base']}")

        cmake_args.append(f"-DBOARD:STRING={options['zephyr_board']}")

        check_call(cmake_args, cwd=BUILD_DIR)

        args = ["make", "-j2"]
        if options.get("verbose"):
            args.append("VERBOSE=1")
        check_call(args, cwd=BUILD_DIR)

    @classmethod
    def _is_qemu(cls, options):
        return "qemu" in options["zephyr_board"]

    def flash(self, options):
        if self._is_qemu(options):
            return  # NOTE: qemu requires no flash step--it is launched from connect_transport.

        zephyr_board = options["zephyr_board"]

        # The nRF5340DK requires an additional `nrfjprog --recover` before each flash cycle.
        # This is because readback protection is enabled by default when this device is flashed.
        # Otherwise, flashing may fail with an error such as the following:
        #  ERROR: The operation attempted is unavailable due to readback protection in
        #  ERROR: your device. Please use --recover to unlock the device.
        if zephyr_board.startswith("nrf5340dk") and _get_flash_runner() == "nrfjprog":
            recover_args = ["nrfjprog", "--recover"]
            recover_args.extend(_get_nrf_device_args())
            self._subprocess_env.run(recover_args, cwd=build_dir)

        check_call(["make", "flash"], cwd=API_SERVER_DIR / "build")

    def _connect_qemu_transport(self, options):
        zephyr_board = options["zephyr_board"]
        # For Zephyr boards that run emulated by default but don't have the prefix "qemu_" in their
        # board names, a suffix "-qemu" is added by users of µTVM when specifying the board name to
        # inform that the QEMU transporter must be used just like for the boards with the prefix.
        # Zephyr does not recognize the suffix, so we trim it off before passing it.
        if "-qemu" in zephyr_board:
            zephyr_board = zephyr_board.replace("-qemu", "")

        self._transport = ZephyrQemuTransport(options)
        return self._transport.open()

    def connect_transport(self, options):
        if self._is_qemu(options):
            return self._connect_qemu_transport(options)

        self._proc = subprocess.Popen([self.BUILD_TARGET], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        _set_nonblock(self._proc.stdin.fileno())
        _set_nonblock(self._proc.stdout.fileno())
        return server.TransportTimeouts(session_start_retry_timeout_sec=0,
                                        session_start_timeout_sec=0,
                                        session_established_timeout_sec=0)

    def disconnect_transport(self):
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def read_transport(self, n, timeout_sec):
        if self._transport is None:
            raise server.TransportClosedError()

        # if not hasattr(self, '_first_read'):
        #     read_buf = bytearray()
        #     while b"\xfe\xff\xfd\x03\0\0\0\0\0\x02" b"fw" not in read_buf:
        #         read_buf.extend(self._transport.read(10, 1.0))
        #         print('got', read_buf)

        return self._transport.read(n, timeout_sec)

    def write_transport(self, data, timeout_sec):
        if self._transport is None:
            raise server.TransportClosedError()

        return self._transport.write(data, timeout_sec)


def _set_nonblock(fd):
    flag = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flag | os.O_NONBLOCK)
    new_flag = fcntl.fcntl(fd, fcntl.F_GETFL)
    assert (new_flag & os.O_NONBLOCK) != 0, "Cannot set file descriptor {fd} to non-blocking"


class ZephyrSerialTransport:

    @classmethod
    def _lookup_baud_rate(cls, options):
        zephyr_base = options.get("zephyr_base", os.environ["ZEPHYR_BASE"])
        sys.path.insert(0, os.path.join(zephyr_base, "scripts", "dts"))
        try:
            import dtlib  # pylint: disable=import-outside-toplevel
        finally:
            sys.path.pop(0)

        dt_inst = dtlib.DT(BUILD_DIR / "zephyr" / "zephyr.dts")
        uart_baud = (
            dt_inst.get_node("/chosen")
            .props["zephyr,console"]
            .to_path()
            .props["current-speed"]
            .to_num()
        )
        _LOG.debug("zephyr transport: found UART baudrate from devicetree: %d", uart_baud)

        return uart_baud

    @classmethod
    def _find_nrf_serial_port(cls):
        com_ports = subprocess.check_output(
            ["nrfjprog", "--com"] + _get_device_args(), encoding="utf-8"
        )
        ports_by_vcom = {}
        for line in com_ports.split("\n")[:-1]:
            parts = line.split()
            ports_by_vcom[parts[2]] = parts[1]

        return {"port_path": ports_by_vcom["VCOM2"]}

    @classmethod
    def _find_openocd_serial_port(cls, options):
        return {"grep": openocd_serial(options)}

    @classmethod
    def _find_serial_port(cls, options):
        flash_runner = _get_flash_runner()

        if flash_runner == "nrfjprog":
            return cls._find_nrf_serial_port()

        if flash_runner == "openocd":
            return cls._find_openocd_serial_port(options)

        raise FlashRunnerNotSupported(
            f"Don't know how to deduce serial port for flash runner {flash_runner}"
        )

    def __init__(self, options):
        port_kw = self._find_serial_port()
        port_kw['baudrate'] = self._lookup_baud_rate(options)
        self._port = serial.Serial(**port_kw)

    def read(self, n, timeout_sec):
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

    def write(self, data, timeout_sec):
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


class ZephyrQemuTransport:
    """The user-facing Zephyr QEMU transport class."""

    def __init__(self, options):
        self.options = options
        self.proc = None
        self.pipe_dir = None
        self.read_fd = None
        self.write_fd = None

    def open(self):
        self.pipe_dir = pathlib.Path(tempfile.mkdtemp())
        self.pipe = self.pipe_dir / "fifo"
        self.write_pipe = self.pipe_dir / "fifo.in"
        self.read_pipe = self.pipe_dir / "fifo.out"
        os.mkfifo(self.write_pipe)
        os.mkfifo(self.read_pipe)

        if "gdbserver_port" in self.options:
            if "env" in self.kwargs:
                self.kwargs["env"] = copy.copy(self.kwargs["env"])
            else:
                self.kwargs["env"] = os.environ.copy()

            self.kwargs["env"]["TVM_QEMU_GDBSERVER_PORT"] = str(self.options["gdbserver_port"])

        self.proc = subprocess.Popen(
            ["make", "run", f"QEMU_PIPE={self.pipe}"],
            cwd=BUILD_DIR,
        )
        # NOTE: although each pipe is unidirectional, open both as RDWR to work around a select
        # limitation on linux. Without this, non-blocking I/O can't use timeouts because named
        # FIFO are always considered ready to read when no one has opened them for writing.
        self.read_fd = os.open(self.read_pipe, os.O_RDWR | os.O_NONBLOCK)
        self.write_fd = os.open(self.write_pipe, os.O_RDWR | os.O_NONBLOCK)
        _set_nonblock(self.read_fd)
        _set_nonblock(self.write_fd)

        return server.TransportTimeouts(
            session_start_retry_timeout_sec=2.0,
            session_start_timeout_sec=5.0,
            session_established_timeout_sec=5.0,
        )

    def close(self):
        did_write = False
        if self.write_fd:
            try:
                self.write(b"\x01x", 1.0)  # Use a short timeout since we will kill the process
                did_write = True
            except server.IoTimeoutError:
                pass
            os.close(self.write_fd)

        if self.proc:
            if not did_write:
                self.proc.terminate()
            try:
                self.proc.wait(5.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()

        if self.read_fd:
            os.close(self.read_fd)

        if self.pipe_dir is not None:
            shutil.rmtree(self.pipe_dir)
            self.pipe_dir = None

    def read(self, n, timeout_sec):
        return server.read_with_timeout(self.read_fd, n, timeout_sec)

    def write(self, data, timeout_sec):
        to_write = bytearray()
        escape_pos = []
        for i, b in enumerate(data):
            if b == 0x01:
                to_write.append(b)
                escape_pos.append(i)
            to_write.append(b)

        num_written = server.write_with_timeout(self.write_fd, to_write, timeout_sec)
        num_written -= sum(1 if x < num_written else 0 for x in escape_pos)
        return num_written


if __name__ == '__main__':
    server.main(Handler())
