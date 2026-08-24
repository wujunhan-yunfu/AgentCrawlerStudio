"""元素频道: 通过 CDP DOM 域提供 DOM 树、节点属性与盒模型(供 Elements 面板)。

- POST /dom/tree: DOM.getDocument(depth=-1) 整棵 DOM 树
- POST /dom/box:   DOM.getBoxModel 取元素内容盒 → 前端在实时画面上高亮
- DOM.documentUpdated 事件 → /ws/dom 推送 reload, 前端自动重取
"""

from __future__ import annotations

from typing import Any

from .cdp import CDPManager, CDPSession


class DOMChannel:
    name = "dom"
    domains: list[tuple[str, dict[str, Any]]] = [
        ("DOM.enable", {}),
    ]

    def __init__(self, mgr: CDPManager):
        self.mgr = mgr
        self.channel = mgr.register_channel(self)

    async def on_event(self, session: CDPSession, method: str, params: dict[str, Any]) -> bool:
        if method == "DOM.documentUpdated":
            await self.channel.publish({"type": "dom", "op": "reload"})
            return True
        return False

    # ------------------------------------------------------------ 命令接口

    async def tree(self) -> dict[str, Any]:
        session = self.mgr.primary()
        if session is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await session.command("DOM.getDocument", {"depth": -1, "pierce": True}, timeout=8.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        root = (resp.get("result") or {}).get("root") or {}
        return {"ok": True, "root": self._transform(root)}

    async def box_model(self, backend_node_id: int) -> dict[str, Any]:
        """取元素内容盒模型(视口 CSS 像素), 供前端高亮。"""
        session = self.mgr.primary()
        if session is None:
            return {"ok": False, "error": "浏览器控制台未连接"}
        try:
            resp = await session.command("DOM.getBoxModel", {"backendNodeId": backend_node_id}, timeout=3.0)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        if "error" in resp:
            return {"ok": False, "error": str(resp["error"].get("message", "CDP 错误"))}
        model = (resp.get("result") or {}).get("model") or {}
        content = model.get("content") or []
        if len(content) >= 4:
            xs = content[0::2]
            ys = content[1::2]
            box = {
                "x": min(xs),
                "y": min(ys),
                "w": max(xs) - min(xs),
                "h": max(ys) - min(ys),
            }
            return {"ok": True, "box": box}
        return {"ok": True, "box": None}

    # ------------------------------------------------------------ 转换

    @staticmethod
    def _transform(node: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": node.get("backendNodeId"),
            "t": node.get("nodeType"),
            "name": node.get("nodeName") or node.get("localName") or "",
            "value": node.get("nodeValue") or "",
            "attrs": {},
            "count": node.get("childNodeCount"),
            "children": [],
        }
        attrs = node.get("attributes") or []
        for i in range(0, len(attrs) - 1, 2):
            out["attrs"][attrs[i]] = attrs[i + 1]
        for child in node.get("children") or []:
            out["children"].append(DOMChannel._transform(child))
        if node.get("contentDocument"):
            out["children"].append(DOMChannel._transform(node["contentDocument"]))
        return out
