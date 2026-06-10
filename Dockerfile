FROM fedora:44

# ffmpeg-free is Fedora's standard FFmpeg build (no libx264, but includes
# libopenh264, h264_vaapi, h264_nvenc, and h264_qsv).  This matches the
# working host environment exactly.  Do NOT swap this for RPM Fusion's
# `ffmpeg` package: it ships a different FFmpeg version with libx264, and
# libx264's output currently causes Discord to drop the stream after one frame.
#
# ffmpeg-free's h264_vaapi encoder is only the libva *loader* — it still needs a
# hardware-specific VA-API driver to reach the GPU.  Fedora's stock VA-API drivers
# strip H.264/HEVC for patent reasons, so hardware H.264 encode needs the RPM
# Fusion builds.  We install one driver per supported GPU vendor:
#   * AMD            -> mesa-va-drivers-freeworld (radeonsi)  [RPM Fusion free]
#   * Intel Gen8+    -> intel-media-driver        (iHD)       [RPM Fusion nonfree]
#   * Intel pre-Gen8 -> libva-intel-driver        (i965)      [RPM Fusion free]
# Without the matching driver the h264_vaapi probe fails and the bot silently
# falls back to the libopenh264 software encoder.  (NVIDIA uses h264_nvenc, not
# VA-API; that encoder is already compiled into ffmpeg-free and loads the host's
# libnvidia-encode at runtime via the NVIDIA container toolkit — no image package
# needed.)  Fedora's libva searches /usr/lib64/dri, dri-freeworld and dri-nonfree
# by default and auto-selects the right driver from the /dev/dri device's PCI ID,
# so installing every driver side by side is enough; we deliberately do NOT
# hardcode LIBVA_DRIVER_NAME, which would force one vendor and break the others.
# We enable RPM Fusion ONLY for these drivers (and libva-utils, which provides
# `vainfo` for debugging) — ffmpeg itself stays on ffmpeg-free.
RUN dnf install -y \
        ffmpeg-free \
        libva-utils \
        python3 \
        python3-pip \
        https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-44.noarch.rpm \
        https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-44.noarch.rpm \
    && dnf install -y --allowerasing \
        mesa-va-drivers-freeworld \
        intel-media-driver \
        libva-intel-driver \
    && dnf clean all \
    && rm -rf /var/cache/dnf

WORKDIR /app

# Install Python deps before copying source so this layer is cached unless
# requirements.txt changes.
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "bot.py"]
