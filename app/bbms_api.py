# SPDX-License-Identifier: GPL-2.0-or-later

#
# RFID tag machine access
#
# Talking to BBMS: one HTTP request at a time, over a new connection each time
#
# The device API's rule (docs/device-api-v2.md in the BBMS repository): every decision is a 200 with a
# JSON body. Anything else, including no reply at all, means "BBMS couldn't decide".
#

import asyncio
import json

from util import MICROPYTHON, log

MAX_BODY = 16384  # replies are a few hundred bytes; refuse anything that could exhaust the memory


class Result:
    def __init__(self, status=0, data=None, etag=None, error=None):
        self.status = status  # the HTTP status, or 0 if there was no reply
        self.data = data  # the JSON body (or the raw bytes, if asked for), or None
        self.etag = etag
        self.error = error
        self.ok = status == 200 and data is not None


class Api:
    def __init__(self, url, key, ca_file=None, tls_insecure=False, timeout=10, link_up=None):
        scheme, rest = url.split("://", 1)
        parts = rest.split("/", 1)
        self.tls = scheme == "https"
        self.host_header = parts[0]
        self.base_path = "/" + (parts[1] if len(parts) > 1 else "")
        host = parts[0].split(":")
        self.host = host[0]
        self.port = int(host[1]) if len(host) > 1 else (443 if self.tls else 80)
        self.key = key
        self.ca_file = ca_file
        self.tls_insecure = tls_insecure
        self.timeout = timeout
        self.link_up = link_up  # a function saying whether the network is connected, if there's a way to tell
        self._tls_context = None

    async def request(self, method, path, body=None, etag=None, binary=False):
        """
        Send one request. body: sent as JSON (None: no body). etag: sent as If-None-Match.
        binary: hand back the body of a 200 as bytes. Always returns a Result; it never raises.
        """
        if self.link_up and not self.link_up():
            result = Result(error="no network")
        else:
            try:
                result = await asyncio.wait_for(self._exchange(method, path, body, etag, binary), self.timeout)
            except asyncio.TimeoutError:
                result = Result(error="timed out")
            except Exception as error:  # refused, reset, DNS, TLS, out of memory...
                result = Result(error=repr(error))
        log(method, path, "->", result.status or result.error, "" if binary or result.data is None else result.data)
        return result

    def _context(self):
        if self._tls_context is None:
            import ssl

            if MICROPYTHON:
                # MicroPython checks the certificate only against authorities it is given.
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                authority = None
                try:
                    with open(self.ca_file, "rb") as f:
                        authority = f.read()
                except (OSError, TypeError):
                    pass
                if authority:
                    context.verify_mode = ssl.CERT_REQUIRED
                    context.load_verify_locations(cadata=authority)
                elif self.tls_insecure:
                    context.verify_mode = ssl.CERT_NONE
                else:
                    raise OSError("no CA certificate: " + str(self.ca_file))
            else:
                context = ssl.create_default_context()  # the computer's own list of authorities
                if self.tls_insecure:  # bench testing only, as on the reader
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
            self._tls_context = context
        return self._tls_context

    async def _exchange(self, method, path, body, etag, binary):
        payload = None if body is None else json.dumps(body).encode()
        head = [
            "%s %s%s HTTP/1.1" % (method, self.base_path, path),
            "Host: " + self.host_header,
            "Authorization: Bearer " + self.key,
            "Accept: application/json",
            "Connection: close",
        ]
        if payload is not None:
            head.append("Content-Type: application/json")
            head.append("Content-Length: %d" % len(payload))
        if etag:
            head.append("If-None-Match: " + etag)
        head.append("\r\n")

        if self.tls:
            reader, writer = await asyncio.open_connection(
                self.host, self.port, ssl=self._context(), server_hostname=self.host
            )
        else:
            reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            writer.write("\r\n".join(head).encode())
            if payload:
                writer.write(payload)
            await writer.drain()

            status_line = await reader.readline()
            status = int(status_line.decode().split()[1])
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, value = line.decode().split(":", 1)
                headers[name.strip().lower()] = value.strip()

            raw = b""
            if status == 200:
                if "chunked" in headers.get("transfer-encoding", ""):
                    while True:
                        size = int((await reader.readline()).decode().split(";")[0], 16)
                        if size == 0:
                            break
                        if len(raw) + size > MAX_BODY:
                            raise ValueError("reply too long")
                        raw += await reader.readexactly(size)
                        await reader.readline()  # the line end after each chunk
                elif "content-length" in headers:
                    length = int(headers["content-length"])
                    if length > MAX_BODY:
                        raise ValueError("reply too long")
                    raw = await reader.readexactly(length)
                else:
                    raw = await reader.read(-1)  # no length given: the body is everything until BBMS hangs up

            data = None
            if status == 200:
                if binary:
                    data = raw
                else:
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        pass
            return Result(status, data, headers.get("etag"))
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
