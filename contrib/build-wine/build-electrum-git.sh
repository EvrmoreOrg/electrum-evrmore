#!/bin/bash

NAME_ROOT=electrum-ravencoin

export PYTHONDONTWRITEBYTECODE=1  # don't create __pycache__/ folders with .pyc files


# Let's begin!
set -e

. "$CONTRIB"/build_tools_util.sh

pushd $WINEPREFIX/drive_c/electrum

VERSION=`git describe --tags --dirty --always`
info "Last commit: $VERSION"

# Load electrum-locale for this release
git submodule update --init

pushd ./contrib/deterministic-build/electrum-locale
if ! which msgfmt > /dev/null 2>&1; then
    fail "Please install gettext"
fi
# we want the binary to have only compiled (.mo) locale files; not source (.po) files
rm -rf "$WINEPREFIX/drive_c/electrum/electrum/locale/"
for i in ./locale/*; do
    dir="$WINEPREFIX/drive_c/electrum/electrum/$i/LC_MESSAGES"
    mkdir -p $dir
    msgfmt --output-file="$dir/electrum.mo" "$i/electrum.po" || true
done
popd

find -exec touch -h -d '2000-11-11T11:11:11+00:00' {} +
popd

# Install frozen dependencies
$WINE_PYTHON -m pip install --no-build-isolation --no-dependencies --no-warn-script-location \
    --cache-dir "$WINE_PIP_CACHE_DIR" -r "$CONTRIB"/deterministic-build/requirements.txt

$WINE_PYTHON -m pip install --no-build-isolation --no-dependencies --no-warn-script-location \
    --cache-dir "$WINE_PIP_CACHE_DIR" -r "$CONTRIB"/deterministic-build/requirements-binaries.txt

$WINE_PYTHON -m pip install --no-build-isolation --no-dependencies --no-warn-script-location \
    --cache-dir "$WINE_PIP_CACHE_DIR" -r "$CONTRIB"/deterministic-build/requirements-hw.txt


X16R="x16r_hash-1.0.1-cp39-cp39-win32.whl"
X16RV2="x16rv2_hash-1.0-cp39-cp39-win32.whl"
KAWPOW="kawpow-0.9.4.4-cp39-cp39-win32.whl"

download_if_not_exist "$CACHEDIR/$X16R" "https://raw.githubusercontent.com/kralverde/electrum-ravencoin-wheels/master/$X16R"
verify_hash "$CACHEDIR/$X16R" "bb352c6d3dc17eca04f7b3d1db4dabe21d79707423cad306e32252db1a63ce67"
download_if_not_exist "$CACHEDIR/$X16RV2" "https://raw.githubusercontent.com/kralverde/electrum-ravencoin-wheels/master/$X16RV2"
verify_hash "$CACHEDIR/$X16RV2" "af2427be53f2256c01ac0c4ce7f7845582abddd9540351b5a78ec2166334694c"
download_if_not_exist "$CACHEDIR/$KAWPOW" "https://raw.githubusercontent.com/kralverde/electrum-ravencoin-wheels/master/$KAWPOW"
verify_hash "$CACHEDIR/$KAWPOW" "33dd35bf4ab2c819dda33bc407c7632c424e3a2fa13b621cf68be793f7f17630"

$WINE_PYTHON -m pip install --cache-dir "$WINE_PIP_CACHE_DIR" "$CACHEDIR/$X16R"
$WINE_PYTHON -m pip install --cache-dir "$WINE_PIP_CACHE_DIR" "$CACHEDIR/$X16RV2"
$WINE_PYTHON -m pip install --cache-dir "$WINE_PIP_CACHE_DIR" "$CACHEDIR/$KAWPOW"


pushd $WINEPREFIX/drive_c/electrum
# see https://github.com/pypa/pip/issues/2195 -- pip makes a copy of the entire directory
info "Pip installing Electrum. This might take a long time if the project folder is large."
$WINE_PYTHON -m pip install --no-build-isolation --no-dependencies --no-warn-script-location .
popd


rm -rf dist/

# build standalone and portable versions
info "Running pyinstaller..."
wine "$WINE_PYHOME/scripts/pyinstaller.exe" --noconfirm --ascii --clean --name $NAME_ROOT-$VERSION -w deterministic.spec

# set timestamps in dist, in order to make the installer reproducible
pushd dist
find -exec touch -h -d '2000-11-11T11:11:11+00:00' {} +
popd

info "building NSIS installer"
# $VERSION could be passed to the electrum.nsi script, but this would require some rewriting in the script itself.
wine "$WINEPREFIX/drive_c/Program Files (x86)/NSIS/makensis.exe" /DPRODUCT_VERSION=$VERSION electrum.nsi

cd dist
mv electrum-ravencoin-setup.exe $NAME_ROOT-$VERSION-setup.exe
cd ..

info "Padding binaries to 8-byte boundaries, and fixing COFF image checksum in PE header"
# note: 8-byte boundary padding is what osslsigncode uses:
#       https://github.com/mtrojnar/osslsigncode/blob/6c8ec4427a0f27c145973450def818e35d4436f6/osslsigncode.c#L3047
(
    cd dist
    for binary_file in ./*.exe; do
        info ">> fixing $binary_file..."
        # code based on https://github.com/erocarrera/pefile/blob/bbf28920a71248ed5c656c81e119779c131d9bd4/pefile.py#L5877
        python3 <<EOF
pe_file = "$binary_file"
with open(pe_file, "rb") as f:
    binary = bytearray(f.read())
pe_offset = int.from_bytes(binary[0x3c:0x3c+4], byteorder="little")
checksum_offset = pe_offset + 88
checksum = 0

# Pad data to 8-byte boundary.
remainder = len(binary) % 8
binary += bytes(8 - remainder)

for i in range(len(binary) // 4):
    if i == checksum_offset // 4:  # Skip the checksum field
        continue
    dword = int.from_bytes(binary[i*4:i*4+4], byteorder="little")
    checksum = (checksum & 0xffffffff) + dword + (checksum >> 32)
    if checksum > 2 ** 32:
        checksum = (checksum & 0xffffffff) + (checksum >> 32)

checksum = (checksum & 0xffff) + (checksum >> 16)
checksum = (checksum) + (checksum >> 16)
checksum = checksum & 0xffff
checksum += len(binary)

# Set the checksum
binary[checksum_offset : checksum_offset + 4] = int.to_bytes(checksum, byteorder="little", length=4)

with open(pe_file, "wb") as f:
    f.write(binary)
EOF
    done
)

sha256sum dist/electrum*.exe
