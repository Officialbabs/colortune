#!/bin/sh
# ColorTune installer: system packages (optional extras), DDC/CI access, launcher + menu entry.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing dependencies (needs sudo)"
if command -v apt-get >/dev/null; then
    sudo apt-get install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-gi-cairo ddcutil colord argyll
elif command -v dnf >/dev/null; then
    sudo dnf install -y python3-gobject gtk4 libadwaita python3-cairo ddcutil colord argyllcms
elif command -v pacman >/dev/null; then
    sudo pacman -S --needed --noconfirm python-gobject gtk4 libadwaita python-cairo ddcutil colord argyllcms
elif command -v zypper >/dev/null; then
    sudo zypper install -y python3-gobject python3-gobject-Gdk typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 python3-pycairo ddcutil colord argyllcms
else
    echo "Unknown package manager - install: PyGObject + GTK 4 + libadwaita, pycairo, ddcutil, colord, ArgyllCMS"
fi

echo "==> Enabling DDC/CI access (monitor hardware controls)"
echo i2c-dev | sudo tee /etc/modules-load.d/i2c-dev.conf >/dev/null
sudo modprobe i2c-dev || true
if getent group i2c >/dev/null; then
    sudo usermod -aG i2c "$USER" && echo "   added $USER to group i2c (log out and back in)"
fi

echo "==> Installing launcher and icon"
mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.local/share/icons"
cp -r "$HERE/data/icons/hicolor" "$HOME/.local/share/icons/"
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -qtf "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
ln -sf "$HERE/bin/colortune" "$HOME/.local/bin/colortune"
sed "s|@EXEC@|$HOME/.local/bin/colortune|" "$HERE/colortune.desktop.in" > "$HOME/.local/share/applications/colortune.desktop"

echo "Done. Run 'colortune' or open ColorTune from your app menu. 'colortune detect' shows what your setup supports."
