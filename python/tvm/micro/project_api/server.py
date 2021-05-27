"""Defines a basic Project API server template.

This file is meant to be imported or copied into Project API servers, so it should not have any
imports or dependencies outside of things strictly required to run the API server.
"""

import abc
import argparse
import base64
import collections
import enum
import io
import json
import logging
import os
import pathlib
import re
import sys
import textwrap
import traceback
import typing


_LOG = logging.getLogger(__name__)


ProjectOption = collections.namedtuple('ProjectOption', ('name', 'help'))


ServerInfo = collections.namedtuple('ServerInfo', ('platform_name', 'is_template', 'model_library_format_path', 'project_options'))


# Timeouts supported by the underlying C++ MicroSession.
#
# session_start_retry_timeout_sec : float
#     Number of seconds to wait for the device to send a kSessionStartReply after sending the
#     initial session start message. After this time elapses another
#     kSessionTerminated-kSessionStartInit train is sent. 0 disables this.
# session_start_timeout_sec : float
#     Total number of seconds to wait for the session to be established. After this time, the
#     client gives up trying to establish a session and raises an exception.
# session_established_timeout_sec : float
#     Number of seconds to wait for a reply message after a session has been established. 0
#     disables this.
TransportTimeouts = collections.namedtuple(
    "TransportTimeouts",
    [
        "session_start_retry_timeout_sec",
        "session_start_timeout_sec",
        "session_established_timeout_sec",
    ],
)


class ErrorCode(enum.IntEnum):
    """Enumerates error codes which can be returned. Includes JSON-RPC standard and custom codes."""
    # Custom (in reserved error code space).
    SERVER_ERROR = -32000  # A generic error was raised while processing the request.

    # JSON-RPC standard
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class JSONRPCError(Exception):
    """An error class with properties that meet the JSON-RPC error spec."""

    def __init__(self, code, message, data):
        self.code = code
        self.message = message
        self.data = data

    def to_json(self):
        return {"code": self.code,
                "message": self.message,
                "data": self.data,
        }

    def __str__(self):
        data_str = ''
        if self.data:
            if isinstance(self.data, dict) and self.data.get("traceback"):
                data_str = f'\n{self.data["traceback"]}'
            else:
                data_str = f'\n{self.data!r}'
        return f"JSON-RPC error # {self.code}: {self.message}" + data_str


class ServerError(JSONRPCError):

    @classmethod
    def from_exception(cls, exc, **kw):
        to_return = cls(**kw)
        to_return.set_traceback(traceback.TracebackException.from_exception(exc).format())
        return to_return

    def __init__(self, message=None, data=None, client_context=None):
        if message is None:
            message = self.__class__.__name__
        super(ServerError, self).__init__(ErrorCode.SERVER_ERROR, message, data)
        self.client_context = client_context

    def __str__(self):
        context_str = f"{self.client_context}: " if self.client_context is not None else ""
        super_str = super(ServerError, self).__str__()
        return context_str + super_str

    def set_traceback(self, traceback):
        if self.data is None:
            self.data = {}

        if "traceback" not in self.data:
            # NOTE: TVM's FFI layer reorders Python stack traces several times and strips
            # intermediary lines that start with "Traceback". This logic adds a comment to the first
            # stack frame to explicitly identify the first stack frame line that occurs on the server.
            traceback_list = list(traceback)

            # The traceback list contains one entry per stack frame, and each entry contains 1-2 lines:
            #    File "path/to/file", line 123, in <method>:
            #      <copy of the line>
            # We want to place a comment on the first line of the outermost frame to indicate this is the
            # server-side stack frame.
            first_frame_list = traceback_list[1].split('\n')
            self.data["traceback"] = (
                traceback_list[0] +
                f"{first_frame_list[0]}  # <--- Outermost server-side stack frame\n" +
                "\n".join(first_frame_list[1:]) +
                "".join(traceback_list[2:])
                )

    @classmethod
    def from_json(cls, client_context, json_error):
        assert json_error["code"] == ErrorCode.SERVER_ERROR

        for sub_cls in cls.__subclasses__():
            if sub_cls.__name__ == json_error["message"]:
                return sub_cls(message=json_error["message"], data=json_error.get("data"), client_context=client_context)

        return cls(json_error["message"], data=json_error.get("data"), client_context=client_context)


class TransportClosedError(ServerError):
    """Raised when a transport can no longer be used due to underlying I/O problems."""


class IoTimeoutError(ServerError):
    """Raised when the I/O operation could not be completed before the timeout.

    Specifically:
     - when no data could be read before the timeout
     - when some of the write data could be written before the timeout

    Note the asymmetric behavior of read() vs write(), since in one case the total length of the
    data to transfer is known.
    """


class ProjectAPIHandler(metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def server_info_query(self) -> ServerInfo:
        raise NotImplementedError()

    @abc.abstractmethod
    def generate_project(self, model_library_format_path : pathlib.Path, standalone_crt_dir : pathlib.Path, project_dir : pathlib.Path, options : dict):
        """Generate a project from the given artifacts, copying ourselves to that project.

        Parameters
        ----------
        model_library_format_path : pathlib.Path
            Path to the Model Library Format tar archive.
        standalone_crt_dir : pathlib.Path
            Path to the root directory of the "standalone_crt" TVM build artifact. This contains the
            TVM C runtime.
        project_dir : pathlib.Path
            Path to a nonexistent directory which should be created and filled with the generated
            project.
        options : dict
            Dict mapping option name to ProjectOption.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def build(self, options : dict):
        """Build the project, enabling the flash() call to made.

        Parameters
        ----------
        options : Dict[str, ProjectOption]
            ProjectOption which may influence the build, keyed by option name.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def flash(self, options : dict):
        """Program the project onto the device.

        Parameters
        ----------
        options : Dict[str, ProjectOption]
            ProjectOption which may influence the programming process, keyed by option name.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def connect_transport(self, options : dict) -> TransportTimeouts:
        """Connect the transport layer, enabling write_transport and read_transport calls.

        Parameters
        ----------
        options : Dict[str, ProjectOption]
            ProjectOption which may influence the programming process, keyed by option name.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def disconnect_transport(self):
        """Disconnect the transport layer.

        If the transport is not connected, this method is a no-op.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def read_transport(self, n : int, timeout_sec : typing.Union[float, type(None)]) -> int:
        """Read data from the transport

        Parameters
        ----------
        n : int
            Maximum number of bytes to read from the transport.
        timeout_sec : Union[float, None]
            Number of seconds to wait for at least one byte to be written before timing out. The
            transport can wait additional time to account for transport latency or bandwidth
            limitations based on the selected configuration and number of bytes being received. If
            timeout_sec is 0, write should attempt to service the request in a non-blocking fashion.
            If timeout_sec is None, write should block until at least 1 byte of data can be
            returned.

        Returns
        -------
        bytes :
            Data read from the channel. Less than `n` bytes may be returned, but 0 bytes should
            never be returned. If returning less than `n` bytes, the full timeout_sec, plus any
            internally-added timeout, should be waited. If a timeout or transport error occurs,
            an exception should be raised rather than simply returning empty bytes.

        Raises
        ------
        TransportClosedError :
            When the transport layer determines that the transport can no longer send or receive
            data due to an underlying I/O problem (i.e. file descriptor closed, cable removed, etc).

        IoTimeoutError :
            When `timeout_sec` elapses without receiving any data.
        """
        raise NotImplementedError()

    @abc.abstractmethod
    def write_transport(self, data : bytes, timeout_sec : float) -> int:
        """Connect the transport layer, enabling write_transport and read_transport calls.

        Parameters
        ----------
        data : bytes
            The data to write over the channel.
        timeout_sec : Union[float, None]
            Number of seconds to wait for at least one byte to be written before timing out. The
            transport can wait additional time to account for transport latency or bandwidth
            limitations based on the selected configuration and number of bytes being received. If
            timeout_sec is 0, write should attempt to service the request in a non-blocking fashion.
            If timeout_sec is None, write should block until at least 1 byte of data can be
            returned.

        Returns
        -------
        int :
            The number of bytes written to the underlying channel. This can be less than the length
            of `data`, but cannot be 0 (raise an exception instead).

        Raises
        ------
        TransportClosedError :
            When the transport layer determines that the transport can no longer send or receive
            data due to an underlying I/O problem (i.e. file descriptor closed, cable removed, etc).

        IoTimeoutError :
            When `timeout_sec` elapses without receiving any data.
        """
        raise NotImplementedError()



class ProjectAPIServer:
    """Base class for Project API Servers.

    This API server implements communication using JSON-RPC 2.0: https://www.jsonrpc.org/specification

    Suggested use of this class is to import this module or copy this file into Project Generator
    implementations, then instantiate it with server.start().

    This RPC server is single-threaded, blocking, and one-request-at-a-time. Don't get anxious.
    """

    _PROTOCOL_VERSION = 1

    def __init__(self, read_file : typing.BinaryIO, write_file : typing.BinaryIO,
                 handler : ProjectAPIHandler):
        """Initialize a new ProjectAPIServer.

        Parameters
        ----------
        read_file : BinaryIO
            A file-like object used to read binary data from the client.
        write_file : BinaryIO
            A file-like object used to write binary data to the client.
        handler : ProjectAPIHandler
            A class which extends the abstract class ProjectAPIHandler and implements the server RPC
            functions.
        """
        self._read_file = io.TextIOWrapper(read_file, encoding='UTF-8', errors='strict')
        self._write_file = io.TextIOWrapper(write_file, encoding='UTF-8', errors='strict', write_through=True)
        self._handler = handler

    def serve_forever(self):
        """Serve requests until no more are available."""
        has_more = True
        while has_more:
            has_more = self.serve_one_request()

    def serve_one_request(self):
        """Read, process, and reply to a single request from read_file.

        When errors occur reading the request line or loading the request into JSON, they are
        propagated to the caller (the stream is then likely corrupted and no further requests
        should be served. When errors occur past this point, they are caught and send back to the
        client.

        Return
        ----------
        bool :
            True when more data could be read from read_file, False otherwise.
        """
        try:
            line = self._read_file.readline()
            _LOG.debug('read request <- %s', line)
            if not line:
                return False

            request = json.loads(line)

        except EOFError:
            _LOG.error('EOF')
            return False

        except Exception as exc:
            _LOG.error("Caught error reading request", exc_info=1)
            return False

        did_validate = False
        try:
            self._validate_request(request)
            did_validate = True
            self._dispatch_request(request)
        except JSONRPCError as exc:
            exc.set_traceback(traceback.TracebackException.from_exception(exc).format())
            request_id = None if not isinstance(request, dict) else request.get('id')
            self._reply_error(request_id, exc)
            return did_validate
        except Exception as exc:
            message = "validating request"
            if did_validate:
                message = f"calling method {request['method']}"

            exc = ServerError.from_exception(exc, message=message)
            request_id = None if not isinstance(request, dict) else request.get('id')
            self._reply_error(request_id, exc)
            return did_validate

        return True

    VALID_METHOD_RE = re.compile('^[a-zA-Z0-9_]+$')

    def _validate_request(self, request):
        if type(request) is not dict:
            raise ValidationError(f'request: want dict; got {request!r}')

        jsonrpc = request.get('jsonrpc')
        if jsonrpc != "2.0":
            raise JSONRPCError(ErrorCode.INVALID_REQUEST, f'request["jsonrpc"]: want "2.0", got {jsonrpc!r}', None)

        method = request.get('method')
        if type(method) != str:
            raise JSONRPCError(ErrorCode.INVALID_REQUEST, f'request["method"]: want str, got {method!r}', None)

        if not self.VALID_METHOD_RE.match(method):
            raise JSONRPCError(
                ErrorCode.INVALID_REQUEST,
                f'request["method"]: should match regex {self.VALID_METHOD_RE.pattern}, got {method!r}')

        params = request.get('params')
        if type(params) != dict:
            raise JSONRPCError(
                ErrorCode.INVALID_REQUEST,
                f'request["params"]: want dict, got {type(params)}', None)

        request_id = request.get('id')
        if type(request_id) not in (str, int, type(None)):
            raise JSONRPCError(
                ErrorCode.INVALID_REQUEST,
                f'request["id"]: want str, number, null, got: {request_id!r}', None)

    def _dispatch_request(self, request):
        method = request['method']

        interface_method = getattr(ProjectAPIHandler, method)
        if interface_method is None:
            raise JSONRPCError(
                ErrorCode.METHOD_NOT_FOUND, f'{request["method"]}: no such method', None)

        has_preprocessing = True
        dispatch_method = getattr(self, f'_dispatch_{method}', None)
        if dispatch_method is None:
            dispatch_method = getattr(self._handler, method)
            has_preprocessing = False

        request_params = request['params']
        params = {}

        for var_name, var_type in typing.get_type_hints(interface_method).items():
            if var_name == 'self' or var_name == 'return':
                continue

            # NOTE: types can only be JSON-compatible types, so var_type is expected to be of type 'type'.
            if var_name not in request_params:
                raise JSONRPCError(ErrorCode.INVALID_PARAMS, f'method {request["method"]}: parameter {var_name} not given', None)

            param = request_params[var_name]
            if not has_preprocessing and not isinstance(param, var_type):
                raise JSONRPCError(
                    ErrorCode.INVALID_PARAMS,
                    f'method {request["method"]}: parameter {var_name}: want {var_type!r}, got {type(param)!r}', None)

            params[var_name] = param

        extra_params = [p for p in request['params'] if p not in params]
        if extra_params:
            raise JSONRPCError(ErrorCode.INVALID_PARAMS,
                               f'{request["method"]}: extra parameters: {", ".join(extra_params)}',
                               None)

        return_value = dispatch_method(**params)
        self._write_reply(request['id'], result=return_value)

    def _write_reply(self, request_id, result=None, error=None):
        reply_dict = {
            'jsonrpc': "2.0",
            'id': request_id,
        }

        if error is not None:
            assert result is None, f'Want either result= or error=, got result={result!r} and error={error!r})'
            reply_dict["error"] = error
        else:
            reply_dict["result"] = result

        reply_str = json.dumps(reply_dict)
        _LOG.debug('write reply -> %r', reply_dict)
        self._write_file.write(reply_str)
        self._write_file.write('\n')

    def _reply_error(self, request_id, exception):
        self._write_reply(request_id, error=exception.to_json())

    def _dispatch_generate_project(self, model_library_format_path, standalone_crt_dir, project_dir, options):
        return self._handler.generate_project(pathlib.Path(model_library_format_path),
                                              pathlib.Path(standalone_crt_dir),
                                              pathlib.Path(project_dir),
                                              options)

    def _dispatch_server_info_query(self):
        query_reply = self._handler.server_info_query()
        to_return = query_reply._asdict()
        to_return["model_library_format_path"] = str(to_return["model_library_format_path"])
        to_return["protocol_version"] = self._PROTOCOL_VERSION
        to_return["project_options"] = [o._asdict() for o in query_reply.project_options]
        return to_return

    def _dispatch_connect_transport(self, options):
        reply = self._handler.connect_transport(options)
        return {"timeouts": reply._asdict()}

    # def _wrap_io_call(self, call):
    #     try:
    #         return call()
    #     except IoTimeoutError as exc:
    #         return {'error': {'name': 'io_timeout', 'message': str(exc)}}
    #     except TransportClosedError as exc:
    #         return {'error': {'name': 'transport_closed', 'message': str(exc)}}

    def _dispatch_write_transport(self, data, timeout_sec):
        return self._handler.write_transport(base64.b85decode(data), timeout_sec)
#        return self._wrap_io_call(lambda: self._handler.write_transport(base64.b85decode(data), timeout_sec))

    def _dispatch_read_transport(self, n, timeout_sec):
#        def _do_read():
        reply = self._handler.read_transport(n, timeout_sec)
        reply['data'] = str(base64.b85encode(reply['data']), 'utf-8')
        return reply
#
#        return self._wrap_io_call(_do_read)

def main(handler : ProjectAPIHandler, argv : typing.List[str] = None):
    """Start a Project API server.

    Parameters
    ----------
    argv : list[str]
        Command-line parameters to this program. If not given, sys.argv is used.
    handler : ProjectAPIHandler
        Handler class that implements the API server RPC calls.
    """
    if argv is None:
        argv = sys.argv[1:]

    parser = argparse.ArgumentParser(description="Generic TVM Project API server entry point")
    parser.add_argument("--read-fd", type=int, required=True, help="Numeric file descriptor where RPC requests should be read.")
    parser.add_argument("--write-fd", type=int, required=True, help="Numeric file descriptor where RPC replies should be written.")
    parser.add_argument("--debug", action="store_true", help="When given, configure logging at DEBUG level.")
    args = parser.parse_args()

    logging.basicConfig(level='DEBUG' if args.debug else 'INFO',
                        stream=sys.stderr)

    read_file = os.fdopen(args.read_fd, 'rb', buffering=0)
    write_file = os.fdopen(args.write_fd, 'wb', buffering=0)

    server = ProjectAPIServer(read_file, write_file, handler)
    server.serve_forever()
