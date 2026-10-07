"""The name and icon everyone sees: the admin website and its favicon, the phone and computer apps,
the icon by the clock, the desktop shortcut, and (after the app is rebuilt) the app's own icon and
name on phones' home screens.

Set in the admin website (Settings → Name and icon), by the Windows installer, or with
`python -m songarr --brand-name "..." --brand-icon picture.png`. Any picture Pillow reads works
(PNG, JPEG, WebP, GIF, BMP, ICO); it's fitted into a square without cropping.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from xml.sax.saxutils import escape

if TYPE_CHECKING:
    from .db import DB

log = logging.getLogger(__name__)

DEFAULT_NAME = "Songarr"
DEFAULT_ICON = Path(__file__).with_name("assets") / "songarr.ico"
DEFAULT_BACKGROUND = "#8B5CF6"  # behind the icon on Android home screens (as the built-in icon)
NAME_MAX = 30
ICON_MAX_BYTES = 20 * 1024 * 1024
SIZES = (16, 32, 48, 64, 72, 96, 128, 144, 180, 192, 256, 512)  # square PNGs served to the website and the apps
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)  # Windows: tray, shortcut, favicon.ico
# Android home-screen icons, per screen density: the classic square icon, and the adaptive icon's
# foreground layer (108 dp, of which the middle 72 dp shows through the launcher's mask)
ANDROID = {"mdpi": (48, 108), "hdpi": (72, 162), "xhdpi": (96, 216), "xxhdpi": (144, 324), "xxxhdpi": (192, 432)}


class BrandError(ValueError):
    pass


def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name:
        raise BrandError("Give it a name.")
    if len(name) > NAME_MAX:
        raise BrandError(f"Keep the name to {NAME_MAX} characters or fewer.")
    if any(ord(c) < 32 for c in name) or any(c in name for c in '<>"\\/|?*:'):
        raise BrandError('The name can\'t contain < > " \\ / | ? * or :')
    return name


def clean_color(color: str) -> str:
    color = str(color or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise BrandError("Colours look like #8B5CF6.")
    return color.upper()


def file_name(name: str) -> str:
    """The name, safe for a file name (shortcuts are named after it)."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(" .") or DEFAULT_NAME


def render(data: bytes) -> dict[str, bytes]:
    """Every file Songarr needs from one picture: PNGs by size, a Windows .ico, and Android's icons."""
    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(data) > ICON_MAX_BYTES:
        raise BrandError("That picture is too big (20 MB at most).")
    try:
        with Image.open(io.BytesIO(data)) as im:
            if getattr(im, "n_frames", 1) > 1:
                im.seek(0)  # an animated picture: its first frame
            im.load()
            im = ImageOps.exif_transpose(im).convert("RGBA")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as e:
        raise BrandError("That isn't a picture Songarr can read. Use a PNG, JPEG, WebP, GIF, BMP or ICO file.") from e
    if min(im.size) < 16:
        raise BrandError("That picture is too small (16 × 16 pixels at least; 512 × 512 or bigger looks best).")
    side = max(im.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    if side > 1024:
        square = square.resize((1024, 1024), Image.LANCZOS)
    see_through = square.getchannel("A").getextrema()[0] < 250  # a logo on nothing, rather than a full square

    def png(img: Image.Image) -> bytes:
        out = io.BytesIO()
        img.save(out, "PNG", optimize=True)
        return out.getvalue()

    def sized(s: int) -> Image.Image:
        return square.resize((s, s), Image.LANCZOS)

    files = {f"icon-{s}.png": png(sized(s)) for s in SIZES}
    ico = io.BytesIO()
    sized(256).save(ico, "ICO", sizes=[(s, s) for s in ICO_SIZES])
    files["icon.ico"] = ico.getvalue()
    # Android: a see-through logo sits inside the launcher's safe zone on the background colour;
    # a full square picture fills the visible part of the icon.
    fill = 0.6 if see_through else 72 / 108
    for dpi, (legacy, layer) in ANDROID.items():
        fg = Image.new("RGBA", (layer, layer), (0, 0, 0, 0))
        inner = round(layer * fill)
        fg.alpha_composite(sized(inner), ((layer - inner) // 2, (layer - inner) // 2))
        files[f"android-{dpi}-foreground.png"] = png(fg)
        files[f"android-{dpi}-legacy.png"] = png(sized(legacy))
    files["see-through"] = b"1" if see_through else b"0"
    return files


class Branding:
    def __init__(self, db: DB, data_dir: Path):
        self.db = db
        self.dir = Path(data_dir) / "branding"
        self.listeners: list[Callable[[], None]] = []  # told after a change: the tray icon, the shortcuts

    # -- what's set now -----------------------------------------------------------------------------

    def name(self) -> str:
        return self.db.setting("brand_name") or DEFAULT_NAME

    def icon_id(self) -> str:
        """"" for Songarr's own icon, else the folder (named by content) with the chosen one."""
        icon = self.db.setting("brand_icon") or ""
        return icon if re.fullmatch(r"[0-9a-f]{16}", icon) and (self.dir / icon / "icon.ico").is_file() else ""

    def background(self) -> str:
        return self.db.setting("brand_background") or DEFAULT_BACKGROUND

    def info(self) -> dict:
        icon = self.icon_id()
        see_through = icon and (self.dir / icon / "see-through").read_bytes() == b"1"
        return {"name": self.name(), "icon": icon or None, "background": self.background(),
                "see_through": bool(see_through), "default_name": DEFAULT_NAME,
                "custom": self.name() != DEFAULT_NAME or bool(icon)}

    def file(self, name: str) -> Path | None:
        """One of the chosen icon's files (icon-192.png, icon.ico, ...), or None with Songarr's own icon."""
        icon = self.icon_id()
        if not icon or not re.fullmatch(r"[a-z0-9.-]+", name):
            return None
        path = self.dir / icon / name
        return path if path.is_file() else None

    def png(self, size: int) -> Path | None:
        """The chosen icon as a PNG at least [size] pixels square (the nearest made)."""
        fit = next((s for s in SIZES if s >= size), SIZES[-1])
        return self.file(f"icon-{fit}.png")

    def ico(self) -> Path:
        return self.file("icon.ico") or DEFAULT_ICON

    # -- changing it --------------------------------------------------------------------------------

    def set(self, name: str | None = None, icon: bytes | None = None, reset_icon: bool = False,
            background: str | None = None) -> dict:
        """Change the name and/or icon. [icon] is a picture file's bytes; [reset_icon] goes back to Songarr's."""
        changes = []
        if name is not None:
            name = clean_name(name)
            if name != self.name():
                self.db.set_setting("brand_name", name)
                changes.append(f'name "{name}"')
        if background is not None:
            background = clean_color(background)
            if background != self.background():
                self.db.set_setting("brand_background", background)
                changes.append("icon background")
        if icon is not None:
            files = render(icon)
            ident = hashlib.sha256(icon).hexdigest()[:16]
            folder = self.dir / ident
            if not (folder / "icon.ico").is_file():
                tmp = self.dir / (ident + ".part")
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True)
                for fname, data in files.items():
                    (tmp / fname).write_bytes(data)
                shutil.rmtree(folder, ignore_errors=True)
                tmp.rename(folder)
            if ident != self.icon_id():
                self.db.set_setting("brand_icon", ident)
                changes.append("icon")
        elif reset_icon and self.icon_id():
            self.db.set_setting("brand_icon", "")
            changes.append("icon (back to Songarr's)")
        if changes:
            self.db.log("branding", "Changed the " + " and ".join(changes) + ".")
            self._tell()
        return self.info()

    def _tell(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                log.exception("branding listener failed")

    def tidy(self) -> None:
        """Remove icons no longer used (after shortcuts have moved to the new one)."""
        keep = self.icon_id()
        if not self.dir.is_dir():
            return
        for child in self.dir.iterdir():
            if child.is_dir() and child.name != keep:
                shutil.rmtree(child, ignore_errors=True)

    # -- the Android app's own name and icon (used when rebuilding it) --------------------------------

    def write_android(self, res: Path) -> None:
        """Release-build resources overriding the app's built-in name and icon (nothing when it's Songarr's)."""
        shutil.rmtree(res, ignore_errors=True)
        icon = self.icon_id()
        if self.name() == DEFAULT_NAME and not icon:
            return
        (res / "values").mkdir(parents=True)
        values = [f'    <string name="app_name">{escape(self.name())}</string>']
        if icon:
            values.append(f'    <color name="brand_icon_background">{self.background()}</color>')
            for dpi in ANDROID:
                folder = res / f"mipmap-{dpi}"
                folder.mkdir()
                shutil.copyfile(self.dir / icon / f"android-{dpi}-legacy.png", folder / "ic_launcher.png")
                shutil.copyfile(self.dir / icon / f"android-{dpi}-foreground.png", folder / "ic_launcher_brand.png")
            (res / "mipmap-anydpi-v26").mkdir()
            (res / "mipmap-anydpi-v26" / "ic_launcher.xml").write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n'
                '<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">\n'
                '    <background android:drawable="@color/brand_icon_background"/>\n'
                '    <foreground android:drawable="@mipmap/ic_launcher_brand"/>\n'
                '</adaptive-icon>\n', encoding="utf-8")
        (res / "values" / "brand.xml").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<!-- Written by Songarr (Settings → Name and icon); '
            'changes here are replaced. -->\n<resources>\n' + "\n".join(values) + "\n</resources>\n", encoding="utf-8")
