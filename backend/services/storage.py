"""存储频道: Local/Session Storage、Cookies、IndexedDB(供 Application 面板)。

- DOMStorage.enable → getDOMStorageItems / set/remove 命令 + 变更事件
- Network.getCookies / setCookie / deleteCookies
- IndexedDB.requestDatabaseNames / requestDatabase / requestData
"""

from __future__ import annotations

from typing import Any

from .cdp import CDPManager, CDPSession


class StorageChannel:
    name = "storage"
    domains: list[tuple[str, dict[str, Any]]] = [
        ("DOMStorage.enable", {}),
        ("IndexedDB.enable", {}),
        ("Storage.enable", {}),
    ]

    def __init__(self, mgr: CDPManager):
        self.mgr = mgr
        self.channel = mgr.register_channel(self)

    async def on_event(self, session: CDPSession, method: str, params: dict[str, Any]) -> bool:
        if method in (
            "DOMStorage.domStorageItemAdded",
            "DOMStorage.domStorageItemRemoved",
            "DOMStorage.domStorageItemUpdated",
        ):
            await self.channel.publish({
                "type": "storage",
                "op": "storage-changed",
                "storage_key": (params.get("storageId") or {}).get("storageKey"),
                "local": bool((params.get("storageId") or {}).get("isLocalStorage")),
            })
            return True
        if method == "DOMStorage.domStorageItemsCleared":
            await self.channel.publish({
                "type": "storage",
                "op": "storage-changed",
                "storage_key": (params.get("storageId") or {}).get("storageKey"),
                "local": bool((params.get("storageId") or {}).get("isLocalStorage")),
            })
            return True
        return False

    # ------------------------------------------------------------ origin

    async def origin(self) -> dict[str, Any]:
        res = await self.mgr.evaluate("location.origin")
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error")}
        item = res.get("item") or {}
        value = (item.get("v") or "").strip()
        if not value or value == "undefined":
            value = "null"
        return {"ok": True, "origin": value}

    # ------------------------------------------------------------ Storage

    def _storage_id(self, origin: str, session: bool) -> dict[str, Any]:
        # CDP 的 storageKey 带尾部斜杠, 而 location.origin 不带
        key = origin if origin.endswith("/") else origin + "/"
        return {"storageKey": key, "isLocalStorage": not session}

    async def items(self, origin: str, session: bool = False) -> dict[str, Any]:
        sess = self.mgr.primary()
        if sess is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await sess.command("DOMStorage.getDOMStorageItems",
                                      {"storageId": self._storage_id(origin, session)}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        entries = (resp.get("result") or {}).get("entries") or []
        return {"ok": True, "items": [{"key": k, "value": v} for k, v in entries]}

    async def set_item(self, origin: str, session: bool, key: str, value: str) -> dict[str, Any]:
        return await self._storage_cmd("DOMStorage.setDOMStorageItem",
                                       {"storageId": self._storage_id(origin, session), "key": key, "value": value})

    async def remove_item(self, origin: str, session: bool, key: str) -> dict[str, Any]:
        return await self._storage_cmd("DOMStorage.removeDOMStorageItem",
                                       {"storageId": self._storage_id(origin, session), "key": key})

    async def _storage_cmd(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        sess = self.mgr.primary()
        if sess is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await sess.command(method, params, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        return {"ok": True}

    # ------------------------------------------------------------ Cookies

    async def cookies(self, origin: str) -> dict[str, Any]:
        sess = self.mgr.primary()
        if sess is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await sess.command("Network.getCookies", {"urls": [origin]}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        cookies = (resp.get("result") or {}).get("cookies") or []
        return {"ok": True, "cookies": cookies}

    async def set_cookie(self, origin: str, name: str, value: str, *,
                         path: str = "/", domain: str | None = None,
                         http_only: bool = False, secure: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "value": value, "path": path,
                                  "httpOnly": http_only, "secure": secure, "url": origin}
        if domain:
            params["domain"] = domain
        sess = self.mgr.primary()
        if sess is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await sess.command("Network.setCookie", params, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        return {"ok": True}

    async def delete_cookie(self, origin: str, name: str) -> dict[str, Any]:
        sess = self.mgr.primary()
        if sess is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await sess.command("Network.deleteCookies",
                                      {"name": name, "url": origin}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        return {"ok": True}

    # ------------------------------------------------------------ IndexedDB

    async def idb_databases(self, origin: str) -> dict[str, Any]:
        sess = self.mgr.primary()
        if sess is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await sess.command("IndexedDB.requestDatabaseNames",
                                      {"securityOrigin": origin}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        return {"ok": True, "databases": (resp.get("result") or {}).get("databaseNames") or []}

    async def idb_stores(self, origin: str, database: str) -> dict[str, Any]:
        sess = self.mgr.primary()
        if sess is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await sess.command("IndexedDB.requestDatabase",
                                      {"securityOrigin": origin, "databaseName": database}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        db = (resp.get("result") or {}).get("databaseWithObjectStores") or {}
        stores = [
            {"name": s.get("name"), "keyPath": s.get("keyPath") or None, "indexes": len(s.get("indexes") or [])}
            for s in db.get("objectStores") or []
        ]
        return {"ok": True, "stores": stores}

    async def idb_data(self, origin: str, database: str, store: str,
                       skip: int = 0, count: int = 50) -> dict[str, Any]:
        sess = self.mgr.primary()
        if sess is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await sess.command("IndexedDB.requestData", {
                "securityOrigin": origin,
                "databaseName": database,
                "objectStoreName": store,
                "indexName": "",
                "skipCount": skip,
                "pageSize": count,
            }, timeout=8.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        result = resp.get("result") or {}
        entries = result.get("objectStoreDataEntries") or []
        rows = []
        for e in entries:
            rows.append({
                "key": await self._entry_text(sess, e.get("key")),
                "primaryKey": await self._entry_text(sess, e.get("primaryKey")),
                "value": await self._entry_text(sess, e.get("value")),
            })
        return {"ok": True, "rows": rows, "has_more": len(entries) >= count}

    async def _entry_text(self, sess: CDPSession, v: dict[str, Any] | None) -> str:
        """把 IDB 条目的 RemoteObject 转成可读文本(对象用 JSON.stringify)。"""
        if not v:
            return ""
        if v.get("type") in ("string", "number", "boolean", "bigint"):
            return str(v.get("value"))
        if v.get("value") is not None:
            return str(v.get("value"))
        oid = v.get("objectId")
        if oid:
            try:
                resp = await sess.command("Runtime.callFunctionOn", {
                    "objectId": oid,
                    "functionDeclaration": "function(){ try { return JSON.stringify(this) } catch(e) { return String(this) } }",
                    "returnByValue": True,
                    "silent": True,
                }, timeout=3.0)
                val = ((resp.get("result") or {}).get("result") or {}).get("value")
                if isinstance(val, str):
                    return val
            except Exception:  # noqa: BLE001
                pass
        return v.get("description") or ""
