# -*- coding: utf-8 -*-
"""雷电模拟器(LDPlayer)+ adb 驱动。

语义定位优先:uiautomator dump 找文本/描述节点再点中心;坐标仅作后备。
所有子进程调用绝对 exe 路径(见 config)。
"""
from __future__ import annotations
import re
import subprocess
import time
import xml.etree.ElementTree as ET

import config


def _run(exe: str, args: list[str], timeout: int = 60) -> str:
    p = subprocess.run([exe, *args], capture_output=True, timeout=timeout,
                       creationflags=config.no_window_flags())
    return p.stdout.decode("utf-8", errors="replace")


class Emulator:
    def __init__(self, name: str = "雷电模拟器"):
        self.name = name
        self.adb_ok = False

    # ---------- 生命周期 ----------
    def ldconsole(self, *args) -> str:
        """ldconsole 输出按系统代码页(gbk)解码。"""
        p = subprocess.run([config.LDCONSOLE, *args], capture_output=True, timeout=120,
                           creationflags=config.no_window_flags())
        return p.stdout.decode("gbk", errors="replace")

    def list_instances(self):
        """list2 输出: index,name,top,left,... 解析前两列。"""
        out = self.ldconsole("list2")
        rows = []
        for ln in out.splitlines():
            f = ln.strip().split(",")
            if len(f) >= 2:
                rows.append((f[0], f[1]))
        if not rows:  # 兜底旧版 list(仅名称)
            for ln in self.ldconsole("list").splitlines():
                if ln.strip():
                    rows.append(("0", ln.strip()))
        return rows

    def index(self) -> str:
        for idx, name in self.list_instances():
            if name == self.name:
                return idx
        return "0"

    def start(self, wait_boot: bool = True) -> bool:
        self.ldconsole("launch", "--index", self.index())
        if not wait_boot:
            return True
        deadline = time.time() + 120
        while time.time() < deadline:
            if self.connect_adb(quiet=True):
                return True
            time.sleep(3)
        return False

    def stop(self) -> None:
        self.ldconsole("quit", "--index", self.index())

    def is_running(self) -> bool:
        return self.adb_ok

    # ---------- adb ----------
    def connect_adb(self, quiet: bool = False) -> bool:
        """连接实例 adb;LDPlayer 每实例占用不同端口,0 号通常是 5555。"""
        try:
            _run(config.ADB, ["connect", f"{config.ADB_HOST}:{config.ADB_PORT}"], timeout=15)
            out = _run(config.ADB, ["-s", f"{config.ADB_HOST}:{config.ADB_PORT}", "get-state"], timeout=15)
            self.adb_ok = "device" in out.strip()
        except Exception:
            self.adb_ok = False
        if not quiet and not self.adb_ok:
            print(f"[emu] adb 连接失败({config.ADB_HOST}:{config.ADB_PORT})")
        return self.adb_ok

    def _dev(self, *args) -> str:
        if not self.adb_ok:
            raise RuntimeError("adb 未连接")
        return _run(config.ADB, ["-s", f"{config.ADB_HOST}:{config.ADB_PORT}", *args], timeout=40)

    def shell(self, cmd: str) -> str:
        return self._dev("shell", cmd)

    def screencap(self, save_path: str) -> bool:
        """截图并保存到本地 PNG。"""
        p = subprocess.run(
            [config.ADB, "-s", f"{config.ADB_HOST}:{config.ADB_PORT}", "exec-out", "screencap", "-p"],
            capture_output=True, timeout=40, creationflags=config.no_window_flags())
        if p.returncode != 0 or not p.stdout:
            return False
        with open(save_path, "wb") as f:
            f.write(p.stdout)
        return True

    # ---------- 输入 ----------
    def tap(self, x: int, y: int) -> None:
        self.shell(f"input tap {x} {y}")

    def tap_center(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.tap((x1 + x2) // 2, (y1 + y2) // 2)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, dur: int = 300) -> None:
        self.shell(f"input swipe {x1} {y1} {x2} {y2} {dur}")

    def input_text(self, s: str) -> None:
        """只能输入 ASCII;中文请走剪贴板/其他通道,本程序主要输数字代码/数量。"""
        safe = re.sub(r"[^A-Za-z0-9*#_\-.,%]", "", s)
        self.shell(f'input text "{safe}"')

    def keyevent(self, code: int) -> None:
        self.shell(f"input keyevent {code}")

    def home(self):
        self.keyevent(3)

    def back(self):
        self.keyevent(4)

    # ---------- UI 层级 ----------
    def dump_nodes(self, tries: int = 3) -> list[dict]:
        """返回节点列表: {text, desc, cx, cy, x1,y1,x2,y2}。"""
        xml = ""
        for _ in range(tries):
            self.shell("uiautomator dump /sdcard/_ui.xml")
            time.sleep(0.4)
            try:
                xml = self._dev("shell", "cat", "/sdcard/_ui.xml")
            except Exception:
                continue
            if "<hierarchy" in xml:
                break
            time.sleep(0.4)
        if "<hierarchy" not in xml:
            return []
        nodes = []
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            return []
        for n in root.iter("node"):
            text = n.get("text") or ""
            desc = n.get("content-desc") or ""
            b = n.get("bounds") or ""
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b)
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            cls = (n.get("class") or "").split(".")[-1]
            if text or desc:
                nodes.append({"text": text.strip(), "desc": desc.strip(),
                              "cx": (x1 + x2) // 2, "cy": (y1 + y2) // 2,
                              "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                              "cls": cls})
        return nodes

    def find(self, sub: str, field: str = "text") -> list[dict]:
        """在节点里按文本或 desc 子串查找。"""
        nodes = self.dump_nodes()
        return [n for n in nodes if sub in n.get(field, "")]

    def tap_by_text(self, sub: str, field: str = "text", exact: bool = False) -> bool:
        """点击含指定文本的第一个节点;找不到返回 False。"""
        nodes = self.dump_nodes()
        for n in nodes:
            v = n.get(field, "")
            if (v == sub if exact else sub in v):
                self.tap(n["cx"], n["cy"])
                time.sleep(1.0)
                return True
        return False

    def visible_text(self) -> str:
        """当前屏全部可见文本(诊断用)。"""
        return " | ".join(n["text"] for n in self.dump_nodes() if n["text"])

    def screen_size(self) -> tuple[int, int]:
        out = self.shell("wm size")
        m = re.search(r"(\d+)x(\d+)", out)
        return (int(m.group(1)), int(m.group(2))) if m else (config.SCREEN_W, config.SCREEN_H)


if __name__ == "__main__":
    e = Emulator()
    print("instances:", e.list_instances())
    print("adbrunning:", e.connect_adb(quiet=True))
