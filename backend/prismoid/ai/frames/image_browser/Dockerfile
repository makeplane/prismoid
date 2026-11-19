# The browser profile (ADR-066): today's frame plus a headless Chromium and the
# sidecar that drives it. Layered on the code tag (itself on base) so the toolchain is identical
# and a browser Machine runs every step; the browser is an addition, never a fork.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root

# Chromium from Debian bookworm, pinned to one exact build (ADR-066). The image tag hashes this
# file, so a bump is a new image on every worker, and an unbumped pin never drifts under the same
# tag. Debian drops a superseded build from the archive within weeks: a fresh build failing here
# is the reminder that the bump (to bookworm-security's current build) is overdue. Fonts so pages
# render as their authors meant.
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium=156.0.8078.79-1~deb12u1 \
      fonts-liberation fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*

# The `browser` user owns Chromium, its profile and the sidecar socket. The agent (uid 1000)
# cannot read any of it: cookies are tokens, and the shell must not hold them (ADR-066).
RUN useradd --uid 1001 --user-group --create-home --home-dir /home/browser --shell /usr/sbin/nologin browser \
 && mkdir -p /browser/profile /run/browser \
 && chown -R browser:browser /browser /run/browser \
 && chmod 0700 /browser /browser/profile /run/browser

# The sidecar: stdlib only, root-owned and read-only like the rest of /opt/plane.
COPY sidecar/ /opt/plane/browser/sidecar/
RUN chown -R root:root /opt/plane/browser && chmod -R a-w /opt/plane/browser
# The recording folder is written by the browser user into the loading dock; the agent may read it.
RUN mkdir -p /artifacts && chmod 1777 /artifacts

USER agent
WORKDIR /work
CMD ["sleep", "infinity"]
