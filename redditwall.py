#!/usr/bin/env python3
"""
RedditWall - system tray app that pulls landscape images from subreddits
and sets them as your desktop wallpaper.

Usage:
    redditwall.py                       # use saved config
    redditwall.py --subs earthporn wallpaper
    redditwall.py --once                # change wallpaper once and exit (no tray)

Deps: PyQt6, requests, Pillow
"""
import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from PIL import Image
from PyQt6.QtCore import QObject, QPoint, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap, QPolygon
from PyQt6.QtWidgets import QApplication, QInputDialog, QMenu, QSystemTrayIcon

APP = "redditwall"
CONF_DIR = Path.home() / ".config" / APP
CONF_FILE = CONF_DIR / "config.json"
STATE_FILE = CONF_DIR / "state.json"
IMG_DIR = Path.home() / "Pictures" / "RedditWall"
AUTOSTART_FILE = Path.home() / ".config" / "autostart" / f"{APP}.desktop"
UA = "linux:redditwall:1.0 (personal wallpaper changer, low-volume)"
EXTS = (".jpg", ".jpeg", ".png", ".webp")

DEFAULTS = {
    "subreddits": ["wallpaper", "earthporn", "wallpapers"],
    "interval_minutes": 30,
    "sort": "hot",          # hot | new | top | rising
    "time": "week",         # for sort=top: hour/day/week/month/year/all
    "min_width": 1920,
    "min_height": 1080,
    "min_ratio": 1.3,       # width/height must be >= this (landscape)
    "keep_last": 50,        # how many downloaded images to keep
    "nsfw": False,
    "paused": False,
    "client_id": "",        # optional: Reddit "script" app credentials
    "client_secret": "",
}


# --------------------------------------------------------------------------- #
# Config / state
# --------------------------------------------------------------------------- #
def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


cfg = {**DEFAULTS, **load_json(CONF_FILE, {})}
state = {"used": [], "current": None, "permalink": None, **load_json(STATE_FILE, {})}
lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Reddit fetching
# --------------------------------------------------------------------------- #
_token = {"value": None, "exp": 0.0}


def oauth_token():
    """Only used if you have Reddit-approved API credentials in config.json."""
    if _token["value"] and time.time() < _token["exp"]:
        return _token["value"]
    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(cfg["client_id"], cfg["client_secret"]),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": UA},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    _token["value"] = j["access_token"]
    _token["exp"] = time.time() + j.get("expires_in", 3600) - 60
    return _token["value"]


def rss_posts(sub, sort, t):
    """Read the subreddit's public RSS feed (like a feed reader would)."""
    import re
    import xml.etree.ElementTree as ET
    params = {"limit": 100}
    if t:
        params["t"] = t
    r = requests.get(f"https://www.reddit.com/r/{sub}/{sort}.rss",
                     headers={"User-Agent": UA}, params=params, timeout=20)
    r.raise_for_status()
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in ET.fromstring(r.content).findall("a:entry", ns):
        html = e.findtext("a:content", default="", namespaces=ns) or ""
        m = re.search(r'https://i\.redd\.it/[^"\s<]+\.(?:jpe?g|png|webp)', html)
        if not m:
            continue
        link = e.find("a:link", ns)
        href = link.get("href") if link is not None else ""
        out.append({
            "id": (e.findtext("a:id", default="", namespaces=ns) or "").replace("t3_", ""),
            "url": m.group(0),
            "permalink": href.replace("https://www.reddit.com", ""),
            "over_18": False,
        })
    return out


def listing_posts(sub):
    """Return post dicts for a subreddit: approved OAuth -> RSS -> public JSON."""
    sort = cfg["sort"]
    t = cfg["time"] if sort in ("top", "controversial") else None
    params = {"limit": 100, "raw_json": 1, **({"t": t} if t else {})}
    errors = []

    if cfg["client_id"] and cfg["client_secret"]:
        try:
            r = requests.get(
                f"https://oauth.reddit.com/r/{sub}/{sort}",
                headers={"User-Agent": UA, "Authorization": f"bearer {oauth_token()}"},
                params=params, timeout=20)
            r.raise_for_status()
            return [c["data"] for c in r.json()["data"]["children"]]
        except Exception as e:
            errors.append(f"oauth: {e}")

    try:
        return rss_posts(sub, sort, t)
    except Exception as e:
        errors.append(f"rss: {e}")

    try:
        r = requests.get(f"https://www.reddit.com/r/{sub}/{sort}.json",
                         headers={"User-Agent": UA}, params=params, timeout=20)
        r.raise_for_status()
        return [c["data"] for c in r.json()["data"]["children"]]
    except Exception as e:
        errors.append(f"json: {e}")

    raise RuntimeError("; ".join(errors))


def candidates(sub):
    """Yield (post_id, image_url, permalink) for landscape-looking image posts."""
    for d in listing_posts(sub):
        if d.get("over_18") and not cfg["nsfw"]:
            continue
        url = d.get("url_overridden_by_dest") or d.get("url") or ""
        if not url.lower().split("?")[0].endswith(EXTS):
            continue
        try:  # cheap pre-filter from preview metadata (absent in RSS; checked after download)
            src = d["preview"]["images"][0]["source"]
            w, h = src["width"], src["height"]
            if w < cfg["min_width"] or h < cfg["min_height"] or w / h < cfg["min_ratio"]:
                continue
        except (KeyError, IndexError, TypeError, ZeroDivisionError):
            pass
        yield d["id"], url, "https://reddit.com" + d["permalink"]


def download(sub, post_id, url):
    """Download image, verify it's a real landscape image. Return Path or None."""
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(url.split("?")[0]).suffix.lower() or ".jpg"
    dest = IMG_DIR / f"{sub}_{post_id}{ext}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(65536):
                    f.write(chunk)
        with Image.open(tmp) as im:
            w, h = im.size
        if w < cfg["min_width"] or h < cfg["min_height"] or w / h < cfg["min_ratio"]:
            tmp.unlink(missing_ok=True)
            return None
        tmp.rename(dest)
        return dest
    except Exception:
        tmp.unlink(missing_ok=True)
        return None


# --------------------------------------------------------------------------- #
# Setting the wallpaper
# --------------------------------------------------------------------------- #
def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def which(name):
    from shutil import which as w
    return w(name)


def set_wallpaper(path: Path):
    path = Path(path).resolve()
    uri = path.as_uri()
    de = (os.environ.get("XDG_CURRENT_DESKTOP", "") + ":" +
          os.environ.get("DESKTOP_SESSION", "")).lower()

    if "cinnamon" in de:
        run("gsettings", "set", "org.cinnamon.desktop.background", "picture-uri", uri)
        run("gsettings", "set", "org.cinnamon.desktop.background", "picture-options", "zoom")
    elif "mate" in de:
        run("gsettings", "set", "org.mate.background", "picture-filename", str(path))
    elif "gnome" in de or "unity" in de or "budgie" in de or "ubuntu" in de or "pop" in de:
        run("gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri)
        run("gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri)
        run("gsettings", "set", "org.gnome.desktop.background", "picture-options", "zoom")
    elif "kde" in de or "plasma" in de:
        run("plasma-apply-wallpaperimage", str(path))
    elif "xfce" in de:
        props = run("xfconf-query", "-c", "xfce4-desktop", "-l").stdout.splitlines()
        for p in props:
            if p.endswith("last-image"):
                run("xfconf-query", "-c", "xfce4-desktop", "-p", p, "-s", str(path))
    elif "lxqt" in de and which("pcmanfm-qt"):
        run("pcmanfm-qt", "--set-wallpaper", str(path), "--wallpaper-mode=fit")
    elif which("feh"):
        run("feh", "--bg-fill", str(path))
    elif which("nitrogen"):
        run("nitrogen", "--set-zoom-fill", "--save", str(path))
    else:
        raise RuntimeError(f"Don't know how to set wallpaper on desktop '{de}'. Install feh.")


# --------------------------------------------------------------------------- #
# Main action
# --------------------------------------------------------------------------- #
def change_wallpaper():
    """Pick a fresh landscape image and apply it. Returns (ok, message)."""
    if not lock.acquire(blocking=False):
        return False, "Already working on it..."
    try:
        subs = list(cfg["subreddits"])
        random.shuffle(subs)
        errors = []
        for sub in subs:
            try:
                posts = [p for p in candidates(sub) if p[0] not in state["used"]]
            except Exception as e:
                errors.append(f"r/{sub}: {e}")
                continue
            random.shuffle(posts)
            for post_id, url, permalink in posts[:15]:
                path = download(sub, post_id, url)
                if not path:
                    state["used"].append(post_id)  # don't retry rejects
                    continue
                set_wallpaper(path)
                state["used"] = (state["used"] + [post_id])[-2000:]
                state["current"] = str(path)
                state["permalink"] = permalink
                save_json(STATE_FILE, state)
                cleanup()
                return True, f"Wallpaper set from r/{sub}"
        save_json(STATE_FILE, state)
        return False, "No suitable landscape image found. " + "; ".join(errors[:2])
    except Exception as e:
        return False, str(e)
    finally:
        lock.release()


def cleanup():
    files = sorted(IMG_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[cfg["keep_last"]:]:
        if str(f) != state.get("current"):
            f.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Autostart
# --------------------------------------------------------------------------- #
def autostart_enabled():
    return AUTOSTART_FILE.exists()


def set_autostart(on):
    if on:
        AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART_FILE.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=RedditWall\n"
            "Comment=Reddit wallpaper changer\n"
            f"Exec={sys.executable} {Path(__file__).resolve()}\n"
            "Icon=preferences-desktop-wallpaper\n"
            "X-GNOME-Autostart-enabled=true\n"
            "X-GNOME-Autostart-Delay=10\n"
            "Terminal=false\n"
        )
    else:
        AUTOSTART_FILE.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Tray UI
# --------------------------------------------------------------------------- #
class Bridge(QObject):
    finished = pyqtSignal(bool, str)


def make_icon():
    icon = QIcon.fromTheme("preferences-desktop-wallpaper")
    if not icon.isNull():
        return icon
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setBrush(QColor("#ff4500"))
    p.setPen(QColor("#ff4500"))
    p.drawRoundedRect(4, 12, 56, 40, 6, 6)
    p.setBrush(QColor("white"))
    p.drawEllipse(40, 20, 10, 10)
    p.drawPolygon(QPolygon([QPoint(x, y) for x, y in [(8, 48), (24, 28), (36, 42), (44, 34), (56, 48)]]))
    p.end()
    return QIcon(pm)


class Tray:
    def __init__(self, app):
        self.app = app
        self.bridge = Bridge()
        self.bridge.finished.connect(self.on_done)
        self.tray = QSystemTrayIcon(make_icon(), app)
        self.tray.setToolTip("RedditWall")
        self.build_menu()
        self.tray.activated.connect(self.on_activated)
        self.tray.show()

        self.timer = QTimer()
        self.timer.timeout.connect(self.tick)
        self.timer.start(cfg["interval_minutes"] * 60 * 1000)

        # first run shortly after login (give the network a moment)
        QTimer.singleShot(5000, self.tick)

    # -- menu --
    def build_menu(self):
        m = QMenu()
        m.addAction("Next wallpaper", self.next_now)

        self.pause_act = QAction("Pause", m, checkable=True, checked=cfg["paused"])
        self.pause_act.toggled.connect(self.toggle_pause)
        m.addAction(self.pause_act)

        iv = m.addMenu("Change every")
        for mins in (10, 15, 30, 60, 180, 360, 1440):
            label = f"{mins} min" if mins < 60 else f"{mins // 60} h"
            a = iv.addAction(label)
            a.setCheckable(True)
            a.setChecked(cfg["interval_minutes"] == mins)
            a.triggered.connect(lambda _=False, v=mins: self.set_interval(v))

        m.addAction("Subreddits...", self.edit_subs)
        m.addSeparator()
        m.addAction("Open post on Reddit", self.open_post)
        m.addAction("Open image folder", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(IMG_DIR))))
        m.addSeparator()
        auto = QAction("Start on login", m, checkable=True, checked=autostart_enabled())
        auto.toggled.connect(set_autostart)
        m.addAction(auto)
        m.addAction("Quit", self.app.quit)
        self.menu = m
        self.tray.setContextMenu(m)

    # -- actions --
    def tick(self):
        if not cfg["paused"]:
            self.next_now(silent=True)

    def next_now(self, silent=False):
        def work():
            ok, msg = change_wallpaper()
            self.bridge.finished.emit(ok, "" if (ok and silent) else msg)
        threading.Thread(target=work, daemon=True).start()

    def on_done(self, ok, msg):
        if msg:
            self.tray.showMessage("RedditWall", msg,
                                  QSystemTrayIcon.MessageIcon.Information if ok
                                  else QSystemTrayIcon.MessageIcon.Warning, 4000)
        if not ok and "Already" not in msg:
            QTimer.singleShot(5 * 60 * 1000, self.tick)  # retry later (e.g. offline)

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.next_now()

    def toggle_pause(self, on):
        cfg["paused"] = on
        save_json(CONF_FILE, cfg)

    def set_interval(self, mins):
        cfg["interval_minutes"] = mins
        save_json(CONF_FILE, cfg)
        self.timer.start(mins * 60 * 1000)
        self.build_menu()

    def edit_subs(self):
        text, ok = QInputDialog.getText(
            None, "Subreddits", "Comma-separated (without r/):",
            text=", ".join(cfg["subreddits"]))
        if ok:
            subs = [s.strip().removeprefix("r/") for s in text.split(",") if s.strip()]
            if subs:
                cfg["subreddits"] = subs
                save_json(CONF_FILE, cfg)

    def open_post(self):
        if state.get("permalink"):
            QDesktopServices.openUrl(QUrl(state["permalink"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subs", nargs="+", help="subreddits to use (saved to config)")
    ap.add_argument("--once", action="store_true", help="change once and exit")
    args = ap.parse_args()

    if args.subs:
        cfg["subreddits"] = [s.removeprefix("r/") for s in args.subs]
    save_json(CONF_FILE, cfg)

    if args.once:
        ok, msg = change_wallpaper()
        print(msg)
        sys.exit(0 if ok else 1)

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # At login the tray host may not be ready yet; wait up to ~60s.
    for _ in range(30):
        if QSystemTrayIcon.isSystemTrayAvailable():
            break
        time.sleep(2)
        app.processEvents()
    else:
        print("No system tray available (GNOME needs the AppIndicator extension).")

    tray = Tray(app)  # noqa: F841 (keep reference)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
