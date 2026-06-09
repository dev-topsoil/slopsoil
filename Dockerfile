FROM fedora:44

# ffmpeg-free is Fedora's standard FFmpeg build (no libx264, but includes
# libopenh264, h264_vaapi, h264_nvenc, and h264_qsv).  This matches the
# working host environment exactly.  Do NOT swap this for RPM Fusion's
# `ffmpeg` package: it ships a different FFmpeg version with libx264, and
# libx264's output currently causes Discord to drop the stream after one frame.
#
# ffmpeg-free's h264_vaapi encoder is only the libva *loader* — it still needs a
# hardware-specific VA-API driver to reach the GPU.  For AMD that's the radeonsi
# driver in mesa-va-drivers, but Fedora's stock build strips H.264/HEVC for patent
# reasons, so hardware H.264 encode requires mesa-va-drivers-freeworld from RPM
# Fusion.  For Intel iGPUs (Gen8+, e.g. UHD Graphics) the equivalent driver is
# intel-media-driver (the iHD driver) from RPM Fusion's nonfree repo — without it
# the h264_vaapi probe fails on Intel hardware and the bot silently falls back to
# the libopenh264 software encoder.  libva auto-selects the right driver from the
# /dev/dri device's PCI ID, so installing both drivers is enough; we deliberately
# do NOT hardcode LIBVA_DRIVER_NAME, which would force one vendor and break the
# other.  We enable RPM Fusion ONLY for these drivers (and libva-utils, which
# provides `vainfo` for debugging) — ffmpeg itself stays on ffmpeg-free.
RUN dnf install -y \
        ffmpeg-free \
        libva-utils \
        python3 \
        python3-pip \
        https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-44.noarch.rpm \
        https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-44.noarch.rpm \
    && dnf install -y --allowerasing mesa-va-drivers-freeworld intel-media-driver \
    && dnf clean all \
    && rm -rf /var/cache/dnf

WORKDIR /app

# Install Python deps before copying source so this layer is cached unless
# requirements.txt changes.
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "bot.py"]
