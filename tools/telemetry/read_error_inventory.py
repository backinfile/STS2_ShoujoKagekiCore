"""Collect review evidence sequentially without moving the reviewed baseline."""
import argparse
from email.utils import parsedate_to_datetime
from http.client import HTTPException
import json
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
COUNTERS = ("occurrences", "sessions", "last_seen")
METADATA = ("first_seen", "build", "mod_version", "platform", "source")


class InventoryChanged(ValueError):
    pass


def get(url):
    for attempt in range(6):
        try:
            request = Request(url, headers={"Accept": "application/json", "X-Observatory-Client": "error-watch"})
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            with error:
                try:
                    reason = json.load(error).get("error")
                except (ValueError, AttributeError):
                    reason = None
                if reason in ("inventory_changed", "inventory_snapshot_expired"):
                    raise InventoryChanged(reason) from None
                if reason == "inventory_snapshot_too_large":
                    return None
                if error.code not in (429, 500, 502, 503, 504) or attempt == 5:
                    raise RuntimeError(f"Observatory HTTP {error.code}") from None
                delay = 2**attempt
                retry = error.headers.get("Retry-After")
                if retry:
                    try:
                        requested = int(retry) if retry.isdigit() else parsedate_to_datetime(retry).timestamp() - time.time()
                        delay = max(delay, min(requested, 300))
                    except (ValueError, TypeError, OverflowError):
                        pass
        except (URLError, TimeoutError, ConnectionError, HTTPException):
            if attempt == 5:
                raise RuntimeError("Observatory connection unavailable") from None
            delay = 2**attempt
        time.sleep(delay)


def checked_items(page):
    items = page.get("items")
    if not isinstance(items, list):
        raise ValueError("invalid_inventory_items")
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or
                not FINGERPRINT.fullmatch(item["id"]) or
                any(type(item.get(key)) is not int or item[key] < 0 for key in COUNTERS)):
            raise ValueError("invalid_inventory_item")
    return items


def inventory(read, first):
    groups, cursors = {}, set()
    current, total = first["revision"], first["total"]
    token = first.get("snapshot")
    page = first
    after = ""
    while True:
        if page["revision"] != current or page["total"] != total or page.get("snapshot") != token:
            raise InventoryChanged("inventory_changed")
        items = checked_items(page)
        for item in items:
            if item["id"] <= after or item["id"] in groups:
                raise ValueError("invalid_inventory_order")
            groups[item["id"]] = item
            after = item["id"]
        cursor = page.get("next")
        if not cursor:
            break
        if not items or cursor != after or cursor in cursors:
            raise ValueError("invalid_inventory_cursor")
        cursors.add(cursor)
        args = {"inventory": 1, "after": cursor, "revision": current}
        if token:
            args["snapshot"] = token
        page = read(**args)
    if len(groups) != total:
        raise InventoryChanged("incomplete_inventory")
    return groups, first


def legacy(read, first):
    latest = first
    observed = {}
    for sort in ("recent", "count", "sessions"):
        initial = first if sort == "recent" else read(compact=1, page=1, sort=sort)
        for number in range(1, initial["pages"] + 1):
            page = initial if number == 1 else read(compact=1, page=number, sort=sort)
            for item in checked_items(page):
                old = observed.get(item["id"])
                if old is None or all(item[k] >= old[k] for k in COUNTERS):
                    observed[item["id"]] = item
        latest = read(compact=1, page=1, sort="recent")
        for item in checked_items(latest):
            old = observed.get(item["id"])
            if old is None or all(item[k] >= old[k] for k in COUNTERS):
                observed[item["id"]] = item
        if len(observed) == latest["total"]:
            return observed, latest
    raise InventoryChanged("incomplete_legacy_inventory")


def collect(url, baseline):
    def read(**query):
        return get(url + "?" + urlencode(query))

    for attempt in range(3):
        try:
            first = read(inventory=1, snapshot="start", compact=1)
            if first is None:
                first = read(inventory=1)
            if first.get("schema_version") == 1 and type(first.get("revision")) is int:
                groups, counts = inventory(read, first)
            else:
                groups, counts = legacy(read, first)
            if baseline.keys() - groups.keys():
                raise InventoryChanged("historical_fingerprints_missing")
            for identity, old in baseline.items():
                if any(groups[identity][key] < old.get(key, 0) for key in COUNTERS):
                    raise InventoryChanged("inventory_counters_regressed")
            break
        except InventoryChanged:
            if attempt == 2:
                raise

    details = []
    for identity, item in groups.items():
        old = baseline.get(identity)
        # Older baselines contain active reports without an explicit state and
        # may omit display metadata. Compare all counters and known metadata.
        if (old is not None and all(old.get(key, 0) == item[key] for key in COUNTERS)
                and all(key not in old or old[key] == item.get(key) for key in METADATA)
                and old.get("data_state", "active") == item.get("data_state", "active")):
            continue
        detail = get(url + "/" + identity)
        if detail.get("id") != identity or any(detail.get(key, -1) < item[key] for key in COUNTERS):
            raise InventoryChanged("detail_precedes_inventory")
        details.append(detail)
    metadata = {key: value for key, value in counts.items()
                if key not in ("items", "next", "snapshot", "snapshot_ttl_seconds")}
    return {"items": list(groups.values()), "details": details, "counts": metadata, "complete": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = json.loads(args.config.read_text(encoding="utf-8-sig"))["errors_api_base"].rstrip("/")
    target = urlsplit(url)
    if (target.scheme != "https" and not (target.scheme == "http" and target.hostname == "127.0.0.1")
            or target.username or target.password or target.query or target.fragment):
        parser.error("Expected an HTTPS errors API or an isolated loopback URL")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8-sig"))["groups"]
    if args.output.resolve() in (args.config.resolve(), args.baseline.resolve()):
        parser.error("Evidence output must be separate from the configuration and baseline")
    try:
        result = collect(url, baseline)
    except (RuntimeError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.output.parent, delete=False) as stream:
            temp = Path(stream.name)
            json.dump(result, stream, ensure_ascii=False)
        temp.replace(args.output)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    print(f"Collected {len(result['items'])} fingerprints and {len(result['details'])} changed details")


if __name__ == "__main__":
    main()
