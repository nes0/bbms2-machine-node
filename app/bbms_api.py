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
import binascii
import gc
import hashlib
import json
import socket

from util import MICROPYTHON, log, now_ms

MAX_BODY = 16384  # replies are a few hundred bytes; refuse anything that could exhaust the memory
DNS_REST_MS = 300000  # after a failed look-up of BBMS's address, use the last known one for this long

# BBMS answered, and the answer was "no" or "nothing new". Any other failure means the question never
# reached BBMS, or what came back wasn't from BBMS: no network, no DNS, no route, a certificate that has
# run out, a proxy's 502, a Wi-Fi log-in page.
ANSWERS = (304, 400, 401, 403, 404, 409)

# What was going on when a request failed, for the screen.
STAGES = {
    "dns": "Server not found",
    "connect": "Can't connect",
    "send": "Connection lost",
    "wait": "No answer",
    "read": "Connection lost",
}


class Result:
    def __init__(self, status=0, data=None, etag=None, error=None, why=None, detail=None):
        self.detail = detail  # more about a failure, for whoever is finding out why (or None)
        self.status = status  # the HTTP status, or 0 if there was no reply
        self.data = data  # the JSON body (or the raw bytes, if asked for), or None
        self.etag = etag
        self.error = error
        self.ok = status == 200 and data is not None
        # BBMS couldn't be asked, so the reader must judge for itself (see ANSWERS above).
        self.cut_off = not self.ok and status not in ANSWERS
        if why is None and not self.ok:
            if status >= 500:
                why = "Server error %d" % status
            elif status in ANSWERS:
                why = "BBMS said no (%d)" % status
            else:
                why = "Unexpected answer"  # a 200 that isn't JSON, a redirect: something in the way
        self.why = why  # a few words for the screen, or None if all is well


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
        self.address = None  # where BBMS last answered from: used when its name can't be looked up
        self._dns_retry_at = 0
        self._stage = ""

    async def request(self, method, path, body=None, etag=None, binary=False):
        """
        Send one request. body: sent as JSON (None: no body). etag: sent as If-None-Match.
        binary: hand back the body of a 200 as bytes. Always returns a Result; it never raises.
        """
        result = await self._attempt(self._exchange(method, path, body, etag, binary), self.timeout)
        log(method, path, "->", result.status or result.error, "" if binary or result.data is None else result.data)
        return result

    async def download(self, path, save_as, timeout, max_bytes=None):
        """
        GET a file of any size straight into the file `save_as`, a block at a time. The Result's data
        is (bytes written, their SHA-256 in hex) for a 200. Like request(), it never raises.
        """
        result = await self._attempt(self._exchange("GET", path, None, None, True, save_as, max_bytes), timeout)
        log("GET", path, "->", result.status or result.error)
        return result

    async def _attempt(self, exchange, timeout):
        if self.link_up and not self.link_up():
            exchange.close()  # it was never started
            return Result(error="no network", why="No Wi-Fi")
        self._stage = "dns"
        # A secure connection needs some tens of kilobytes at once, on top of what the reader is using:
        # clear out what's no longer needed first.
        gc.collect()
        try:
            return await asyncio.wait_for(exchange, timeout)
        except asyncio.TimeoutError:
            error = "timed out"
        except Exception as error_:  # refused, reset, DNS, TLS, out of memory...
            error = repr(error_)
        why = STAGES.get(self._stage)
        text = error.upper()
        if "ALLOC" in text or "MEMORY" in text or "ENOMEM" in text:
            why = "Out of memory"  # most often the secure connection: MBEDTLS_ERR_MPI_ALLOC_FAILED
        elif "CERT" in text:
            why = "Certificate problem"  # run out, the wrong name, or not signed by the authority in ca_file
        elif "SSL" in text or "MBEDTLS" in text or "TLS" in text:
            # With the library's error number, so the cause can be found without a serial cable.
            code = error.split("(", 1)[-1].split(",")[0].rstrip(")")
            why = "Secure link failed" + (" (%s)" % code if code.lstrip("-").isdigit() else "")
        detail = None
        if why == "Out of memory":
            # Which memory ran out, in a form short enough for the screen.
            number = error.split("(", 1)[-1].split(",")[0].rstrip(")")
            detail = ("Python" if "MemoryError" in error else "E" + number) + " " + self._memory()
        elif MICROPYTHON:
            log("memory:", self._memory())
        return Result(error="%s: %s" % (self._stage, error), why=why, detail=detail)

    def _memory(self):
        """
        How much memory is in use, since running out is a common cause of failures on the ESP32. There
        are two pools. MicroPython's holds the program and its data; it grows by taking from the
        system's, and never gives any back. The system's is what Wi-Fi and the secure connection use:
        a secure connection needs about 40 KB of it, in blocks of up to 16 KB.
        """
        try:
            import esp32

            used = gc.mem_alloc()
            regions = esp32.idf_heap_info(esp32.HEAP_DATA)  # (size, free, largest free block, least ever free)
            text = "Py %d/%dK Sys %dK max %dK low %dK" % (
                used // 1024,
                (used + gc.mem_free()) // 1024,
                sum([region[1] for region in regions]) // 1024,
                max([region[2] for region in regions]) // 1024,
                sum([region[3] for region in regions]) // 1024,
            )
        except Exception as error:  # not an ESP32
            text = "(memory unknown: %r)" % error
        log(text)
        return text

    async def _resolve(self):
        """
        BBMS's network address. When its name can't be looked up (the DNS server is down, or the ISP's is
        misbehaving) this is the address BBMS last answered from. That's safe: over https the certificate
        is still checked against the name, so nobody else can answer at that address.
        """
        if self.address and now_ms() < self._dns_retry_at:
            return self.address
        try:
            if MICROPYTHON:
                # This waits for the answer, which is what MicroPython's own open_connection does.
                found = socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM)
            else:
                found = await asyncio.get_event_loop().getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
            return found[0][-1][0]
        except OSError as error:
            if not self.address:
                raise
            log("can't look up", self.host, repr(error), "- trying", self.address)
            self._dns_retry_at = now_ms() + DNS_REST_MS  # a failed look-up is slow: don't repeat it every time
            return self.address

    async def _save(self, reader, headers, save_as, max_bytes):
        """Copy the reply's body into a file. Returns (bytes written, their SHA-256 in hex)."""
        digest = hashlib.sha256()
        written = 0
        chunked = "chunked" in headers.get("transfer-encoding", "")
        left = int(headers["content-length"]) if "content-length" in headers else None
        with open(save_as, "wb") as f:
            while True:
                if chunked:
                    left = int((await reader.readline()).decode().split(";")[0], 16)
                    if left == 0:
                        break
                while left is None or left > 0:
                    block = await reader.read(512 if left is None else min(512, left))
                    if not block:
                        if left is None:
                            break  # no length given: the body ends when BBMS hangs up
                        raise OSError("the connection closed early")
                    f.write(block)
                    digest.update(block)
                    written += len(block)
                    if max_bytes and written > max_bytes:
                        raise ValueError("reply too long")
                    if left is not None:
                        left -= len(block)
                if not chunked:
                    break
                await reader.readline()  # the line end after each chunk
        return written, binascii.hexlify(digest.digest()).decode()

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

    async def _exchange(self, method, path, body, etag, binary, save_as=None, max_bytes=None):
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

        address = await self._resolve()
        self._stage = "connect"
        if self.tls:
            reader, writer = await asyncio.open_connection(
                address, self.port, ssl=self._context(), server_hostname=self.host
            )
        else:
            reader, writer = await asyncio.open_connection(address, self.port)
        try:
            self._stage = "send"
            writer.write("\r\n".join(head).encode())
            if payload:
                writer.write(payload)
            await writer.drain()

            self._stage = "wait"
            status_line = await reader.readline()
            status = int(status_line.decode().split()[1])
            self._stage = "read"
            if status in (200, 304):
                self.address = address  # BBMS itself answered from here
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, value = line.decode().split(":", 1)
                headers[name.strip().lower()] = value.strip()

            raw = b""
            if status == 200 and save_as:
                return Result(200, await self._save(reader, headers, save_as, max_bytes), headers.get("etag"))
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
