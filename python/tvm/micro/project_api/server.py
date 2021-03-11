"""Defines a basic Project API server template.

This file is meant to be imported or copied into Project API servers, so it should not have any
imports or dependencies outside of things strictly required to run the API server.
"""

import abc
import io
import json
import sys
import textwrap
import traceback
import typing


ProjectOption = collections.namedtuple('ProjectOption', ('name', 'help'))


ServerInfo = collections.namedtuple('ServerInfo', ('platform_name', 'is_template', 'model_library_format_path', 'project_options'))


class JSONRPCError(Exception):
    """An error class with properties that meet the JSON-RPC error spec."""

    def __init__(self, code, message, data):
        self.code = code
        self.message = message
        self.data = data

    def __str__(self):
        return f"JSON-RPC error # {self.code}: {self.message}\n{self.data!r}"



class ProjectAPIHandler(metaclass=abc.ABCMeta):

    @abc.abstractmethod
    def server_info_query(self) -> ServerInfo:
        raise NotImplementedError()

    @abc.abstractmethod
    def generate_project(self, model_library_format_path : str, crt_path : str, project_path : str):
        """Generate a project from the given artifacts, copying ourselves to that project.

        Parameters
        ----------
        model_library_format_path : str
            Path to the Model Library Format tar archive.
        crt_path : str
            Path to the root directory of the "standalone_crt" TVM build artifact. This contains the
            TVM C runtime.
        project_path : str
            Path to a nonexistent directory which should be created and filled with the generated
            project.
        """
        raise NotImplementedError()


class ProjectAPIServer:
    """Base class for Project API Servers.

    This API server implements communication using JSON-RPC 2.0: https://www.jsonrpc.org/specification

    Suggested use of this class is to import this module or copy this file into Project Generator
    implementations, then instantiate it with server.start().

    This RPC server is single-threaded, blocking, and one-request-at-a-time. Don't get anxious.
    """

    def __init__(self, read_file : typing.BinaryIO, write_fd : typing.BinaryIO,
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
        self._write_file = io.TextIOWrapper(write_file, encoding='UTF-8')

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
            if not line:
                return False

            request = json.loads(line)

        except EOFError:
            return False

        except Exception as exc:
            _LOG.error("Caught error reading request", exc_info=1)
            return False

        try:
            self._validate_request(request)
        except ValidationError as exc:
            request_id = None if not isinstance(request, dict) else request.get('id')
            self._reply_error(request_id, exc)
            return False

        try:
            self._dispatch_request(request)
        except Exception as exc:
            self._reply_error(request["id"], exc)
            return True

        return True

    VALID_METHOD_RE = re.compile('^[a-zA-Z0-9_]+$')

    def _validate_request(self, request):
        if type(request) is not dict:
            raise ValidationError(f'request: want dict; got {request!r}')

        jsonrpc = request.get('jsonrpc')
        if jsonrpc != "2.0":
            raise ValidationError(f'request["jsonrpc"]: want "2.0", got {jsonrpc!r}')

        method = request.get('method')
        if type(method) != str:
            raise ValidationError(f'request["method"]: want str, got {method!r}')

        if not self.VALID_METHOD_RE.match(method):
            raise ValidationError(
              f'request["method"]: should match regex {self.VALID_METHOD_RE.pattern}, got {method!r}')

        params = request.get('params')
        if type(params) != dict:
            raise ValidationError(f'request["params"]: want dict, got {type(params)}')

        request_id = request.get('id')
        if type(request_id) not in (str, int, type(None)):
            raise ValidationError(f'request["id"]: want str, number, null, got: {request_id!r}')

    def _dispatch_request(self, request):
        method = request['method']
        dispatch_method = getattr(self, method, None)
        if dispatch_method is None:
            dispatch_method = getattr(self.handler, '_dispatch_%s' % method, None)
            if dispatch_method is None:
                raise ValidationError(f'request["method"]: no such method')

        type_hints = typing.get_type_hints(dispatch_method)
        request_params = request['params']
        params = {}
        for var_name, var_type in type_hints.items():
            if var_name == 'self':
                continue

            # NOTE: types can only be JSON-compatible types, so var_type is expected to be of type 'type'.
            if 'var_name' not in request_params:
                raise ValidationError(f'method {request["method"]}: parameter {var_name} not given')

            param = request_params[var_name]
            if not isinstance(param, var_type):
                raise ValidationError(
                    f'method {request["method"]}: parameter {var_name}: want {var_type!r}, got {param!r}')

            params[var_name] = param

        extra_params = (p for p in request['params'] if p not in params)
        if extra_params:
            raise ValidationError(f'{request["method"]}: extra parameters: {", ".join(extra_params)}')

        try:
            return_value = dispatch_method(**params)
        except Exception as exc:
            self._reply_error(request['id'], exc)

        self._write_reply(request['id'], result=return_value)

    def _write_reply(self, request_id, result=None, error=None):
        reply_dict = {
            'jsonrpc': "2.0",
            'id': request_id,
        }

        if result is not None and error is None:
            reply_dict["result"] = result
        elif result is None and error is None:
            reply_dict["error"] = error
        else:
            assert False, f'Want either result= or error=, got result={result!r} and error={error!r})'

        json.dump(reply_dict, self.write_file)

    def _write_error(self, request_id, exception):
        self._write_reply(request_id, '\n'.join(traceback.format_exc(exception)))

    def _dispatch_server_info_query(self):
        query_reply = self._handler.server_info_query()
        to_return = dict(vars(query_reply))
        to_return["protocol_version"] = self._PROTOCOL_VERSION
        return to_return


def _print_usage_and_exit(argv):
    print(textwrap.dedent(f"""
        Usage: {argv[0]} --read-fd <read_fd> --write-fd <write_fd> [--debug]

        Parameters
        ----------
        --read-fd <read_fd>
             An integer file descriptor from which RPC commands will be read.
        --write-rd <write_fd>
             An integer file descriptor to which RPC replied will be written.
        --debug
             If supplied, log at DEBUG level to stderr.""", file=sys.stderr))


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
    # Intentionally we avoid argparse here to ensure other language are not over-burdened parsing
    # command-line arguments.
    read_fd = None
    write_fd = None
    debug = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--debug":
            debug = True
        elif arg == "--read-fd":
            if len(argv) == i + 1:
                _print_usage_and_exit(argv)

            read_fd = int(argv[i + 1])
            i += 1

        elif arg == "--write-fd":
            if len(argv) == i + 1:
                print_usage_and_exit(argv)

            write_fd = int(argv[i + 1])
            i += 1

        else:
            _print_usage_and_exit(argv)

    logging.basicConfig(level='DEBUG' if debug else 'INFO',
                        stream=sys.stderr)

    read_file = os.fdopen(read_fd, 'rb')
    write_file = os.fdopen(write_fd, 'wb')

    server = ProjectAPIServer(read_file, write_file, handler)
    server.serve_forever()
