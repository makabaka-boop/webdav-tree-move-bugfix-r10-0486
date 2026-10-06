"""Directory relocation and lock-path ownership."""

from .store import is_ancestor, etag_str, norm_path


def move_tree(ws, request):
    source = norm_path(request["source"])
    destination = norm_path(request["destination"])
    moved = []
    for path in ws.list_files():
        if path == source or is_ancestor(source, path):
            outcome = ws.put_file(
                path, ws.get_file(path)[1], request["versions"].get(path), None
            )
            if outcome.status != "ok":
                return {"ok": False, "reason": outcome.status}
            target = destination + path[len(source) :]
            with ws.txn() as c:
                c.execute("UPDATE resources SET path=? WHERE path=?", (target, path))
            moved.append({"from": path, "to": target, "etag": outcome.etag})
    return {"ok": True, "moved": moved}
