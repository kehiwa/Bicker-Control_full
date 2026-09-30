#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_root"
version=$(python3 -c 'import tomllib; from pathlib import Path; print(tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"])' < "$project_root/pyproject.toml")
architecture=${DEB_ARCHITECTURE:-$(dpkg --print-architecture)}
package_name="bicker-control-full"
output_dir=${1:-"$project_root/dist"}
build_dir=$(mktemp -d)
package_root="$build_dir/package"
trap 'rm -rf "$build_dir"' EXIT

mkdir -p "$package_root/DEBIAN" "$package_root/opt/bicker-control" \
  "$package_root/etc/bicker-control" "$package_root/etc/systemd/system" \
  "$package_root/usr/local/bin" "$package_root/usr/share/snmp/mibs"

python3 -m venv "$package_root/opt/bicker-control/venv"
"$package_root/opt/bicker-control/venv/bin/python" -m pip install --upgrade pip
"$package_root/opt/bicker-control/venv/bin/python" -m pip install --no-cache-dir "$project_root[device]"

ln -s /opt/bicker-control/venv/bin/bicker-control "$package_root/usr/local/bin/bicker-control"
install -m 0644 "$project_root/packaging/systemd/bicker-control.service" \
  "$package_root/etc/systemd/system/bicker-control.service"
install -m 0640 "$project_root/packaging/bicker-control.env.example" \
  "$package_root/etc/bicker-control/bicker-control.env"
install -m 0644 "$project_root/mibs/BICKER-CONTROL-MIB.mib" \
  "$package_root/usr/share/snmp/mibs/BICKER-CONTROL-MIB.mib"

cat > "$package_root/DEBIAN/control" <<EOF
Package: $package_name
Version: $version
Section: admin
Priority: optional
Architecture: $architecture
Maintainer: Bicker Control
Depends: python3 (>= 3.11), python3-venv, adduser, systemd
Description: Bicker UPS controller
 Network controller and web API for the Bicker UPSI-2412D.
 The package includes the Python runtime dependencies and systemd service.
EOF

cat > "$package_root/DEBIAN/conffiles" <<EOF
/etc/bicker-control/bicker-control.env
EOF

cat > "$package_root/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e

if ! getent passwd bicker-control >/dev/null; then
    adduser --system --home /var/lib/bicker-control --no-create-home \
        --shell /usr/sbin/nologin bicker-control
fi
adduser bicker-control dialout >/dev/null 2>&1 || true
install -d -o bicker-control -g bicker-control -m 0750 /var/lib/bicker-control
install -d -o root -g bicker-control -m 0750 /etc/bicker-control
chown root:bicker-control /etc/bicker-control/bicker-control.env
chmod 0640 /etc/bicker-control/bicker-control.env
systemctl daemon-reload
systemctl enable bicker-control.service
systemctl try-restart bicker-control.service || true
exit 0
EOF

cat > "$package_root/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = remove ] || [ "$1" = upgrade ]; then
    systemctl stop bicker-control.service || true
fi
exit 0
EOF

chmod 0755 "$package_root/DEBIAN/postinst" "$package_root/DEBIAN/prerm"
mkdir -p "$output_dir"
dpkg-deb --build --root-owner-group "$package_root" \
  "$output_dir/${package_name}_${version}_${architecture}.deb"