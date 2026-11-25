"""Serve the public Matrix delegation without changing Synapse's server identity."""

import json

from twisted.web.resource import Resource


class ServerDiscoveryResource(Resource):
    isLeaf = True

    @staticmethod
    def parse_config(config):
        return config["delegated_server"]

    def __init__(self, delegated_server, _module_api):
        super().__init__()
        self.response = json.dumps({"m.server": delegated_server}, separators=(",", ":")).encode("utf-8")

    def render_GET(self, request):
        if request.path != b"/.well-known/matrix/server":
            request.setResponseCode(404)
            return b"Not found\n"

        request.setHeader(b"Content-Type", b"application/json")
        request.setHeader(b"Access-Control-Allow-Origin", b"*")
        return self.response

    render_HEAD = render_GET
