"""
Sure Sort - Sorter 4
Routes barcodes scanned by the OPEX sorter to bin assignments via AS/400 web APIs.

Programmed by Preston Todd Cash
"""

import json
import logging
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VERSION = "2.0"
HOST_IP = os.environ.get("SURESORT_HOST", "172.17.0.54")
PORT = int(os.environ.get("SURESORT_PORT", "24200"))

BASE_API_URL = os.environ.get("SURESORT_API_URL", "http://atlprod:1028/suresort4")
BIN_LOOKUP_ENDPOINT = f"{BASE_API_URL}/sortc2502/"

# barcode length -> (endpoint, human-readable label)
BARCODE_ROUTES = {
    23: ("sortc2054", "Replenishment"),
    16: ("sortc2054", "Replenishment"),
    11: ("sortc3054", "Lab Jobs"),
    25: ("sortc4054", "X-Dock Orders"),
    10: ("sortc3064", "Enclosures"),
}

NEVADA_BIN = "C299"
BAD_READ_MARKER = "??????????"

logging.basicConfig(format="%(levelname)s:%(message)s", level=logging.DEBUG)
log = logging.getLogger("suresort")


def timestamp() -> str:
    """RPC timestamp in the same format the sorter expects."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + ".000"


# ---------------------------------------------------------------------------
# Barcode normalization
# (Lab scanners occasionally return two concatenated barcodes; these
#  functions collapse them down to the single real barcode.)
# ---------------------------------------------------------------------------
def normalize_dual_read(barcode: str) -> str:
    """Collapses a 23- or 24-char dual barcode read into a single barcode."""
    if len(barcode) == 23:
        for part in barcode.split("|"):
            if len(part) == 11:
                first, second = barcode[:11], barcode[12:]
                barcode = second if any(c.isalpha() for c in first) else first
    elif len(barcode) == 24:
        for part in barcode.split("|"):
            barcode = barcode[:11] if len(part) == 12 else barcode[13:]
    return barcode


def resolve_orientation(barcode: str) -> str:
    """For 23/16-length barcodes, extracts the correct substring by pipe position."""
    if len(barcode) == 16:
        return barcode
    pipe_index = barcode.find("|")
    if pipe_index == 6:
        return barcode[7:]
    if pipe_index == 16:
        return barcode[0:16]
    return barcode


# ---------------------------------------------------------------------------
# AS/400 API calls
# ---------------------------------------------------------------------------
def call_api(url: str) -> dict | None:
    """GETs a URL and parses the JSON body. Returns None (and logs) on failure."""
    try:
        with urllib.request.urlopen(url) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, ValueError) as err:
        log.error("API call failed for %s: %s", url, err)
        return None


def get_bin_for_barcode(barcode: str, endpoint: str) -> str | None:
    """
    Looks up the store for a barcode, then the bin for that store.
    Nevada lab-job orders are routed straight to bin C299 without a
    second lookup.
    """
    store_result = call_api(f"{BASE_API_URL}/{endpoint}/?{barcode}")
    if not store_result:
        return None

    store = store_result.get("store")
    log.info("Store %s", store)

    if store_result.get("success") == "Nevada":
        log.info("Nevada order - Bin is %s", NEVADA_BIN)
        return NEVADA_BIN

    bin_result = call_api(f"{BIN_LOOKUP_ENDPOINT}?{store}")
    if not bin_result:
        return None

    dest = bin_result.get("bin")
    log.info("Bin is %s", dest)
    return dest


# ---------------------------------------------------------------------------
# Sorter session (socket + JSON-RPC)
# ---------------------------------------------------------------------------
class SorterSession:
    """Manages the socket connection to the SureSort induction controller."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: socket.socket | None = None

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.connect((self.host, self.port))
            log.info("Connected to InductELC at %s:%s", self.host, self.port)
        except OSError:
            return

        threading.Thread(target=self.receive_loop, daemon=True).start()
        log.info("Receive thread started.")

        self.send({
            "id": "0",
            "jsonrpc": "2.0",
            "method": "StartSession",
            "params": {"sockets": 1, "version": 1, "wcsListenPort": self.port},
            "timestamp": timestamp(),
        })
        self.send({
            "id": "1",
            "jsonrpc": "2.0",
            "method": "StartRun",
            "params": {"runID": 0},
            "timestamp": timestamp(),
        })

    def send(self, payload: dict) -> None:
        message = "\x02" + json.dumps(payload) + "\x03"
        try:
            self.sock.sendall(message.encode())
        except OSError:
            log.exception("Unable to send message")

    def send_bin_result(self, msg_id: str, barcode: str, item_id: str, dest: str) -> None:
        self.send({
            "id": msg_id,
            "jsonrpc": "2.0",
            "result": {"barcode": barcode, "itemID": item_id, "machID": "OPEX", "reqDest": dest},
            "timestamp": timestamp(),
        })

    def receive_loop(self) -> None:
        while True:
            data = self.sock.recv(1024)
            if not data:
                sys.exit(0)
            if b"RoutingRequest" in data:
                self.handle_routing_request(data)

    def handle_routing_request(self, data: bytes) -> None:
        message = decode_json(data)
        if not message:
            log.error("No data returned")
            return

        msg_id = message.get("id")
        params = message.get("params", {})
        barcode = params.get("barcode", "")
        item_id = params.get("itemID")

        log.info("------------------------------------")
        log.info("Sorter read this barcode %s", barcode)

        if barcode == BAD_READ_MARKER:
            log.info("A BAD READ!!!!!")
            return

        barcode = normalize_dual_read(barcode)
        if len(barcode) in (23, 16):
            barcode = resolve_orientation(barcode)

        route = BARCODE_ROUTES.get(len(barcode))
        if not route:
            return  # unrecognized barcode length; nothing to route

        endpoint, label = route
        barcode_for_url = barcode.replace(" ", "%20")
        log.info("Barcode sent to AS400 (%s): %s", label, barcode_for_url)

        dest = get_bin_for_barcode(barcode_for_url, endpoint)
        if dest is None:
            log.error("Could not resolve a bin for barcode %s", barcode)
            return

        self.send_bin_result(msg_id, barcode, item_id, dest)


def decode_json(raw: bytes) -> dict | None:
    try:
        text = raw.decode("utf8").replace("'", '"').split("\n\x03")[0]
        if text.startswith("\x02"):
            text = text[1:]
        return json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.exception("Error decoding JSON data")
        return None


def main() -> None:
    print("Sure Sort - Sorter 4")
    print(f"Version {VERSION}")
    print("Programmed by Preston Todd Cash")
    print("")

    session = SorterSession(HOST_IP, PORT)
    session.connect()

    try:
        while True:
            threading.Event().wait()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()