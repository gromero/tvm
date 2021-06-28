"""Defines glue wrappers around the Project API which mate to TVM interfaces."""

from ..contrib import utils
from .build import get_standalone_crt_dir
from .model_library_format import export_model_library_format
from .project_api import client
from .transport import Transport, TransportTimeouts


class ProjectTransport(Transport):

    def __init__(self, client, options):
        self._client = client
        self._options = options
        self._timeouts = None

    def timeouts(self):
        assert self._timeouts is not None, "Transport not yet opened"
        return self._timeouts

    def open(self):
        reply = self._client.connect_transport(self._options)
        self._timeouts = TransportTimeouts(**reply["timeouts"])

    def close(self):
        if not self._client.shutdown:
            self._client.disconnect_transport()

    def write(self, data, timeout_sec):
        return self._client.write_transport(data, timeout_sec)["bytes_written"]

    def read(self, n, timeout_sec):
        return self._client.read_transport(n, timeout_sec)["data"]


class TemplateProjectError(Exception):
    """Raised when the Project API server given to GeneratedProject reports is_template=True."""


class GeneratedProject:
    """Defines a glue interface to interact with a generated project through the API server."""

    @classmethod
    def from_directory(cls, project_dir, options):
        return cls(client.instantiate_from_dir(project_dir), options)

    def __init__(self, client, options):
        self._client = client
        self._options = options
        self._info = self._client.server_info_query()
        if self._info['is_template']:
            raise TemplateProjectError()

    def build(self):
        self._client.build(self._options)

    def flash(self):
        self._client.flash(self._options)

    def transport(self):
        return ProjectTransport(self._client, self._options)


class NotATemplateProjectError(Exception):
    """Raised when the Project API server given to TemplateProject reports is_template=false."""


class TemplateProject:

    @classmethod
    def from_directory(cls, template_project_dir, options):
        """Instantiate a tvm.micro.project_api.client object given a 'source' directory.
        That 'source' directory is the one that contains a launch_microtvm_api_server.sh or
        a microtvm_api_server.py file which can be used to bring up a server and then have
        a project_api.client connected to it - the client class which will be effectively
        returned.

        Params:
        ------

        template_project_dir : dir containing proper .sh and .py server startup files.

        options :

        Returns:
        --------

        A instantiated client class connected to the server found in 'template_project_dir'.

        """
        return cls(client.instantiate_from_dir(template_project_dir), options)

    def __init__(self, client, options):
        self._client = client
        self._options = options
        self._info = self._client.server_info_query()
        if not self._info['is_template']:
            raise NotATemplateProjectError()

    def generate_project(self, model_library_format_path, project_dir):
        """Generate a project given a MLF archive and an output dir."""
        self._client.generate_project(
            model_library_format_path=model_library_format_path,
            standalone_crt_dir=get_standalone_crt_dir(),
            project_dir=project_dir,
            options=self._options)

        return GeneratedProject.from_directory(project_dir, self._options)


def generate_project(template_project_dir : str, model_library_format_path : str, project_dir : str, options : dict = None):
    template = TemplateProject.from_directory(template_project_dir, options)
    return template.generate_project(model_library_format_path, project_dir)
