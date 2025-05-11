from __future__ import annotations

import importlib
import typing

from . import errors
from .request import WasiRequest
from .response import BytesResponseBody, ResponseBody, WasiResponse

C_UINT64_MAX = 18_446_744_073_709_551_615


def preload(world_name: str) -> None:
    importlib.import_module(".imports.outgoing_handler", world_name)
    importlib.import_module(".imports.types", world_name)
    importlib.import_module(".imports.streams", world_name)
    importlib.import_module(".types", world_name)


def send_request(request: WasiRequest, world_name: str) -> WasiResponse:
    wasi_http_outgoing_handler = importlib.import_module(
        ".imports.outgoing_handler", world_name
    )
    wasi_http_types = importlib.import_module(".imports.types", world_name)
    wasi_streams = importlib.import_module(".imports.streams", world_name)
    wasi_types = importlib.import_module(".types", world_name)

    def convert_response(  # type: ignore[no-untyped-def]
        request: WasiRequest,
        response,
    ) -> WasiResponse:
        headers: dict[str, str] = dict()
        with response.headers() as response_headers:
            for k, v in response_headers.entries():
                headers[k] = v.decode()

        return WasiResponse(
            status_code=response.status(),
            headers=headers,
            body=convert_body(response, request.preload_content),
            request=request,
        )

    def convert_body(  # type: ignore[no-untyped-def]
        response, preload_content: bool
    ) -> ResponseBody:
        if preload_content:
            body_bytes = b""
            with response.consume() as response_body_resource:
                with response_body_resource.stream() as response_stream:
                    done = False
                    while not done:
                        try:
                            chunk = response_stream.blocking_read(1024)
                            if len(chunk) != 0:
                                body_bytes += chunk
                            else:
                                done = True
                        except wasi_types.Err as e:
                            if isinstance(e.value, wasi_streams.StreamError_Closed):
                                done = True
                            elif isinstance(
                                e.value, wasi_streams.StreamError_LastOperationFailed
                            ):
                                raise errors.ResponseStreamReadingError(
                                    f"Failed reading response body {e.value.value}"
                                )
                            else:
                                raise errors.UnknownWasiError() from e

                return BytesResponseBody(body_bytes)
        else:
            response_body_resource = response.consume()
            response_body_resource.__enter__()
            response_stream = response_body_resource.stream()
            response_stream.__enter__()
            return StreamResponseBody(response_body_resource, response_stream)

    class StreamResponseBody(ResponseBody):
        def __init__(self, response_body_resource, stream) -> None:  # type: ignore[no-untyped-def]
            self.resource = response_body_resource
            self.stream = stream
            self._closed = False

        def read(self, amt: int | None) -> bytes:
            if self.closed():
                raise errors.ResourceClosedError("ResponseStream")

            if amt is None or amt > C_UINT64_MAX:
                amt = C_UINT64_MAX

            try:
                data = self.stream.blocking_read(amt)
                if len(data) == 0:
                    self.close()
                return typing.cast(bytes, data)
            except wasi_types.Err as e:
                self.close()
                if isinstance(e.value, wasi_streams.StreamError_Closed):
                    return b""
                elif isinstance(e.value, wasi_streams.StreamError_LastOperationFailed):
                    raise errors.ResponseStreamReadingError(
                        f"Failed reading response body {e.value.value}"
                    )
                else:
                    raise errors.UnknownWasiError() from e

        def closed(self) -> bool:
            return self._closed

        def close(self) -> None:
            if not self._closed:
                self.stream.__exit__(None, None, None)
                wasi_http_types.IncomingBody.finish(self.resource)
                self._closed = True

    headers = wasi_http_types.Fields()

    for k, v in request.headers.items():
        headers.append(k, v.encode())

    outgoing_request = wasi_http_types.OutgoingRequest(headers)
    outgoing_body = outgoing_request.body()

    if request.scheme == "http":
        outgoing_request.set_scheme(wasi_http_types.Scheme_Http())
    elif request.scheme == "https":
        outgoing_request.set_scheme(wasi_http_types.Scheme_Https())
    else:
        outgoing_request.set_scheme(wasi_http_types.Scheme_Other(request.scheme))

    if request.method == "GET":
        outgoing_request.set_method(wasi_http_types.Method_Get())
    elif request.method == "HEAD":
        outgoing_request.set_method(wasi_http_types.Method_Head())
    elif request.method == "POST":
        outgoing_request.set_method(wasi_http_types.Method_Post())
    elif request.method == "PUT":
        outgoing_request.set_method(wasi_http_types.Method_Put())
    elif request.method == "DELETE":
        outgoing_request.set_method(wasi_http_types.Method_Delete())
    elif request.method == "OPTIONS":
        outgoing_request.set_method(wasi_http_types.Method_Options())
    elif request.method == "TRACE":
        outgoing_request.set_method(wasi_http_types.Method_Trace())
    elif request.method == "PATCH":
        outgoing_request.set_method(wasi_http_types.Method_Patch())
    else:
        outgoing_request.set_method(wasi_http_types.Method_Other(request.method))

    outgoing_request.set_authority(f"{request.host}:{request.port}")
    outgoing_request.set_path_with_query(request.url)

    request_options = wasi_http_types.RequestOptions()
    if request.timeout is not None and request.timeout > 0:
        # wasi uses nanoseconds while we use seconds
        request_options.set_connect_timeout(int(request.timeout * 1_000_000_000))

    with wasi_http_outgoing_handler.handle(
        outgoing_request, request_options
    ) as future_response:
        if request.body is not None:
            with outgoing_body.write() as stream:
                for chunk in request.body:
                    stream.blocking_write_and_flush(chunk)

        wasi_http_types.OutgoingBody.finish(outgoing_body, None)

        # wait for request to complete
        with future_response.subscribe() as pollable:
            pollable.block()
        response = future_response.get()

        if isinstance(response, wasi_types.Ok):
            if isinstance(response.value, wasi_types.Ok):
                return convert_response(request, response.value.value)
            else:
                raise errors.WasiErrorCode(response.value)
        else:
            raise errors.ResponseAlreadyTaken(request)
