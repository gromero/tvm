import io
import sys

import pytest

from tvm.micro import project_api


class BaseTestHandler(project_api.server.ProjectAPIHandler):

    DEFAULT_TEST_SERVER_INFO = project_api.server.ServerInfo(
        platform_name='platform_name',
        is_template=True,
        model_library_format_path="./model-library-format-path.sh",
        project_options=[project_api.server.ProjectOption(name="foo", help="Option foo"),
                         project_api.server.ProjectOption(name="bar", help="Option bar"),
                         ])

    # Overridable by test case.
    TEST_SERVER_INFO = None

    def server_info_query(self):
        return self.TEST_SERVER_INFO if self.TEST_SERVER_INFO is not None else self.DEFAULT_TEST_SERVER_INFO

    def generate_project(self, model_library_format_path, crt_path, project_path, options):
        assert False, "generate_project is not implemented for this test"

    def build(self, options):
        assert False, "build is not implemented for this test"

    def flash(self, options):
        assert False, "flash is not implemented for this test"

    def connect_transport(self, options):
        assert False, "connect_transport is not implemented for this test"

    def disconnect_transport(self, options):
        assert False, "disconnect_transport is not implemented for this test"

    def read_transport(self, n, timeout_sec):
        assert False, "read_transport is not implemented for this test"

    def write_transport(self, data, timeout_sec):
        assert False, "write_transport is not implemented for this test"


class Transport:

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return False

    closed = False

    def __init__(self):
        self.data = bytearray()
        self.rpos = 0

        self.items = []

    def read(self, size=-1):
        to_read = len(self.data) - 1 - self.rpos
        if size != -1:
            to_read = min(size, to_read)

        rpos = self.rpos
        self.rpos += to_read
        return self.data[rpos:self.rpos]

    def write(self, data):
        self.data.extend(data)


def create_test_client_server(handler):
    client_to_server = Transport()
    server_to_client = Transport()

    server = project_api.server.ProjectAPIServer(client_to_server, server_to_client, handler)
    def process_server_request():
        assert server.serve_one_request(), "Server failed to process request"

    client = project_api.client.ProjectAPIClient(server_to_client, client_to_server,
                                                 testonly_did_write_request=process_server_request)

    return client, server


def test_server_info_query():
    handler = BaseTestHandler()
    client, server = create_test_client_server(handler)

    reply = client.server_info_query()
    assert reply['protocol_version'] == 1
    assert reply['platform_name'] == 'platform_name'
    assert reply['is_template'] == True
    assert reply['model_library_format_path'] == './model-library-format-path.sh'
    assert reply['project_options'] == [{"name": 'foo', 'help': 'Option foo'}, {'name': 'bar', 'help': 'Option bar'}]



if __name__ == '__main__':
    sys.exit(pytest.main([__file__] + sys.argv[1:]))
