from . import project_api_server


class ProjectAPIErrorBase(Exception):
    """Base class for all Project API errors."""


class MalformedReplyError(ProjectAPIErrorBase):
    """Raised when the server responds with an invalid reply."""


class MismatchedIdError(ProjectAPIErrorBase):
    """Raised when the reply ID does not match the request."""


class ServerError(ProjectAPIErrorBase):

    def __init__(self, request, error):
        self.request = request
        self.error = error

    def __str__(self):
        return (f"Project API method {request['method']}: server returned error:" "\n"
                f"{self.error}")


class ProjectAPIClient:
    """A client for the Project API."""

    def __init__(self, read_file : typing.BinaryIO, write_fd : typing.BinaryIO):
        self.read_file = io.TextIOWrapper(read_file, encoding='UTF-8', errors='strict')
        self.write_file = io.TextIOWrapper(write_file, encoding='UTF-8', errors='strict')
        self.next_request_id = 1

    def _request_reply(self, method, params):
        request = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": self.next_request_id,
        }
        self.next_request_id += 1

        json.dump(request, self.write_file)
        reply = json.load(self.read_file)

        if reply.get("jsonrpc") != "2.0":
            raise MalformedReplyError(
                f"Server reply should include 'jsonrpc': '2.0'; "
                f"saw jsonrpc={reply.get('jsonrpc')!r}")

        if reply["id"] != request["id"]:
            raise MismatchedIdError(
                f"Reply id ({reply['id']}) does not equal request id ({request['id']}")

        if "error" in reply:
            raise ServerError(request, reply["error"])
        elif "result" not in reply:
            raise MalformedReplyError(f"Expected 'result' key in server reply, got {reply!r}")

        return reply["result"]

    def server_info_query(self):
        return self._request_reply("server_info_query", {})

    def generate_project(self, model_library_format_path : str, standalone_crt_dir : str, project_dir : str):
        return self._request_reply("generate_project", {"model_library_format_path": model_library_format_path,
                                                        "standalone_crt_dir": standalone_crt_dir,
                                                        "project_dir": project_dir})


# NOTE: windows support untested
SERVER_LAUNCH_SCRIPT_FILENAME = f"launch_microtvm_api_server.{'sh' if os.system != 'win32' else '.bat'}"


SERVER_PYTHON_FILENAME = "microtvm_api_server.py"


def instantiate_from_dir(project_dir : str, debug : bool = False):
    """Launch server located in project_dir, and instantiate a Project API Client connected to it."""
    args = None

    launch_script = os.path.join(project_dir, SERVER_LAUNCH_SCRIPT_FILENAME)
    if os.path.exists(launch_script):
        args = [launch_script]

    python_script = os.path.join(project_dir, SERVER_PYTHON_FILENAME)
    if os.path.exists(python_script):
        args = [python_script]

    if args is None:
        raise ProjectAPIServerNotFoundError(
            f"No Project API server found in project directory: {preojct_dir}" "\n"
            f"Tried: {SERVER_LAUNCH_SCRIPT_FILENAME}, {SERVER_PYTHON_FILENAME}")

    api_server_read_fd, tvm_write_fd = os.pipe2()
    tvm_read_fd, api_server_write_fd = os.pipe2()

    api_server_proc = subprocess.Popen(args, bufsize=0, pass_fds=(api_server_read_fd, api_server_write_fd))
    os.close(api_server_read_fd)
    os.close(api_server_write_fd)

    return ProjectAPIClient(os.fdopen(tvm_read_fd, 'rb'), os.fdopen(tvm_write_fd, 'wb'))
